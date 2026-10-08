"""Unit tests for scorer, weighted_scorer, and the /get-best-node endpoint."""

import json
import os

os.environ.setdefault("ORCHESTRATOR_API_KEY", "test-api-key")

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import src.orchestrator.main as main_module
import src.orchestrator.weighted_scorer  # noqa: F401 — triggers self-registration
from src.orchestrator.main import NodeResponse, app
from src.orchestrator.scorer import get_scorer
from src.orchestrator.weighted_scorer import WeightedScorer

pytestmark = pytest.mark.unit

_TEST_API_KEY = "test-api-key"


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch):
    monkeypatch.setattr(main_module, "API_KEY", _TEST_API_KEY)


# WeightedScorer
_NODE = {
    "queue_size": 0,
    "disk_free_mb": 5000.0,
    "disk_total_mb": 10_000,
    "rtt_ms": 99,
}


def test_score_formula():
    assert WeightedScorer().score(_NODE) == pytest.approx(0.75088, abs=1e-6)


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
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
    ) as c:
        yield c


async def test_no_nodes_returns_503(client):
    assert (
        await client.get("/get-best-node", params={"agent_id": "test-agent"})
    ).status_code == 503


async def test_all_unhealthy_returns_503(client, fake_redis):
    await fake_redis.set("node:us-east1", json.dumps({**_HEALTHY_NODE, "healthy": False}))
    assert (
        await client.get("/get-best-node", params={"agent_id": "test-agent"})
    ).status_code == 503


async def test_returns_best_healthy_node(client, fake_redis):
    await fake_redis.set("rtt:test-agent", json.dumps({"us-east1": 42.0}))
    best = {**_HEALTHY_NODE, "queue_size": 0}
    worse = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU", "queue_size": 20}
    await fake_redis.set("node:us-east1", json.dumps(best))
    await fake_redis.set("node:eu-west1", json.dumps(worse))

    resp = await client.get("/get-best-node", params={"agent_id": "test-agent"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "us-east1"
    assert data["healthy"] is True


async def test_response_matches_node_response_schema(client, fake_redis):
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    resp = await client.get("/get-best-node", params={"agent_id": "test-agent"})
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


@pytest.mark.parametrize("path", ["/nodes", "/get-best-node?agent_id=test-agent"])
async def test_returns_503_when_redis_unreachable(monkeypatch, path):
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(
        main_module, "_redis", fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
    ) as c:
        resp = await c.get(path)
    assert resp.status_code == 503


async def test_nodes_returns_503_when_redis_uninitialized(monkeypatch):
    monkeypatch.setattr(main_module, "_redis", None)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
    ) as c:
        resp = await c.get("/nodes")
    assert resp.status_code == 503


# /heartbeat endpoint
async def test_heartbeat_returns_204(client):
    resp = await client.post(
        "/heartbeat", json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 45.0}}
    )
    assert resp.status_code == 204


async def test_heartbeat_stores_rtt_in_redis(client, fake_redis):
    await client.post("/heartbeat", json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 42.0}})
    assert json.loads(await fake_redis.get("rtt:test-agent")) == {"us-east1": 42.0}


async def test_heartbeat_overwrites_existing_rtt(client, fake_redis):
    await fake_redis.set("rtt:test-agent", json.dumps({"us-east1": 100.0}))
    await client.post("/heartbeat", json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 25.0}})
    assert json.loads(await fake_redis.get("rtt:test-agent")) == {"us-east1": 25.0}


async def test_heartbeat_affects_scoring(client, fake_redis, monkeypatch):
    """Node with lower RTT in cache should be preferred over one with higher RTT."""
    node_us = {**_HEALTHY_NODE, "node_id": "us-east1", "queue_size": 0}
    node_eu = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU", "queue_size": 0}
    await fake_redis.set("node:us-east1", json.dumps(node_us))
    await fake_redis.set("node:eu-west1", json.dumps(node_eu))

    # Give eu-west1 a much better RTT
    await client.post(
        "/heartbeat",
        json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 500.0, "eu-west1": 10.0}},
    )

    resp = await client.get("/get-best-node", params={"agent_id": "test-agent"})
    assert resp.status_code == 200
    assert resp.json()["node_id"] == "eu-west1"


async def test_unknown_agent_falls_back_to_default_scoring(client, fake_redis, monkeypatch):
    """An agent with no heartbeat yet is still routed, even if other agents have RTT data."""
    await fake_redis.set("rtt:other-agent", json.dumps({"us-east1": 10.0}))
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))

    resp = await client.get("/get-best-node", params={"agent_id": "new-agent"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["node_id"] == "us-east1"
    assert data["rtt_ms"] is None


# Study affinity (/get-best-node?study_id=...)
_EU_NODE = {**_HEALTHY_NODE, "node_id": "eu-west1", "ae_title": "Orthanc_EU"}


async def _seed(fake_redis, us_queue: int, eu_queue: int, eu_healthy: bool = True) -> None:
    await fake_redis.set("node:us-east1", json.dumps({**_HEALTHY_NODE, "queue_size": us_queue}))
    await fake_redis.set(
        "node:eu-west1", json.dumps({**_EU_NODE, "queue_size": eu_queue, "healthy": eu_healthy})
    )


async def _best(client, study_id: str | None = None):
    params = {"agent_id": "test-agent"}
    if study_id is not None:
        params["study_id"] = study_id
    resp = await client.get("/get-best-node", params=params)
    assert resp.status_code == 200
    return resp.json()


async def test_without_study_id_does_not_pin(client, fake_redis, monkeypatch):
    await _seed(fake_redis, us_queue=0, eu_queue=20)
    data = await _best(client)
    assert data["node_id"] == "us-east1"
    assert data["study_id"] is None
    assert data["rerouted"] is False
    assert await fake_redis.keys("study:*") == []


async def test_first_study_request_pins_best_node(client, fake_redis, monkeypatch):
    await _seed(fake_redis, us_queue=0, eu_queue=20)
    data = await _best(client, "s1")
    assert data["node_id"] == "us-east1"
    assert data["study_id"] == "s1"
    assert data["rerouted"] is False
    assert await fake_redis.get("study:s1") == "us-east1"
    assert 0 < await fake_redis.ttl("study:s1") <= main_module.STUDY_AFFINITY_TTL_S


async def test_study_stays_on_pinned_node_when_another_scores_higher(
    client, fake_redis, monkeypatch
):
    await _seed(fake_redis, us_queue=0, eu_queue=20)
    await _best(client, "s1")

    await _seed(fake_redis, us_queue=20, eu_queue=0)
    assert (await _best(client))["node_id"] == "eu-west1"
    data = await _best(client, "s1")
    assert data["node_id"] == "us-east1"
    assert data["rerouted"] is False


async def test_pinned_hit_refreshes_ttl(client, fake_redis, monkeypatch):
    await _seed(fake_redis, us_queue=0, eu_queue=20)
    await fake_redis.set("study:s1", "us-east1", ex=5)
    await _best(client, "s1")
    assert await fake_redis.ttl("study:s1") > 5


async def test_study_repinned_when_pinned_node_unhealthy(client, fake_redis, monkeypatch):
    await _seed(fake_redis, us_queue=20, eu_queue=0, eu_healthy=False)
    await fake_redis.set("study:s1", "eu-west1")
    data = await _best(client, "s1")
    assert data["node_id"] == "us-east1"
    assert data["rerouted"] is True
    assert await fake_redis.get("study:s1") == "us-east1"


async def test_study_repinned_when_pinned_node_telemetry_expired(client, fake_redis, monkeypatch):
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    await fake_redis.set("study:s1", "eu-west1")  # no node:eu-west1 key at all
    data = await _best(client, "s1")
    assert data["node_id"] == "us-east1"
    assert data["rerouted"] is True


async def test_study_nx_race_second_writer_gets_first_writers_node(client, fake_redis, monkeypatch):
    """Another agent pins the study between our GET and SET NX: we must follow its choice."""
    await _seed(fake_redis, us_queue=0, eu_queue=20)

    real_get = fake_redis.get
    raced = False

    async def racing_get(key):
        nonlocal raced
        if key == "study:s1" and not raced:
            raced = True
            await fake_redis.set(key, "eu-west1")  # the other agent wins the race
            return None
        return await real_get(key)

    monkeypatch.setattr(fake_redis, "get", racing_get)
    data = await _best(client, "s1")
    assert data["node_id"] == "eu-west1"
    assert data["rerouted"] is False


@pytest.mark.parametrize("study_id", ["", "bad id", "a/b", "x" * 129])
async def test_invalid_study_id_rejected(client, fake_redis, study_id):
    await fake_redis.set("node:us-east1", json.dumps(_HEALTHY_NODE))
    resp = await client.get(
        "/get-best-node", params={"agent_id": "test-agent", "study_id": study_id}
    )
    assert resp.status_code == 422


# Atomic re-pin (compare-and-set)
async def test_replace_pin_succeeds_when_key_unchanged(fake_redis):
    await fake_redis.set("study:s1", "eu-west1")
    assert await main_module._replace_pin(fake_redis, "study:s1", "eu-west1", "us-east1")
    assert await fake_redis.get("study:s1") == "us-east1"


async def test_replace_pin_fails_when_another_agent_already_moved_it(fake_redis):
    await fake_redis.set("study:s1", "asia-northeast1")
    assert not await main_module._replace_pin(fake_redis, "study:s1", "eu-west1", "us-east1")
    assert await fake_redis.get("study:s1") == "asia-northeast1"


async def test_replace_pin_fails_when_key_changes_after_watch(fake_redis, monkeypatch):
    """Another agent writes between WATCH and EXEC: the transaction must abort."""
    await fake_redis.set("study:s1", "eu-west1")
    other = fakeredis.aioredis.FakeRedis(
        connection_pool=fake_redis.connection_pool, decode_responses=True
    )
    real_pipeline = fake_redis.pipeline

    def racing_pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_watch = pipe.watch

        async def watch(*keys):
            await real_watch(*keys)
            await other.set("study:s1", "asia-northeast1")

        pipe.watch = watch
        return pipe

    monkeypatch.setattr(fake_redis, "pipeline", racing_pipeline)
    assert not await main_module._replace_pin(fake_redis, "study:s1", "eu-west1", "us-east1")
    assert await other.get("study:s1") == "asia-northeast1"


async def test_concurrent_repin_follows_other_agents_choice(client, fake_redis, monkeypatch):
    """If another agent re-pinned the study first, use its node instead of overwriting it."""
    await _seed(fake_redis, us_queue=0, eu_queue=0, eu_healthy=False)
    await fake_redis.set(
        "node:asia-northeast1",
        json.dumps({**_HEALTHY_NODE, "node_id": "asia-northeast1", "queue_size": 20}),
    )
    await fake_redis.set("study:s1", "eu-west1")

    async def other_agent_wins(redis, key, old, new):
        await redis.set(key, "asia-northeast1")
        return False

    monkeypatch.setattr(main_module, "_replace_pin", other_agent_wins)
    data = await _best(client, "s1")
    assert data["node_id"] == "asia-northeast1"
    assert await fake_redis.get("study:s1") == "asia-northeast1"


async def test_pin_gives_up_with_503_when_it_never_settles(client, fake_redis, monkeypatch):
    await _seed(fake_redis, us_queue=0, eu_queue=0, eu_healthy=False)
    await fake_redis.set("study:s1", "eu-west1")

    async def always_lose(redis, key, old, new):
        return False

    monkeypatch.setattr(main_module, "_replace_pin", always_lose)
    resp = await client.get("/get-best-node", params={"agent_id": "test-agent", "study_id": "s1"})
    assert resp.status_code == 503


# RTT stored in Redis
async def test_heartbeat_rtt_expires(client, fake_redis):
    """An agent that stops sending heartbeats does not keep its old RTTs forever."""
    await client.post("/heartbeat", json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 42.0}})
    assert 0 < await fake_redis.ttl("rtt:test-agent") <= main_module.RTT_TTL_S


async def test_rtt_shared_between_orchestrator_instances(client, fake_redis):
    """RTT lives in Redis, so a restarted or second orchestrator sees the same data."""
    await client.post(
        "/heartbeat",
        json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 500.0, "eu-west1": 10.0}},
    )
    await _seed(fake_redis, us_queue=0, eu_queue=0)
    assert await fake_redis.get("rtt:test-agent") is not None
    assert (await _best(client))["node_id"] == "eu-west1"


async def test_heartbeat_returns_503_when_redis_unreachable(monkeypatch):
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(
        main_module, "_redis", fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
    ) as c:
        resp = await c.post(
            "/heartbeat", json={"agent_id": "test-agent", "rtt_dict": {"us-east1": 45.0}}
        )
    assert resp.status_code == 503
