"""Unit tests for how the telemetry daemon judges a node's free disk space."""

import json

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import Response

import src.orchestrator.daemon as daemon_module
from src.orchestrator.daemon import poll_node

pytestmark = pytest.mark.unit

BASE = "https://orthanc-us:8042"
JOBS = [{"ID": "a", "State": "Success"}]


def _cfg() -> dict:
    return {"base": BASE, "ae_title": "Orthanc_US", "auth": ("orthanc", "pass")}


@pytest.fixture(autouse=True)
def fresh_quota_cache(monkeypatch):
    monkeypatch.setattr(daemon_module, "node_quota_map", {})
    monkeypatch.setattr(daemon_module, "_quota_checked_at", {})


def _mock_node(quota_mb: int, used_mb: float) -> None:
    respx.get(f"{BASE}/system").mock(
        return_value=Response(
            200, json={"MaximumStorageSize": quota_mb, "MaximumStorageMode": "Reject"}
        )
    )
    respx.get(f"{BASE}/statistics").mock(
        return_value=Response(200, json={"CountInstances": 1, "TotalDiskSizeMB": used_mb})
    )
    respx.get(f"{BASE}/jobs?expand").mock(return_value=Response(200, json=JOBS))


async def _poll() -> dict:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    async with httpx.AsyncClient() as client:
        await poll_node(client, redis, "us-east1", _cfg())
    return json.loads(await redis.get("node:us-east1"))


@respx.mock
@pytest.mark.asyncio
async def test_node_below_min_free_mb_is_unhealthy(monkeypatch):
    # 400 MB free is 4% of 10 GB, so only MIN_FREE_MB can mark this node unhealthy.
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 500.0)
    _mock_node(quota_mb=10_000, used_mb=9_600)

    payload = await _poll()

    assert payload["healthy"] is False
    assert payload["disk_free_mb"] == 400


@respx.mock
@pytest.mark.asyncio
async def test_node_above_min_free_mb_stays_healthy(monkeypatch):
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 500.0)
    _mock_node(quota_mb=10_000, used_mb=9_400)

    assert (await _poll())["healthy"] is True


@respx.mock
@pytest.mark.asyncio
async def test_two_percent_rule_still_applies_without_min_free_mb(monkeypatch):
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 0.0)
    _mock_node(quota_mb=10_000, used_mb=9_850)

    assert (await _poll())["healthy"] is False


@respx.mock
@pytest.mark.asyncio
async def test_node_without_quota_ignores_min_free_mb(monkeypatch):
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 500.0)
    _mock_node(quota_mb=0, used_mb=50_000)

    payload = await _poll()

    assert payload["healthy"] is True
    assert payload["disk_free_mb"] is None


@respx.mock
@pytest.mark.asyncio
async def test_raised_quota_is_picked_up_without_restarting_the_daemon(monkeypatch):
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 0.0)
    _mock_node(quota_mb=10_000, used_mb=9_900)
    assert (await _poll())["healthy"] is False

    # The operator doubles the node's storage quota; the cached value is now stale.
    _mock_node(quota_mb=20_000, used_mb=9_900)
    daemon_module._quota_checked_at["us-east1"] -= daemon_module.QUOTA_REFRESH_S + 1

    payload = await _poll()

    assert payload["healthy"] is True
    assert payload["disk_total_mb"] == 20_000


@respx.mock
@pytest.mark.asyncio
async def test_quota_is_not_fetched_on_every_poll(monkeypatch):
    monkeypatch.setattr(daemon_module, "MIN_FREE_MB", 0.0)
    _mock_node(quota_mb=10_000, used_mb=100)

    await _poll()
    await _poll()
    await _poll()

    assert respx.get(f"{BASE}/system").call_count == 1


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf"])
def test_invalid_min_free_mb_is_rejected(monkeypatch, value):
    monkeypatch.setenv("MIN_FREE_MB", value)

    with pytest.raises(ValueError, match="MIN_FREE_MB"):
        daemon_module._min_free_mb()


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 0.0), ("", 0.0), ("500", 500.0), ("0", 0.0)]
)
def test_min_free_mb_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MIN_FREE_MB", raising=False)
    else:
        monkeypatch.setenv("MIN_FREE_MB", value)

    assert daemon_module._min_free_mb() == expected
