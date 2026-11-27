"""Unit tests for DICOM-level upgrades: DIMSE+Storage Commitment, STOW-RS, atomic study commit,
in-flight load tracking, data-residency policies, study location index, baseline scorers,
and dynamic DicomModalities rendering."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient, ConnectError, Response

import src.edge.forwarder as forwarder_module
import src.orchestrator.main as main_module
import src.orchestrator.scorer as scorer_module
from src.edge.destinations import (
    StorageCommitmentError,
    StowRsRejectedError,
    forward_dimse_study,
    forward_stow_rs_instance,
    probe_dimse_echo,
)
from src.edge.transport import DicomSource
from src.orchestrator.baseline_scorers import LeastConnectionsScorer, RoundRobinScorer
from src.orchestrator.main import app
from src.orchestrator.weighted_scorer import WeightedScorer
from src.utils.nodes import load_nodes
from src.utils.render_orthanc_config import inject_dicom_modalities

pytestmark = pytest.mark.unit

_API_KEY = "test-api-key"
_ORCH_URL = "http://orchestrator:8000/get-best-node"
_ORCH_START = "http://orchestrator:8000/transfers/start"
_ORCH_FINISH = "http://orchestrator:8000/transfers/finish"
_ORCH_COMMIT = "http://orchestrator:8000/studies/{study_id}/commit"
_EDGE_BASE = "http://edge-orthanc:8042"


class _MemSource(DicomSource):
    def __init__(self) -> None:
        self.acknowledged: list[str] = []

    async def poll_new(self, client: AsyncClient) -> dict[str, list[str]]:
        return {}

    @asynccontextmanager
    async def open_stream(
        self, client: AsyncClient, instance_id: str
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        async def _body() -> AsyncIterator[bytes]:
            yield f"DICM_{instance_id}".encode()

        yield _body()

    async def acknowledge(self, client: AsyncClient, instance_id: str) -> None:
        self.acknowledged.append(instance_id)


@pytest.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def client(fake_redis, monkeypatch):
    monkeypatch.setattr(main_module, "API_KEY", _API_KEY)
    monkeypatch.setattr(main_module, "_redis", fake_redis)
    monkeypatch.setattr(main_module, "AGENT_POLICIES", {})
    monkeypatch.setattr(scorer_module, "_SCORER_INSTANCE", None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _API_KEY},
    ) as c:
        yield c


# --- 1. Optional Node Registry Fields & Dynamic DicomModalities ---


def test_load_nodes_parses_optional_jurisdiction_and_dicom_fields(monkeypatch):
    monkeypatch.setenv("NODE_US_USER", "u")
    monkeypatch.setenv("NODE_US_PASS", "p")
    monkeypatch.setenv(
        "CLOUD_NODES",
        json.dumps(
            [
                {
                    "name": "us-east1",
                    "base": "https://orthanc-us:8042",
                    "ae_title": "Orthanc_US",
                    "auth_env": "NODE_US",
                    "jurisdiction": "US",
                    "dicom_host": "orthanc-us",
                    "dicom_port": 4242,
                }
            ]
        ),
    )
    cfg = load_nodes()["us-east1"]
    assert cfg["jurisdiction"] == "US"
    assert cfg["dicom_host"] == "orthanc-us"
    assert cfg["dicom_port"] == 4242


def test_inject_dicom_modalities_adds_tls_and_storage_commitment():
    rendered = json.dumps({"Name": "Orthanc_Edge"})
    nodes_json = json.dumps(
        [
            {
                "name": "us-east1",
                "base": "https://orthanc-us:8042",
                "ae_title": "Orthanc_US",
                "auth_env": "NODE_US",
                "dicom_port": 4242,
            }
        ]
    )
    out = json.loads(inject_dicom_modalities(rendered, nodes_json))
    mod = out["DicomModalities"]["us-east1"]
    assert mod == {
        "AET": "Orthanc_US",
        "Host": "orthanc-us",
        "Port": 4242,
        "UseDicomTls": True,
        "AllowStorageCommitment": True,
    }


# --- 2. In-Flight Ingestion Load & Baseline Scorers ---


def test_weighted_scorer_combines_jobs_and_inflight_uploads():
    scorer = WeightedScorer(w_queue=0.5, w_disk=0.15, w_rtt=0.35)
    idle = {"queue_size": 0, "inflight_uploads": 0, "disk_free_mb": 5000, "disk_total_mb": 10000}
    busy = {"queue_size": 0, "inflight_uploads": 8, "disk_free_mb": 5000, "disk_total_mb": 10000}
    assert scorer.score(idle) > scorer.score(busy)


def test_baseline_scorers_round_robin_and_least_connections():
    rr = RoundRobinScorer()
    n1 = {"node_id": "us-east1", "queue_size": 0, "inflight_uploads": 0}
    n2 = {"node_id": "eu-west1", "queue_size": 0, "inflight_uploads": 0}
    first = max([n1, n2], key=rr.score)["node_id"]
    rr.record_selection(first)
    second = max([n1, n2], key=rr.score)["node_id"]
    assert first != second

    lc = LeastConnectionsScorer()
    assert lc.score({"queue_size": 0, "inflight_uploads": 1}) > lc.score(
        {"queue_size": 0, "inflight_uploads": 5}
    )


async def test_inflight_transfer_leases_affect_routing_decision(client, fake_redis):
    us = {
        "node_id": "us-east1",
        "ae_title": "Orthanc_US",
        "base_url": "https://orthanc-us:8042",
        "queue_size": 0,
        "disk_free_mb": 9000.0,
        "disk_total_mb": 10000,
        "healthy": True,
        "ts": "2026-11-27T00:00:00+00:00",
    }
    eu = {**us, "node_id": "eu-west1", "ae_title": "Orthanc_EU"}
    await fake_redis.set("node:us-east1", json.dumps(us))
    await fake_redis.set("node:eu-west1", json.dumps(eu))
    await fake_redis.set("rtt:test-agent", json.dumps({"us-east1": 50.0, "eu-west1": 50.0}))

    # Register 2 active uploads on us-east1 -> eu-west1 should now score higher.
    for sid in ("s1", "s2"):
        r = await client.post(
            "/transfers/start",
            json={"agent_id": "test-agent", "node_id": "us-east1", "study_id": sid},
        )
        assert r.status_code == 204

    best = (await client.get("/get-best-node", params={"agent_id": "test-agent"})).json()
    assert best["node_id"] == "eu-west1"

    # Finish both transfers on us-east1 -> inflight returns to 0.
    for sid in ("s1", "s2"):
        r = await client.post(
            "/transfers/finish",
            json={"agent_id": "test-agent", "node_id": "us-east1", "study_id": sid},
        )
        assert r.status_code == 204

    nodes = {n["node_id"]: n for n in (await client.get("/nodes")).json()}
    assert nodes["us-east1"]["inflight_uploads"] == 0


# --- 3. Data-Residency Policy & Study Location Index ---


async def test_data_residency_policy_restricts_routing_by_jurisdiction(client, fake_redis):
    us = {
        "node_id": "us-east1",
        "ae_title": "Orthanc_US",
        "base_url": "https://orthanc-us:8042",
        "queue_size": 0,
        "jurisdiction": "US",
        "healthy": True,
        "ts": "2026-11-27T00:00:00+00:00",
    }
    eu = {
        **us,
        "node_id": "eu-west1",
        "ae_title": "Orthanc_EU",
        "queue_size": 50,
        "jurisdiction": "EU",
    }
    await fake_redis.set("node:us-east1", json.dumps(us))
    await fake_redis.set("node:eu-west1", json.dumps(eu))

    resp = await client.get(
        "/get-best-node",
        params={"agent_id": "eu-hospital", "jurisdiction": "EU"},
    )
    assert resp.status_code == 200
    assert resp.json()["node_id"] == "eu-west1"

    # Disallowed jurisdiction with no matching healthy node returns 503 (never leaks PHI).
    resp_none = await client.get(
        "/get-best-node",
        params={"agent_id": "eu-hospital", "jurisdiction": "CA"},
    )
    assert resp_none.status_code == 503


async def test_study_location_index_and_commit_endpoint(client, fake_redis):
    r = await client.post(
        "/studies/study-42/commit",
        json={
            "agent_id": "agent-001",
            "node_id": "us-east1",
            "transport": "dimse",
            "storage_committed": True,
            "instance_count": 120,
        },
    )
    assert r.status_code == 200
    loc = (await client.get("/studies/study-42/location")).json()
    assert loc["node_id"] == "us-east1"
    assert loc["transport"] == "dimse"
    assert loc["storage_committed"] is True
    assert loc["instance_count"] == 120

    all_studies = (await client.get("/studies")).json()
    assert any(s["study_id"] == "study-42" for s in all_studies)


async def test_dicom_rtt_heartbeat_preferred_in_auto_mode(client, fake_redis):
    us = {
        "node_id": "us-east1",
        "ae_title": "Orthanc_US",
        "base_url": "https://orthanc-us:8042",
        "queue_size": 0,
        "healthy": True,
        "ts": "2026-11-27T00:00:00+00:00",
    }
    eu = {**us, "node_id": "eu-west1", "ae_title": "Orthanc_EU"}
    await fake_redis.set("node:us-east1", json.dumps(us))
    await fake_redis.set("node:eu-west1", json.dumps(eu))

    hb = await client.post(
        "/heartbeat",
        json={
            "agent_id": "agent-001",
            "rtt_dict": {"us-east1": 50.0, "eu-west1": 50.0},
            "dicom_rtt_dict": {"us-east1": 400.0, "eu-west1": 20.0},
            "throughput_mbps_dict": {"us-east1": 5.0, "eu-west1": 45.0},
            "probe_mode": "hybrid",
        },
    )
    assert hb.status_code == 204
    best = (await client.get("/get-best-node", params={"agent_id": "agent-001"})).json()
    assert best["node_id"] == "eu-west1"
    assert best["dicom_rtt_ms"] == 20.0
    assert best["throughput_mbps"] == 45.0


# --- 4. Edge Transports (STOW-RS, DIMSE + Storage Commitment, Atomic Study Commit) ---


@respx.mock
@pytest.mark.asyncio
async def test_stow_rs_transport_sends_multipart_and_detects_failures():
    node_cfg = {"base": "http://orthanc-us:8042", "ae_title": "Orthanc_US", "auth": ("u", "p")}
    route = respx.post("http://orthanc-us:8042/dicom-web/studies").mock(
        return_value=Response(200, json={"00081199": {"Value": [{}]}})
    )
    async with AsyncClient() as c:
        nbytes = await forward_stow_rs_instance(c, _MemSource(), "inst1", node_cfg)
    assert nbytes == len(b"DICM_inst1")
    assert (
        'multipart/related; type="application/dicom"'
        in route.calls[0].request.headers["content-type"]
    )

    respx.post("http://orthanc-us:8042/dicom-web/studies").mock(
        return_value=Response(200, json={"00081198": {"Value": [{"reason": "failed"}]}})
    )
    async with AsyncClient() as c:
        with pytest.raises(StowRsRejectedError):
            await forward_stow_rs_instance(c, _MemSource(), "inst1", node_cfg)


@respx.mock
@pytest.mark.asyncio
async def test_dimse_store_and_storage_commitment_succeeds_and_rejects_failures():
    respx.post(f"{_EDGE_BASE}/modalities/us-east1/store").mock(return_value=Response(200, json={}))
    respx.post(f"{_EDGE_BASE}/modalities/us-east1/storage-commitment").mock(
        return_value=Response(200, json={"ID": "tx-123"})
    )
    respx.get(f"{_EDGE_BASE}/storage-commitment/tx-123").mock(
        side_effect=[
            Response(200, json={"Status": "Pending"}),
            Response(200, json={"Status": "Success", "Success": [{"SOPInstanceUID": "1.2"}]}),
        ]
    )
    async with AsyncClient() as c:
        committed = await forward_dimse_study(
            c, _EDGE_BASE, ("u", "p"), "us-east1", ["i1", "i2"], sc_poll_interval_s=0.01
        )
    assert committed is True

    respx.get(f"{_EDGE_BASE}/storage-commitment/tx-123").mock(
        return_value=Response(200, json={"Status": "Failure"})
    )
    async with AsyncClient() as c:
        with pytest.raises(StorageCommitmentError):
            await forward_dimse_study(
                c, _EDGE_BASE, ("u", "p"), "us-east1", ["i1"], sc_poll_interval_s=0.01
            )


@respx.mock
@pytest.mark.asyncio
async def test_probe_dimse_echo_returns_positive_rtt():
    respx.post(f"{_EDGE_BASE}/modalities/us-east1/echo").mock(return_value=Response(200, json={}))
    async with AsyncClient() as c:
        rtt = await probe_dimse_echo(c, _EDGE_BASE, ("u", "p"), "us-east1")
    assert rtt >= 0.0


@respx.mock
@pytest.mark.asyncio
async def test_atomic_study_commit_replays_full_study_on_mid_transfer_failover(monkeypatch):
    """When STUDY_ATOMIC_COMMIT is true and us-east1 fails on slice 2 of 3, eu-west1 receives
    all 3 slices (zero split study) and edge copies are deleted only after all 3 succeed."""
    nodes = {
        "us-east1": {"base": "http://orthanc-us:8042", "ae_title": "US", "auth": ("u", "p")},
        "eu-west1": {"base": "http://orthanc-eu:8042", "ae_title": "EU", "auth": ("u", "p")},
    }
    monkeypatch.setattr(forwarder_module, "ORCH_URL", _ORCH_URL)
    monkeypatch.setattr(forwarder_module, "CLOUD_NODES", nodes)
    monkeypatch.setattr(forwarder_module, "FORWARDER_TRANSPORT", "rest")
    monkeypatch.setattr(forwarder_module, "STUDY_ATOMIC_COMMIT", True)
    monkeypatch.setattr(forwarder_module, "REPORT_INFLIGHT", False)

    def _pick(req: httpx.Request) -> Response:
        excl = req.url.params.get_list("exclude")
        nid = "eu-west1" if "us-east1" in excl else "us-east1"
        return Response(200, json={"node_id": nid, "score": 0.8})

    respx.get(_ORCH_URL).mock(side_effect=_pick)
    us_post = respx.post("http://orthanc-us:8042/instances").mock(
        side_effect=[Response(200, json={"ID": "ok"}), ConnectError("node died mid-study")]
    )
    eu_post = respx.post("http://orthanc-eu:8042/instances").mock(
        return_value=Response(200, json={"ID": "ok"})
    )

    source = _MemSource()
    async with AsyncClient() as c:
        await forwarder_module.route_study(c, source, "study-atomic", ["i1", "i2", "i3"])

    assert us_post.call_count == 2
    assert eu_post.call_count == 3  # Replayed i1, i2, i3 completely -> no split study
    assert source.acknowledged == ["i1", "i2", "i3"]
