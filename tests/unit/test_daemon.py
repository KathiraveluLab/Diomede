"""Unit tests for src/orchestrator/daemon.py"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

import src.orchestrator.daemon as daemon_module
from src.orchestrator.daemon import NODES, REDIS_TTL_S, poll_node
from src.utils.logging_config import get_logger

log = get_logger(__name__, "TEST")

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


@respx.mock
@pytest.mark.asyncio
async def test_slow_node_is_cut_off_and_marked_unhealthy(monkeypatch):
    """A node that keeps a response trickling in must not hold the poll past its deadline."""
    monkeypatch.setattr(daemon_module, "POLL_DEADLINE_S", 0.1, raising=False)

    async def _slow(request):
        await asyncio.sleep(5)
        return Response(200, json=STATS_OK)

    respx.get(f"{BASE}/statistics").mock(side_effect=_slow)

    redis = await _redis()
    start = time.monotonic()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert time.monotonic() - start < 1
    assert json.loads(await redis.get("node:us-east1"))["healthy"] is False


def test_warns_when_ttl_shorter_than_poll_cycle(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    monkeypatch.setattr(daemon_module, "POLL_INTERVAL_S", 10)
    monkeypatch.setattr(daemon_module, "POLL_DEADLINE_S", 21.0)
    monkeypatch.setattr(daemon_module, "REDIS_TTL_S", 30)

    daemon_module._warn_short_ttl()

    assert "REDIS_TTL_S" in caplog.text


def test_no_ttl_warning_with_default_settings(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    monkeypatch.setattr(daemon_module, "POLL_INTERVAL_S", 10)
    monkeypatch.setattr(daemon_module, "POLL_DEADLINE_S", 15.0)
    monkeypatch.setattr(daemon_module, "REDIS_TTL_S", 30)

    daemon_module._warn_short_ttl()

    assert "REDIS_TTL_S" not in caplog.text


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


@respx.mock
@pytest.mark.asyncio
async def test_unlimited_storage_reports_unknown_free_disk(monkeypatch):
    """MaximumStorageSize = 0 means no quota: free disk is unknown, not 0 MB."""
    monkeypatch.setattr("src.orchestrator.daemon.node_quota_map", {})
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/system").mock(return_value=Response(200, json={"MaximumStorageSize": 0}))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=[]))

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    payload = json.loads(await redis.get("node:us-east1"))
    assert payload["healthy"] is True
    assert payload["disk_free_mb"] is None
    assert payload["disk_total_mb"] is None


def test_all_four_regions_present():
    assert set(NODES.keys()) == {"us-east1", "eu-west1", "asia-northeast1", "af-south1"}


def test_ae_titles_match_orthanc_config():
    assert NODES["us-east1"]["ae_title"] == "Orthanc_US"
    assert NODES["eu-west1"]["ae_title"] == "Orthanc_EU"
    assert NODES["asia-northeast1"]["ae_title"] == "Orthanc_ASIA"
    assert NODES["af-south1"]["ae_title"] == "Orthanc_AF"


# Redis write failures


@respx.mock
@pytest.mark.asyncio
async def test_redis_write_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/system").mock(return_value=Response(200, json=SYSTEM_OK))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=JOBS_MIXED))

    redis = await _redis()
    monkeypatch.setattr(redis, "setex", AsyncMock(side_effect=ConnectionError("redis down")))
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert "Failed to write node:us-east1" in caplog.text


# Default credential warning


def test_warns_when_default_node_password_is_used(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    monkeypatch.setenv("NODE_US_PASS", "orthanc")

    daemon_module._warn_default_creds()

    assert "insecure default" in caplog.text


def test_no_credential_warning_when_password_is_changed(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    monkeypatch.setenv("NODE_US_PASS", "a-strong-password")

    daemon_module._warn_default_creds()

    assert "insecure default" not in caplog.text


# run() loop


def _stop_run_after_first_cycle(monkeypatch):
    """Stub Redis, node polling and the startup checks so run() stops after one poll cycle."""
    fake_redis = AsyncMock()
    poll = AsyncMock()
    warn_creds = MagicMock()
    warn_ttl = MagicMock()
    monkeypatch.setattr(daemon_module.aioredis, "from_url", lambda *a, **kw: fake_redis)
    monkeypatch.setattr(daemon_module, "poll_node", poll)
    monkeypatch.setattr(daemon_module, "_warn_default_creds", warn_creds)
    monkeypatch.setattr(daemon_module, "_warn_short_ttl", warn_ttl)
    monkeypatch.setattr(
        daemon_module.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)
    )
    return fake_redis, poll, warn_creds, warn_ttl


@pytest.mark.asyncio
async def test_run_polls_every_node_then_closes_redis(monkeypatch):
    fake_redis, poll, _, _ = _stop_run_after_first_cycle(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await daemon_module.run()

    assert poll.await_count == len(NODES)
    assert {call.args[2] for call in poll.await_args_list} == set(NODES)
    fake_redis.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_checks_startup_warnings(monkeypatch):
    _, _, warn_creds, warn_ttl = _stop_run_after_first_cycle(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await daemon_module.run()

    warn_creds.assert_called_once()
    warn_ttl.assert_called_once()


@pytest.fixture
def daemon_logs(monkeypatch, caplog):
    monkeypatch.setattr(daemon_module.log, "propagate", True)
    monkeypatch.setattr(daemon_module, "node_quota_map", {})
    return caplog


def _mock_node(system: dict) -> None:
    respx.get(f"{BASE}/statistics").mock(return_value=Response(200, json=STATS_OK))
    respx.get(f"{BASE}/system").mock(return_value=Response(200, json=system))
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=JOBS_MIXED))


@respx.mock
@pytest.mark.asyncio
async def test_warns_once_when_a_node_recycles_its_oldest_patients(daemon_logs):
    _mock_node({"MaximumStorageSize": 10000, "MaximumStorageMode": "Recycle"})

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())
        await poll_node(client, redis, "us-east1", _cfg())

    warnings = [r for r in daemon_logs.records if "MaximumStorageMode" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


@respx.mock
@pytest.mark.asyncio
async def test_warns_when_the_storage_mode_is_not_reported(daemon_logs):
    """Orthanc releases that predate MaximumStorageMode always recycle."""
    _mock_node({"MaximumStorageSize": 10000})

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert "MaximumStorageMode" in daemon_logs.text


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "system",
    [
        {"MaximumStorageSize": 10000, "MaximumStorageMode": "Reject"},
        {"MaximumStorageSize": 0},
    ],
    ids=["reject", "no-quota"],
)
async def test_no_storage_mode_warning_when_nothing_is_deleted(daemon_logs, system):
    _mock_node(system)

    redis = await _redis()
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())

    assert "MaximumStorageMode" not in daemon_logs.text
