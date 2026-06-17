"""
Telemetry Daemon polls all Orthanc nodes every POLL_INTERVAL_S seconds and
writes a structured JSON heartbeat to Redis with a REDIS_TTL_S expiry.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis

from src.utils.logging_config import get_logger

log = get_logger(__name__, "DAEMON")


# Configuration from environment

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "10"))
REDIS_TTL_S = int(os.getenv("REDIS_TTL_S", "30"))
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "5"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "")


NODES: dict[str, dict[str, str | tuple[str, str]]] = {
    "us-east1": {
        "base": os.getenv("NODE_US_BASE", "https://orthanc-us:8042"),
        "ae_title": "Orthanc_US",
        "auth": (
            os.getenv("NODE_US_USER", "orthanc"),
            os.getenv("NODE_US_PASS", "orthanc"),
        ),
    },
    "eu-west1": {
        "base": os.getenv("NODE_EU_BASE", "https://orthanc-eu:8042"),
        "ae_title": "Orthanc_EU",
        "auth": (
            os.getenv("NODE_EU_USER", "orthanc"),
            os.getenv("NODE_EU_PASS", "orthanc"),
        ),
    },
    "asia-northeast1": {
        "base": os.getenv("NODE_ASIA_BASE", "https://orthanc-asia:8042"),
        "ae_title": "Orthanc_ASIA",
        "auth": (
            os.getenv("NODE_ASIA_USER", "orthanc"),
            os.getenv("NODE_ASIA_PASS", "orthanc"),
        ),
    },
    "af-south1": {
        "base": os.getenv("NODE_AF_BASE", "https://orthanc-af:8042"),
        "ae_title": "Orthanc_AF",
        "auth": (
            os.getenv("NODE_AF_USER", "orthanc"),
            os.getenv("NODE_AF_PASS", "orthanc"),
        ),
    },
}


# Polling logic

node_quota_map: dict[str, int] = {}


async def poll_node(
    client: httpx.AsyncClient,
    redis: aioredis.Redis[str],
    node_id: str,
    cfg: dict[str, str | tuple[str, str]],
) -> None:
    """Get /statistics and /jobs?expand from one node and write to Redis."""
    base = cfg["base"]
    auth = cfg["auth"]
    assert isinstance(auth, tuple)

    try:
        stats_resp = await client.get(f"{base}/statistics", auth=auth, timeout=HTTP_TIMEOUT_S)
        stats_resp.raise_for_status()
        stats = stats_resp.json()

        if node_id not in node_quota_map:
            system_resp = await client.get(f"{base}/system", auth=auth, timeout=HTTP_TIMEOUT_S)
            system_resp.raise_for_status()
            system = system_resp.json()
            log.debug("System info for %s: %s", node_id, system)
            node_quota_map[node_id] = system.get("MaximumStorageSize")

        log.debug("Statistics for %s: %s", node_id, stats)

        # /jobs?expand returns full job objects; plain /jobs returns only IDs.
        jobs_resp = await client.get(f"{base}/jobs?expand", auth=auth, timeout=HTTP_TIMEOUT_S)
        jobs_resp.raise_for_status()
        jobs = jobs_resp.json()

        queue_size = len([j for j in jobs if j.get("State") in ("Pending", "Running")])

        disk_used_mb = stats.get("TotalDiskSizeMB")
        max_storage_mb = node_quota_map.get(node_id, 0) or 0
        disk_free_mb = max(0.0, float(max_storage_mb - disk_used_mb))

        # if free disk space is less than 2%, set node to unhealthy
        is_disk_full = True if float(disk_free_mb / max_storage_mb) < 0.02 else False

        payload = {
            "node_id": node_id,
            "ae_title": cfg["ae_title"],
            "base_url": base,
            "queue_size": queue_size,
            "disk_free_mb": disk_free_mb,
            "disk_total_mb": max_storage_mb,
            "instance_count": stats.get("CountInstances", 0),
            "healthy": not is_disk_full,
            "ts": datetime.now(tz=UTC).isoformat(),
        }

        log.info(
            "node=%-15s healthy  queue=%d  disk_free=%.0f MB  instances=%d",
            node_id,
            queue_size,
            disk_free_mb,
            payload["instance_count"],
        )

    except Exception as exc:
        log.warning("node=%-15s unreachable: %s", node_id, exc)
        payload = {
            "node_id": node_id,
            "ae_title": cfg["ae_title"],
            "base_url": base,
            "healthy": False,
            "ts": datetime.now(tz=UTC).isoformat(),
        }

    try:
        await redis.setex(f"node:{node_id}", REDIS_TTL_S, json.dumps(payload))
    except Exception as redis_err:
        log.error("Failed to write node:%s status to Redis: %s", node_id, redis_err)


async def run() -> None:
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    verify: str | bool = CA_CERT if CA_CERT else True

    try:
        async with httpx.AsyncClient(verify=verify) as client:
            log.info(
                "Telemetry Daemon started — polling %d nodes every %ds (TTL %ds)",
                len(NODES),
                POLL_INTERVAL_S,
                REDIS_TTL_S,
            )
            while True:
                await asyncio.gather(
                    *[poll_node(client, redis, nid, cfg) for nid, cfg in NODES.items()]
                )
                await asyncio.sleep(POLL_INTERVAL_S)
    finally:
        await redis.close()


if __name__ == "__main__":
    asyncio.run(run())
