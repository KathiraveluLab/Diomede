"""Unit tests for src/benchmark/routing_correctness.py"""

import json

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
