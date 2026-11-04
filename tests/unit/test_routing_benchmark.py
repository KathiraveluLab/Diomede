"""Unit tests for src/benchmark/routing_correctness.py"""

import asyncio
import json
import sys

import certifi
import fakeredis.aioredis
import pytest

import src.benchmark.routing_correctness as bench

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ca_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())


def test_every_permutation_has_a_single_least_loaded_node():
    for perm in bench.generate_permutations():
        queues = sorted(perm.values())
        assert queues[0] < queues[1], perm


@pytest.mark.asyncio
async def test_synthetic_telemetry_leaves_the_edge_agents_rtts_alone():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    measured = json.dumps({"us-east1": 12.5})
    await redis.set("rtt:agent-001", measured)

    await bench.set_synthetic_telemetry(redis, {region: 0 for region in bench.REGIONS})

    assert await redis.get("rtt:agent-001") == measured


def test_missing_ca_bundle_stops_the_benchmark_before_it_queries_the_orchestrator(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(
        bench.aioredis,
        "from_url",
        lambda *args, **kwargs: fakeredis.aioredis.FakeRedis(decode_responses=True),
    )

    with pytest.raises(RuntimeError, match="gen_certs"):
        asyncio.run(bench.evaluate_correctness(1))


def test_daemon_is_not_paused_when_the_ca_bundle_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    calls = []
    monkeypatch.setattr(bench.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(sys, "argv", ["routing_correctness"])

    with pytest.raises(RuntimeError, match="gen_certs"):
        bench.main()

    assert calls == []


def test_daemon_is_paused_while_the_trials_run(monkeypatch):
    calls = []

    async def trials(num_trials):
        calls.append("trials")
        raise RuntimeError("orchestrator down")

    monkeypatch.setattr(bench, "evaluate_correctness", trials)
    monkeypatch.setattr(bench.subprocess, "run", lambda cmd, **kw: calls.append(cmd[1]))
    monkeypatch.setattr(sys, "argv", ["routing_correctness"])

    with pytest.raises(RuntimeError):
        bench.main()

    assert calls == ["pause", "trials", "unpause"]
