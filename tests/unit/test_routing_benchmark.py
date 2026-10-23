"""Unit tests for src/benchmark/routing_correctness.py"""

import json
import sys

import fakeredis.aioredis
import pytest

import src.benchmark.routing_correctness as bench

pytestmark = pytest.mark.unit


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
