from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import respx
from httpx import AsyncClient, ConnectError, HTTPStatusError, Response

import src.edge.forwarder as forwarder_module
from src.edge.forwarder import CLOUD_NODES, route_study
from src.edge.orthanc_source import OrthancSource
from src.edge.transport import DicomSource

pytestmark = pytest.mark.unit

_EDGE_BASE = "http://edge-orthanc:8042"
_ORCH_URL = "http://orchestrator:8000/get-best-node"
_ORCH_HB_URL = "http://orchestrator:8000/heartbeat"
_DCM_BYTES = b"DICM_FAKE_BYTES"

_BEST_NODE_RESP = {
    "node_id": "us-east1",
    "ae_title": "Orthanc_US",
    "base_url": "http://orthanc-us:8042",
    "score": 0.75,
    "queue_size": 1,
    "disk_free_mb": 5000.0,
    "rtt_ms": 45.0,
}

_STUDY = "study-1"
_US_ONLY = {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}}


@pytest.fixture(autouse=True)
def _reset_pending_ack(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_pending_ack", set())


class _StubSource(DicomSource):
    """Controllable DicomSource for forwarder unit tests."""

    def __init__(
        self,
        studies: dict[str, list[str]] | None = None,
        dcm_bytes: bytes = _DCM_BYTES,
        fetch_raises: Exception | None = None,
        ack_raises: Exception | None = None,
        fetch_raises_for: set[str] | None = None,
    ) -> None:
        self.studies = studies or {}
        self.dcm_bytes = dcm_bytes
        self.fetch_raises = fetch_raises
        self.ack_raises = ack_raises
        self.fetch_raises_for = fetch_raises_for or set()
        self.fetched: list[str] = []
        self.acknowledged: list[str] = []

    async def poll_new(self, client: AsyncClient) -> dict[str, list[str]]:
        return self.studies

    @asynccontextmanager
    async def open_stream(
        self, client: AsyncClient, instance_id: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        if self.fetch_raises:
            raise self.fetch_raises
        if instance_id in self.fetch_raises_for:
            raise ConnectError("edge read failed")
        self.fetched.append(instance_id)

        async def _body() -> AsyncIterator[bytes]:
            yield self.dcm_bytes

        yield _body()

    async def acknowledge(self, client: AsyncClient, instance_id: str) -> None:
        if self.ack_raises:
            raise self.ack_raises
        self.acknowledged.append(instance_id)


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_groups_instances_by_study():
    respx.get(f"{_EDGE_BASE}/studies").mock(return_value=Response(200, json=["s1", "s2"]))
    respx.get(f"{_EDGE_BASE}/studies/s1/instances").mock(
        return_value=Response(200, json=[{"ID": "abc123"}, {"ID": "def456"}])
    )
    respx.get(f"{_EDGE_BASE}/studies/s2/instances").mock(
        return_value=Response(200, json=[{"ID": "ghi789"}])
    )
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s1": ["abc123", "def456"], "s2": ["ghi789"]}


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_returns_empty_dict_when_no_studies():
    respx.get(f"{_EDGE_BASE}/studies").mock(return_value=Response(200, json=[]))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {}


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_skips_study_deleted_between_requests():
    respx.get(f"{_EDGE_BASE}/studies").mock(return_value=Response(200, json=["gone", "s1"]))
    respx.get(f"{_EDGE_BASE}/studies/gone/instances").mock(return_value=Response(404))
    respx.get(f"{_EDGE_BASE}/studies/s1/instances").mock(
        return_value=Response(200, json=[{"ID": "abc123"}])
    )
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s1": ["abc123"]}


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_raises_on_http_error():
    respx.get(f"{_EDGE_BASE}/studies").mock(return_value=Response(500))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        with pytest.raises(HTTPStatusError):
            await source.poll_new(client)


@respx.mock
@pytest.mark.asyncio
async def test_open_stream_yields_dicom_bytes():
    respx.get(f"{_EDGE_BASE}/instances/abc123/file").mock(
        return_value=Response(200, content=_DCM_BYTES)
    )
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        async with source.open_stream(client, "abc123") as body:
            data = b"".join([chunk async for chunk in body])
    assert data == _DCM_BYTES


@respx.mock
@pytest.mark.asyncio
async def test_open_stream_raises_on_http_error():
    respx.get(f"{_EDGE_BASE}/instances/abc123/file").mock(return_value=Response(404))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        with pytest.raises(HTTPStatusError):
            async with source.open_stream(client, "abc123"):
                pass


@respx.mock
@pytest.mark.asyncio
async def test_acknowledge_deletes_instance():
    route = respx.delete(f"{_EDGE_BASE}/instances/abc123").mock(return_value=Response(200))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        await source.acknowledge(client, "abc123")
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_acknowledge_raises_on_http_error():
    respx.delete(f"{_EDGE_BASE}/instances/abc123").mock(return_value=Response(500))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        with pytest.raises(HTTPStatusError):
            await source.acknowledge(client, "abc123")


@respx.mock
@pytest.mark.asyncio
async def test_route_study_happy_path(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {
            "us-east1": {
                "base": "http://orthanc-us:8042",
                "auth": ("orthanc", "orthanc"),
            }
        },
    )

    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(dcm_bytes=_DCM_BYTES)
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert source.fetched == ["abc123"]
    assert source.acknowledged == ["abc123"]
    assert post_route.called
    assert post_route.calls[0].request.content == _DCM_BYTES


@respx.mock
@pytest.mark.asyncio
async def test_route_study_sets_dicom_content_type(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    async with AsyncClient() as client:
        await route_study(client, _StubSource(), _STUDY, ["abc123"])

    assert post_route.calls[0].request.headers["content-type"] == "application/dicom"


@respx.mock
@pytest.mark.asyncio
async def test_route_study_stream_failure_skips_forward(monkeypatch):
    """A failed edge read (stream open) → no cloud POST and no acknowledge."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(200))

    source = _StubSource(fetch_raises=ConnectError("timeout"))
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_study_orchestrator_failure_aborts_early(monkeypatch):
    """Orchestrator failure → cloud POST never called, no acknowledge."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(503))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(200))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_study_unknown_node_id_aborts_early(monkeypatch):
    """Orchestrator returns a node_id not in CLOUD_NODES → no POST, no acknowledge."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", {})

    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(200))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_study_cloud_post_failure_skips_acknowledge(monkeypatch):
    """Cloud POST failure → acknowledge (delete) must NOT be called to avoid data loss."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(500))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_study_acknowledge_failure_does_not_raise(monkeypatch):
    """Acknowledge failure is logged as a warning — the function must not propagate."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(ack_raises=ConnectError("timeout"))
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])


@respx.mock
@pytest.mark.asyncio
async def test_route_study_retries_failed_acknowledge_without_reforwarding(monkeypatch):
    """After a failed acknowledge, the next poll only retries the delete, not the upload."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(ack_raises=ConnectError("timeout"))
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])
        assert "abc123" in forwarder_module._pending_ack

        await route_study(client, source, _STUDY, ["abc123"])
        assert "abc123" in forwarder_module._pending_ack

        source.ack_raises = None
        await route_study(client, source, _STUDY, ["abc123"])

    assert orch_route.call_count == 1
    assert post_route.call_count == 1
    assert source.fetched == ["abc123"]
    assert source.acknowledged == ["abc123"]
    assert forwarder_module._pending_ack == set()


@respx.mock
@pytest.mark.asyncio
async def test_route_study_successful_acknowledge_is_not_tracked(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(
        forwarder_module,
        "CLOUD_NODES",
        {"us-east1": {"base": "http://orthanc-us:8042", "auth": ("orthanc", "orthanc")}},
    )
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    async with AsyncClient() as client:
        await route_study(client, _StubSource(), _STUDY, ["abc123"])

    assert forwarder_module._pending_ack == set()


@respx.mock
@pytest.mark.asyncio
async def test_route_study_one_orchestrator_call_for_whole_study(monkeypatch):
    """Every instance of a study goes to the node from a single orchestrator call."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    instance_ids = ["i1", "i2", "i3"]
    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, instance_ids)

    assert orch_route.call_count == 1
    assert orch_route.calls[0].request.url.params["study_id"] == _STUDY
    assert post_route.call_count == 3
    assert source.acknowledged == instance_ids


@respx.mock
@pytest.mark.asyncio
async def test_route_study_partial_failure_leaves_rest_on_edge(monkeypatch):
    """A failed forward stops the study; unsent instances stay on the edge for the next poll."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(fetch_raises_for={"i2"})
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2", "i3"])
        assert source.acknowledged == ["i1"]
        assert post_route.call_count == 1

        # Next poll: i1 is gone from the edge, i2 and i3 are routed with the same study_id.
        source.fetch_raises_for = set()
        await route_study(client, source, _STUDY, ["i2", "i3"])

    assert source.acknowledged == ["i1", "i2", "i3"]
    assert post_route.call_count == 3
    assert orch_route.call_count == 2
    assert all(c.request.url.params["study_id"] == _STUDY for c in orch_route.calls)


@respx.mock
@pytest.mark.asyncio
async def test_route_study_skips_orchestrator_when_only_pending_acks(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    forwarder_module._pending_ack.add("abc123")

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["abc123"])

    assert not orch_route.called
    assert source.acknowledged == ["abc123"]
    assert forwarder_module._pending_ack == set()


@respx.mock
@pytest.mark.asyncio
async def test_forward_loop_routes_each_study_once(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 3600)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(studies={"s1": ["a", "b"], "s2": ["c"]})
    task = asyncio.create_task(forwarder_module.forward_loop(source))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(c.request.url.params["study_id"] for c in orch_route.calls) == ["s1", "s2"]
    assert source.acknowledged == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_forward_loop_drops_pending_ack_for_instances_no_longer_buffered(monkeypatch):
    """An instance removed from the edge buffer by other means stops being tracked."""
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    forwarder_module._pending_ack.add("gone")

    task = asyncio.create_task(forwarder_module.forward_loop(_StubSource(studies={})))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert forwarder_module._pending_ack == set()


@respx.mock
@pytest.mark.asyncio
async def test_latency_probe_reports_rtt_for_all_nodes(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "ORCH_HEARTBEAT_URL", _ORCH_HB_URL)
    monkeypatch.setattr(forwarder_module, "PROBE_INTERVAL_S", 0)

    for cfg in CLOUD_NODES.values():
        respx.get(f"{cfg['base']}/system").mock(return_value=Response(200, json={}))
    hb_route = respx.post(_ORCH_HB_URL).mock(return_value=Response(204))

    task = asyncio.create_task(forwarder_module.latency_probe_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hb_route.call_count >= 1
    payload = hb_route.calls[0].request
    import json

    body = json.loads(payload.content)
    assert "agent_id" in body
    assert set(body["rtt_dict"].keys()) == set(CLOUD_NODES.keys())


@respx.mock
@pytest.mark.asyncio
async def test_latency_probe_skips_failed_node_and_continues(monkeypatch):
    """One node unreachable → probe loop continues and reports the other nodes."""
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "ORCH_HEARTBEAT_URL", _ORCH_HB_URL)
    monkeypatch.setattr(forwarder_module, "PROBE_INTERVAL_S", 0)

    nodes = dict(CLOUD_NODES)
    node_ids = list(nodes.keys())

    respx.get(f"{nodes[node_ids[0]]['base']}/system").mock(side_effect=ConnectError("refused"))
    for nid in node_ids[1:]:
        respx.get(f"{nodes[nid]['base']}/system").mock(return_value=Response(200, json={}))

    hb_route = respx.post(_ORCH_HB_URL).mock(return_value=Response(204))

    task = asyncio.create_task(forwarder_module.latency_probe_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert hb_route.call_count >= 1
    import json

    body = json.loads(hb_route.calls[0].request.content)
    assert "agent_id" in body
    assert set(body["rtt_dict"].keys()) == set(node_ids[1:])
