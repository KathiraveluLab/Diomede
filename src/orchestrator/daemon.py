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
from dotenv import load_dotenv

from src.utils.env import require_env
from src.utils.logging_config import get_logger

log = get_logger(__name__, "DAEMON")
load_dotenv()

# Configuration from environment

REDIS_URL = require_env("REDIS_URL")
POLL_INTERVAL_S = int(require_env("DAEMON_POLL_INTERVAL_S"))
REDIS_TTL_S = int(require_env("REDIS_TTL_S"))
HTTP_TIMEOUT_S = float(require_env("HTTP_TIMEOUT_S"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE")


NODES: dict[str, dict[str, str | tuple[str, str]]] = {
    require_env("REGION1_NAME"): {
        "base": require_env("NODE_US_BASE"),
        "ae_title": require_env("REGION1_AET"),
        "auth": (
            require_env("NODE_US_USER"),
            require_env("NODE_US_PASS"),
        ),
    },
    require_env("REGION2_NAME"): {
        "base": require_env("NODE_EU_BASE"),
        "ae_title": require_env("REGION2_AET"),
        "auth": (
            require_env("NODE_EU_USER"),
            require_env("NODE_EU_PASS"),
        ),
    },
    require_env("REGION3_NAME"): {
        "base": require_env("NODE_ASIA_BASE"),
        "ae_title": require_env("REGION3_AET"),
        "auth": (
            require_env("NODE_ASIA_USER"),
            require_env("NODE_ASIA_PASS"),
        ),
    },
    require_env("REGION4_NAME"): {
        "base": require_env("NODE_AF_BASE"),
        "ae_title": require_env("REGION4_AET"),
        "auth": (
            require_env("NODE_AF_USER"),
            require_env("NODE_AF_PASS"),
        ),
    },
}


# Polling logic

node_quota_map: dict[str, int] = {}


async def poll_node(
    client: httpx.AsyncClient,
    redis: aioredis.Redis[str],  # type: ignore[type-arg]
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
        disk_free_mb: float | None
        disk_total_mb: int | None
        if max_storage_mb > 0:
            free_mb = max(0.0, float(max_storage_mb - disk_used_mb))
            disk_free_mb, disk_total_mb = free_mb, max_storage_mb
            # if free disk space is less than 2%, set node to unhealthy
            is_disk_full = free_mb / max_storage_mb < 0.02
        else:
            # MaximumStorageSize = 0 means no quota: free space is unknown, not zero.
            disk_free_mb = disk_total_mb = None
            is_disk_full = False

        payload = {
            "node_id": node_id,
            "ae_title": cfg["ae_title"],
            "base_url": base,
            "queue_size": queue_size,
            "disk_free_mb": disk_free_mb,
            "disk_total_mb": disk_total_mb,
            "instance_count": stats.get("CountInstances", 0),
            "healthy": not is_disk_full,
            "ts": datetime.now(tz=UTC).isoformat(),
        }

        log.info(
            "node=%-15s healthy  queue=%d  disk_free=%s MB  instances=%d",
            node_id,
            queue_size,
            "unlimited" if disk_free_mb is None else f"{disk_free_mb:.0f}",
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


def _warn_default_creds() -> None:
    if os.getenv("NODE_US_PASS", "orthanc") == "orthanc":
        log.warning(
            "NODE_*_PASS is using the insecure default 'orthanc'. "
            "Set NODE_*_PASS env vars before deploying outside local dev."
        )


async def run() -> None:
    _warn_default_creds()

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
