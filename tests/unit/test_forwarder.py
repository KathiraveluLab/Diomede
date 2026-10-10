from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import respx
from httpx import AsyncClient, ConnectError, HTTPStatusError, Response

import src.edge.forwarder as forwarder_module
import src.edge.orthanc_source as orthanc_source_module
from src.edge.forwarder import CLOUD_NODES, route_instance
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

_INSTANCES_ONE = ["abc123"]
_INSTANCES_MULTIPLE = ["abc123", "def456"]
_INSTANCES_EMPTY: list[str] = []


@pytest.fixture(autouse=True)
def _reset_pending_ack(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_pending_ack", set())


class _StubSource(DicomSource):
    """Controllable DicomSource for forwarder unit tests."""

    def __init__(
        self,
        instance_ids: list[str] | None = None,
        dcm_bytes: bytes = _DCM_BYTES,
        fetch_raises: Exception | None = None,
        ack_raises: Exception | None = None,
    ) -> None:
        self.instance_ids = instance_ids or []
        self.dcm_bytes = dcm_bytes
        self.fetch_raises = fetch_raises
        self.ack_raises = ack_raises
        self.fetched: list[str] = []
        self.acknowledged: list[str] = []

    async def poll_new(self, client: AsyncClient) -> list[str]:
        return self.instance_ids

    @asynccontextmanager
    async def open_stream(
        self, client: AsyncClient, instance_id: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        if self.fetch_raises:
            raise self.fetch_raises
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
async def test_poll_new_returns_new_instance_ids():
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(200, json=_INSTANCES_ONE))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        ids = await source.poll_new(client)
    assert ids == ["abc123"]


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_returns_multiple_instance_ids():
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(200, json=_INSTANCES_MULTIPLE))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        ids = await source.poll_new(client)
    assert ids == ["abc123", "def456"]


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_returns_empty_list_when_no_instances():
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(200, json=_INSTANCES_EMPTY))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        ids = await source.poll_new(client)
    assert ids == []


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_raises_on_http_error():
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(500))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        with pytest.raises(HTTPStatusError):
            await source.poll_new(client)


@pytest.fixture
def edge_logs(monkeypatch, caplog):
    monkeypatch.setattr(orthanc_source_module.log, "propagate", True)
    return caplog


def _mock_edge_storage(mode: str = "Reject", quota_mb: int = 10_000, used_mb: float = 0):
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(200, json=_INSTANCES_ONE))
    respx.get(f"{_EDGE_BASE}/system").mock(
        return_value=Response(
            200, json={"MaximumStorageSize": quota_mb, "MaximumStorageMode": mode}
        )
    )
    return respx.get(f"{_EDGE_BASE}/statistics").mock(
        return_value=Response(200, json={"TotalDiskSizeMB": used_mb})
    )


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_warns_once_when_edge_recycles(edge_logs):
    _mock_edge_storage(mode="Recycle")
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        await source.poll_new(client)
        await source.poll_new(client)

    warnings = [r for r in edge_logs.records if "MaximumStorageMode" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_does_not_warn_when_edge_rejects(edge_logs):
    _mock_edge_storage(mode="Reject")
    async with AsyncClient() as client:
        await OrthancSource(base=_EDGE_BASE).poll_new(client)

    assert "MaximumStorageMode" not in edge_logs.text


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_logs_only_when_storage_level_changes(edge_logs):
    stats = _mock_edge_storage(quota_mb=10_000)
    stats.side_effect = [
        Response(200, json={"TotalDiskSizeMB": used}) for used in (8500, 8600, 9600, 9900, 1000)
    ]
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        for _ in range(5):
            await source.poll_new(client)

    logged = [
        (r.levelname, r.getMessage()) for r in edge_logs.records if "storage" in r.getMessage()
    ]
    assert [level for level, _ in logged] == ["WARNING", "ERROR", "INFO"]
    assert "85%" in logged[0][1]
    assert "96%" in logged[1][1]


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_skips_storage_check_without_quota():
    stats = _mock_edge_storage(quota_mb=0)
    async with AsyncClient() as client:
        await OrthancSource(base=_EDGE_BASE).poll_new(client)

    assert not stats.called


@respx.mock
@pytest.mark.asyncio
async def test_storage_check_failure_does_not_break_polling():
    respx.get(f"{_EDGE_BASE}/instances").mock(return_value=Response(200, json=_INSTANCES_ONE))
    respx.get(f"{_EDGE_BASE}/system").mock(return_value=Response(500))
    async with AsyncClient() as client:
        ids = await OrthancSource(base=_EDGE_BASE).poll_new(client)

    assert ids == _INSTANCES_ONE


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
async def test_route_instance_happy_path(monkeypatch):
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
        await route_instance(client, source, "abc123")

    assert source.fetched == ["abc123"]
    assert source.acknowledged == ["abc123"]
    assert post_route.called
    assert post_route.calls[0].request.content == _DCM_BYTES


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_sets_dicom_content_type(monkeypatch):
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
        await route_instance(client, _StubSource(), "abc123")

    assert post_route.calls[0].request.headers["content-type"] == "application/dicom"


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_stream_failure_skips_forward(monkeypatch):
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
        await route_instance(client, source, "abc123")

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_orchestrator_failure_aborts_early(monkeypatch):
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
        await route_instance(client, source, "abc123")

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_unknown_node_id_aborts_early(monkeypatch):
    """Orchestrator returns a node_id not in CLOUD_NODES → no POST, no acknowledge."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", {})

    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(200))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_instance(client, source, "abc123")

    assert not post_route.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_cloud_post_failure_skips_acknowledge(monkeypatch):
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
        await route_instance(client, source, "abc123")

    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_acknowledge_failure_does_not_raise(monkeypatch):
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
        await route_instance(client, source, "abc123")


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_retries_failed_acknowledge_without_reforwarding(monkeypatch):
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
        await route_instance(client, source, "abc123")
        assert "abc123" in forwarder_module._pending_ack

        await route_instance(client, source, "abc123")
        assert "abc123" in forwarder_module._pending_ack

        source.ack_raises = None
        await route_instance(client, source, "abc123")

    assert orch_route.call_count == 1
    assert post_route.call_count == 1
    assert source.fetched == ["abc123"]
    assert source.acknowledged == ["abc123"]
    assert forwarder_module._pending_ack == set()


@respx.mock
@pytest.mark.asyncio
async def test_route_instance_successful_acknowledge_is_not_tracked(monkeypatch):
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
        await route_instance(client, _StubSource(), "abc123")

    assert forwarder_module._pending_ack == set()


@pytest.mark.asyncio
async def test_forward_loop_drops_pending_ack_for_instances_no_longer_buffered(monkeypatch):
    """An instance removed from the edge buffer by other means stops being tracked."""
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    forwarder_module._pending_ack.add("gone")

    task = asyncio.create_task(forwarder_module.forward_loop(_StubSource(instance_ids=[])))
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
