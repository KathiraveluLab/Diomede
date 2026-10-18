from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from httpx import AsyncClient, ConnectError, HTTPStatusError, ReadTimeout, Response

import src.edge.forwarder as forwarder_module
import src.edge.orthanc_source as orthanc_source_module
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
    monkeypatch.setattr(forwarder_module, "_rejected", {})
    monkeypatch.setattr(forwarder_module, "_node_slots", {})


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


def _series(study_id: str, *instance_ids: str) -> dict:
    return {
        "ID": f"series-{instance_ids[0]}",
        "ParentStudy": study_id,
        "Instances": list(instance_ids),
    }


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_groups_instances_by_study_in_one_request():
    route = respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(
            200,
            json=[_series("s1", "abc123"), _series("s1", "def456"), _series("s2", "ghi789")],
        )
    )
    source = OrthancSource(base=_EDGE_BASE, wait_for_stable_study=False)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s1": ["abc123", "def456"], "s2": ["ghi789"]}
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_returns_empty_dict_when_buffer_empty():
    respx.get(f"{_EDGE_BASE}/series?expand").mock(return_value=Response(200, json=[]))
    studies_route = respx.get(f"{_EDGE_BASE}/studies?expand")
    source = OrthancSource(base=_EDGE_BASE, wait_for_stable_study=True)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {}
    assert not studies_route.called


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_skips_series_without_instances():
    empty = {"ID": "series-x", "ParentStudy": "s9", "Instances": []}
    respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(200, json=[empty, _series("s1", "abc123")])
    )
    source = OrthancSource(base=_EDGE_BASE, wait_for_stable_study=False)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s1": ["abc123"]}


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_waits_for_stable_study_when_enabled():
    respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(200, json=[_series("s1", "abc123"), _series("s2", "def456")])
    )
    respx.get(f"{_EDGE_BASE}/studies?expand").mock(
        return_value=Response(
            200, json=[{"ID": "s1", "IsStable": True}, {"ID": "s2", "IsStable": False}]
        )
    )
    source = OrthancSource(base=_EDGE_BASE, wait_for_stable_study=True)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s1": ["abc123"]}


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_ignores_stability_when_disabled():
    respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(200, json=[_series("s2", "def456")])
    )
    studies_route = respx.get(f"{_EDGE_BASE}/studies?expand")
    source = OrthancSource(base=_EDGE_BASE, wait_for_stable_study=False)
    async with AsyncClient() as client:
        studies = await source.poll_new(client)
    assert studies == {"s2": ["def456"]}
    assert not studies_route.called


@respx.mock
@pytest.mark.asyncio
async def test_poll_new_raises_on_http_error():
    respx.get(f"{_EDGE_BASE}/series?expand").mock(return_value=Response(500))
    source = OrthancSource(base=_EDGE_BASE)
    async with AsyncClient() as client:
        with pytest.raises(HTTPStatusError):
            await source.poll_new(client)


@pytest.fixture
def edge_logs(monkeypatch, caplog):
    monkeypatch.setattr(orthanc_source_module.log, "propagate", True)
    return caplog


def _mock_edge_storage(mode: str = "Reject", quota_mb: int = 10_000, used_mb: float = 0):
    respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(200, json=[_series("s1", "abc123")])
    )
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
    respx.get(f"{_EDGE_BASE}/series?expand").mock(
        return_value=Response(200, json=[_series("s1", "abc123")])
    )
    respx.get(f"{_EDGE_BASE}/system").mock(return_value=Response(500))
    async with AsyncClient() as client:
        studies = await OrthancSource(base=_EDGE_BASE).poll_new(client)

    assert studies == {"s1": ["abc123"]}


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


class _IdAsBytesSource(_StubSource):
    """Sends each instance's ID as its file, so a test can tell uploads apart."""

    @asynccontextmanager
    async def open_stream(
        self, client: AsyncClient, instance_id: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        self.fetched.append(instance_id)

        async def _body() -> AsyncIterator[bytes]:
            yield instance_id.encode()

        yield _body()


def _reject_bad(request):
    if request.content == b"bad":
        return Response(400, json={"Message": "Bad file format"})
    return Response(200, json={"ID": "new-id"})


@respx.mock
@pytest.mark.asyncio
async def test_route_study_skips_a_rejected_instance_and_forwards_the_rest(monkeypatch):
    """One file the node can never accept must not hold back the rest of its study."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    orch_route = respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(side_effect=_reject_bad)

    source = _IdAsBytesSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["bad", "i2", "i3"])
        assert source.acknowledged == ["i2", "i3"]

        # Next poll: only the rejected instance is still on the edge, and it is not retried yet.
        await route_study(client, source, _STUDY, ["bad"])

    assert source.fetched == ["bad", "i2", "i3"]
    assert post_route.call_count == 3
    assert orch_route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_rejected_instance_is_retried_after_the_wait(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    monkeypatch.setattr(forwarder_module, "_REJECTED_RETRY_S", 0)
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=_reject_bad)

    source = _IdAsBytesSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["bad"])
        await route_study(client, source, _STUDY, ["bad"])

    assert source.fetched == ["bad", "bad"]


@respx.mock
@pytest.mark.asyncio
async def test_node_wide_client_error_still_stops_the_study(monkeypatch):
    """A 401 means the node or its credentials are wrong, not the file, so nothing is skipped."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    respx.get(_ORCH_URL).mock(return_value=Response(200, json=_BEST_NODE_RESP))
    post_route = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(401))

    source = _IdAsBytesSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2"])
        await route_study(client, source, _STUDY, ["i1", "i2"])

    assert source.fetched == ["i1", "i1"]
    assert post_route.call_count == 2
    assert source.acknowledged == []


_US_EU = {
    **_US_ONLY,
    "eu-west1": {"base": "http://orthanc-eu:8042", "auth": ("orthanc", "orthanc")},
}


def _orchestrator_skipping_excluded(request):
    excluded = request.url.params.get_list("exclude")
    for node_id in ("us-east1", "eu-west1"):
        if node_id not in excluded:
            return Response(200, json={"node_id": node_id, "score": 0.5})
    return Response(503, json={"detail": "No healthy nodes available"})


@respx.mock
@pytest.mark.asyncio
async def test_upload_refused_by_node_moves_study_to_next_node(monkeypatch):
    """A node that answers telemetry but refuses uploads must not keep the study forever."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    orch_route = respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    us_post = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(405))
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2"])

    assert source.acknowledged == ["i1", "i2"]
    assert us_post.call_count == 1
    assert eu_post.call_count == 2
    assert orch_route.calls[1].request.url.params.get_list("exclude") == ["us-east1"]


@respx.mock
@pytest.mark.asyncio
async def test_edge_read_failure_does_not_fail_over(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    orch_route = respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)

    edge_read = httpx.Request("GET", "http://edge-orthanc:8042/instances/i1/file")
    source = _StubSource(
        fetch_raises=HTTPStatusError("404", request=edge_read, response=Response(404))
    )
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2"])

    assert orch_route.call_count == 1
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_failover_stops_when_no_node_is_left(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    orch_route = respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    us_post = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(503))
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(return_value=Response(503))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2"])

    assert us_post.call_count == 1
    assert eu_post.call_count == 1
    assert orch_route.call_count == 3
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
async def test_no_failover_when_the_upload_may_have_landed(monkeypatch):
    """A reply that times out after sending may mean the node stored it; retry it there later."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    orch_route = respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=ReadTimeout("no reply"))
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1"])

    assert orch_route.call_count == 1
    assert not eu_post.called
    assert source.acknowledged == []


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_no_failover_on_gateway_errors(monkeypatch, status):
    """A proxy in front of the node can time out while the node is still storing the file."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    orch_route = respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(status))
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(return_value=Response(200))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1"])

    assert orch_route.call_count == 1
    assert not eu_post.called


@respx.mock
@pytest.mark.asyncio
async def test_unreachable_node_fails_over(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=ConnectError("refused"))
    respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1"])

    assert source.acknowledged == ["i1"]


@respx.mock
@pytest.mark.asyncio
async def test_failover_part_way_through_a_study_keeps_earlier_uploads(monkeypatch):
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_skipping_excluded)
    us_post = respx.post("http://orthanc-us:8042/instances").mock(
        side_effect=[Response(200, json={"ID": "new-id"}), Response(405)]
    )
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2", "i3"])

    assert source.acknowledged == ["i1", "i2", "i3"]
    assert us_post.call_count == 2
    assert eu_post.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_failover_gives_up_if_orchestrator_ignores_exclude(monkeypatch):
    """An older orchestrator that ignores exclude must not send the forwarder into a loop."""
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    answers = [Response(200, json=_BEST_NODE_RESP)] * 5
    orch_route = respx.get(_ORCH_URL).mock(side_effect=answers)
    us_post = respx.post("http://orthanc-us:8042/instances").mock(return_value=Response(503))

    source = _StubSource()
    async with AsyncClient() as client:
        await route_study(client, source, _STUDY, ["i1", "i2"])

    assert us_post.call_count == 1
    assert orch_route.call_count == 2
    assert source.acknowledged == []


@pytest.mark.asyncio
async def test_forward_loop_forgets_rejected_instances_no_longer_buffered(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    forwarder_module._rejected["gone"] = 0.0

    task = asyncio.create_task(forwarder_module.forward_loop(_StubSource(studies={})))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert forwarder_module._rejected == {}


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
async def test_latency_probe_reports_failed_node_as_slow(monkeypatch):
    """One node unreachable → probe loop continues and reports it at the probe timeout."""
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
    assert set(body["rtt_dict"].keys()) == set(node_ids)
    assert body["rtt_dict"][node_ids[0]] == forwarder_module._UNREACHABLE_RTT_MS
    assert all(body["rtt_dict"][nid] < forwarder_module._UNREACHABLE_RTT_MS for nid in node_ids[1:])


def _orchestrator_by_study(request):
    node_id = "us-east1" if request.url.params["study_id"].startswith("slow") else "eu-west1"
    return Response(200, json={"node_id": node_id, "score": 0.5})


def _hanging_node(started: list[str]):
    async def _hang(request):
        started.append(request.url.host)
        await asyncio.Event().wait()

    return _hang


async def _run_one_poll(source: DicomSource) -> None:
    task = asyncio.create_task(forwarder_module.forward_loop(source))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@respx.mock
@pytest.mark.asyncio
async def test_hung_node_does_not_hold_up_studies_for_other_nodes(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 3600)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_by_study)

    respx.post("http://orthanc-us:8042/instances").mock(side_effect=_hanging_node([]))
    respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(studies={"slow-1": ["a"], "fast-1": ["b"], "fast-2": ["c"]})
    await _run_one_poll(source)

    assert source.acknowledged == ["b", "c"]


@respx.mock
@pytest.mark.asyncio
async def test_uploads_to_a_hung_node_are_capped_without_blocking_other_nodes(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 3600)
    monkeypatch.setattr(forwarder_module, "MAX_UPLOADS_PER_NODE", 2)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_by_study)
    started: list[str] = []
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=_hanging_node(started))
    respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    studies = {"slow-1": ["a"], "slow-2": ["b"], "slow-3": ["c"], "fast-1": ["d"]}
    source = _StubSource(studies=studies)
    await _run_one_poll(source)

    assert len(started) == 2
    assert source.acknowledged == ["d"]


@respx.mock
@pytest.mark.asyncio
async def test_study_still_uploading_is_not_started_again(monkeypatch):
    """A second poll would see the in-flight instance again and upload it twice."""
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_by_study)

    started: list[str] = []
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=_hanging_node(started))

    await _run_one_poll(_StubSource(studies={"slow-1": ["a"]}))

    assert len(started) == 1


@pytest.mark.asyncio
async def test_error_in_one_study_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 3600)
    routed: list[str] = []

    async def _route(client, source, study_id, instance_ids):
        if study_id == "bad":
            raise RuntimeError("boom")
        routed.append(study_id)

    monkeypatch.setattr(forwarder_module, "route_study", _route)

    await _run_one_poll(_StubSource(studies={"bad": ["a"], "good": ["b"]}))

    assert routed == ["good"]


@respx.mock
@pytest.mark.asyncio
async def test_study_arriving_during_a_hung_upload_is_not_held_back(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_EU)
    respx.get(_ORCH_URL).mock(side_effect=_orchestrator_by_study)
    respx.post("http://orthanc-us:8042/instances").mock(side_effect=_hanging_node([]))
    respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(studies={"slow-1": ["a"]})
    task = asyncio.create_task(forwarder_module.forward_loop(source))
    await asyncio.sleep(0.02)
    source.studies = {"slow-1": ["a"], "fast-1": ["b"]}
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The stub never drops acknowledged instances, so later polls route "b" again.
    assert set(source.acknowledged) == {"b"}


@respx.mock
@pytest.mark.asyncio
async def test_study_is_retried_on_a_later_poll_after_a_failure(monkeypatch):
    monkeypatch.setattr(forwarder_module, "_VERIFY", True)
    monkeypatch.setattr(forwarder_module, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", _US_ONLY)
    respx.get(_ORCH_URL).mock(
        side_effect=[Response(503)] + [Response(200, json=_BEST_NODE_RESP)] * 50
    )
    respx.post("http://orthanc-us:8042/instances").mock(
        return_value=Response(200, json={"ID": "new-id"})
    )

    source = _StubSource(studies={"s1": ["a"]})
    await _run_one_poll(source)

    assert "a" in source.acknowledged
