"""Unit tests for scorer, weighted_scorer, and the /get-best-node endpoint."""

import json

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import src.orchestrator.main as main_module
import src.orchestrator.weighted_scorer  # noqa: F401 — triggers self-registration
from src.orchestrator.main import NodeResponse, app
from src.orchestrator.scorer import get_scorer
from src.orchestrator.weighted_scorer import WeightedScorer

pytestmark = pytest.mark.unit

# WeightedScorer
_NODE = {
    "queue_size": 0,
    "disk_free_mb": 5000.0,
    "disk_total_mb": 10_000,
    "rtt_ms": 99,
}


def test_score_formula():
    assert WeightedScorer().score(_NODE) == pytest.approx(0.5785)


def test_score_prefers_low_queue():
    scorer = WeightedScorer()
    assert scorer.score({**_NODE, "queue_size": 0}) > scorer.score({**_NODE, "queue_size": 20})


def test_score_prefers_low_rtt():
    scorer = WeightedScorer()
    assert scorer.score({**_NODE, "rtt_ms": 10}) > scorer.score({**_NODE, "rtt_ms": 500})


def test_score_missing_keys_returns_float():
    score = WeightedScorer().score({})
    assert isinstance(score, float) and score > 0


# Scorer registry
def test_get_scorer_default_is_weighted(monkeypatch):
    monkeypatch.delenv("SCORER", raising=False)
    assert isinstance(get_scorer(), WeightedScorer)


def test_get_scorer_unknown_raises(monkeypatch):
    import src.orchestrator.scorer as scorer_module

    monkeypatch.setattr(
        scorer_module, "_SCORER_INSTANCE", None
    )  # reset singleton for test isolation
    monkeypatch.setenv("SCORER", "nonexistent")
    with pytest.raises(ValueError, match="nonexistent"):
        get_scorer()


# /get-best-node endpoint
_HEALTHY_NODE = {
    "node_id": "us-east1",
    "ae_title": "Orthanc_US",
    "base_url": "https://orthanc-us:8042",
    "queue_size": 1,
    "disk_free_mb": 5000.0,
    "disk_total_mb": 10_000,
    "instance_count": 5,
    "healthy": True,
    "ts": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.close()


@pytest.fixture
async def client(fake_redis, monkeypatch):
    monkeypatch.setattr(main_module, "_redis", fake_redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_no_nodes_returns_503(client):
    assert (await client.get("/get-best-node")).status_code == 503


async def test_all_unhealthy_returns_503(client, fake_redis):
    await fake_redis.set("node:us-east1", json.dumps({**_HEALTHY_NODE, "healthy": False}))
    assert (await client.get("/get-best-node")).status_code == 503


async def test_returns_best_healthy_node(client, fake_redis):
    best = {**_HEALTHY_NODE, "queue_size": 0}
    worse = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU", "queue_size": 20}
    await fake_redis.set("node:us-east1", json.dumps(best))
    await fake_redis.set("node:eu-west1", json.dumps(worse))

    resp = await client.get("/get-best-node")
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "us-east1"
    assert data["healthy"] is True


async def test_response_matches_node_response_schema(client, fake_redis):
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    resp = await client.get("/get-best-node")
    NodeResponse.model_validate(resp.json())


# /nodes endpoint
async def test_nodes_empty_list_when_no_redis_data(client):
    """NODES keys are registered but no telemetry written → empty list, not an error."""
    resp = await client.get("/nodes")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_nodes_returns_all_seeded_nodes(client, fake_redis):
    node_b = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU"}
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    await fake_redis.set("node:eu-west1", json.dumps(node_b))

    resp = await client.get("/nodes")
    assert resp.status_code == 200
    ids = {n["node_id"] for n in resp.json()}
    assert "us-east1" in ids
    assert "eu-west1" in ids


async def test_nodes_includes_unhealthy_nodes(client, fake_redis):
    """Unlike /get-best-node, /nodes returns every node regardless of health."""
    await fake_redis.set("node:us-east1", json.dumps({**_HEALTHY_NODE, "healthy": False}))

    resp = await client.get("/nodes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["healthy"] is False


async def test_nodes_skips_keys_absent_from_redis(client, fake_redis):
    """Nodes with no telemetry entry in Redis are omitted from the response."""
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    # eu-west1, asia-northeast1, af-south1 intentionally not seeded

    resp = await client.get("/nodes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["node_id"] == "us-east1"


async def test_nodes_each_item_matches_schema(client, fake_redis):
    node_b = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU"}
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    await fake_redis.set("node:eu-west1", json.dumps(node_b))

    resp = await client.get("/nodes")
    assert resp.status_code == 200
    for item in resp.json():
        NodeResponse.model_validate(item)


async def test_nodes_returns_503_when_redis_uninitialized(monkeypatch):
    monkeypatch.setattr(main_module, "_redis", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/nodes")
    assert resp.status_code == 503
