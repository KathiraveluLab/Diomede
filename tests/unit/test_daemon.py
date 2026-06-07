"""Unit tests for src/orchestrator/daemon.py"""

import json

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

from src.orchestrator.daemon import NODES, REDIS_TTL_S, poll_node

pytestmark = pytest.mark.unit

BASE = "https://orthanc-us:8042"

SYSTEM_OK = {
    "MaximumStorageSize": 10000,
}

STATS_OK = {
    "CountInstances": 5,
    "TotalDiskSizeMB": 200,
}

JOBS_MIXED = [
    {"ID": "a", "State": "Running"},
    {"ID": "b", "State": "Pending"},
    {"ID": "c", "State": "Success"},
    {"ID": "d", "State": "Failure"},
]


def _cfg(base: str = BASE) -> dict:
    return {"base": base, "ae_title": "Orthanc_US", "auth": ("orthanc", "pass")}


async def _redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@respx.mock
@pytest.mark.asyncio
async def test_healthy_payload_written_to_redis():
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/system").mock(return_value=Response(200, json=SYSTEM_OK))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=JOBS_MIXED))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    payload = json.loads(await redis.get("node:us-east1"))
    assert payload["healthy"] is True
    assert payload["node_id"] == "us-east1"
    assert payload["ae_title"] == "Orthanc_US"
    assert payload["base_url"] == BASE
    assert payload["instance_count"] == 5
    assert payload["disk_free_mb"] == 9800
    assert payload["disk_total_mb"] == 10_000  # 200 + 9800
    assert payload["queue_size"] == 2  # Running + Pending only
    assert "ts" in payload


@respx.mock
@pytest.mark.asyncio
async def test_queue_size_counts_only_pending_and_running():
    jobs = [
        {"ID": "a", "State": "Running"},
        {"ID": "b", "State": "Pending"},
        {"ID": "c", "State": "Pending"},
        {"ID": "d", "State": "Success"},
        {"ID": "e", "State": "Failure"},
    ]
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/system").mock(return_value=Response(200, json=SYSTEM_OK))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=jobs))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert json.loads(await redis.get("node:us-east1"))["queue_size"] == 3


@respx.mock
@pytest.mark.asyncio
async def test_disk_size():
    stats = {"CountInstances": 0, "TotalDiskSizeMB": 100}
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=stats))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=[]))

    respx.get(f"{BASE}/system").mock(return_value=Response(200, json=SYSTEM_OK))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    payload = json.loads(await redis.get("node:us-east1"))
    assert payload["disk_free_mb"] == 9900
    assert payload["disk_total_mb"] == 10_000


@respx.mock
@pytest.mark.asyncio
async def test_connection_error_writes_unhealthy_payload():
    respx.get(f"{BASE}/statistics").mock(side_effect=httpx.ConnectError("refused"))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    payload = json.loads(await redis.get("node:us-east1"))
    assert payload["healthy"] is False
    assert payload["node_id"] == "us-east1"
    assert "ts" in payload


@respx.mock
@pytest.mark.asyncio
async def test_http_401_writes_unhealthy_payload():
    respx.get(f"{BASE}/statistics").mock(return_value=Response(401))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert json.loads(await redis.get("node:us-east1"))["healthy"] is False


@respx.mock
@pytest.mark.asyncio
async def test_jobs_endpoint_failure_writes_unhealthy_payload():
    """/statistics succeeds but /jobs fails — whole poll is treated as unhealthy."""
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(500))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert json.loads(await redis.get("node:us-east1"))["healthy"] is False


# Redis TTL


@respx.mock
@pytest.mark.asyncio
async def test_redis_key_written_with_correct_ttl():
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=[]))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    ttl = await redis.ttl("node:us-east1")
    assert 0 < ttl <= REDIS_TTL_S


@respx.mock
@pytest.mark.asyncio
async def test_unhealthy_payload_also_gets_ttl():
    """Dead node keys also expire so they are auto-removed after TTL seconds."""
    respx.get(f"{BASE}/statistics").mock(side_effect=httpx.ConnectError("refused"))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    ttl = await redis.ttl("node:us-east1")
    assert 0 < ttl <= REDIS_TTL_S


# NODES config


def test_all_four_regions_present():
    assert set(NODES.keys()) == {"us-east1", "eu-west1", "asia-northeast1", "af-south1"}


def test_ae_titles_match_orthanc_config():
    assert NODES["us-east1"]["ae_title"] == "Orthanc_US"
    assert NODES["eu-west1"]["ae_title"] == "Orthanc_EU"
    assert NODES["asia-northeast1"]["ae_title"] == "Orthanc_ASIA"
    assert NODES["af-south1"]["ae_title"] == "Orthanc_AF"
