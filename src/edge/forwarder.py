from __future__ import annotations

import asyncio
import os
import time
from typing import TypedDict

import httpx

from src.edge.orthanc_source import OrthancSource
from src.edge.transport import DicomSource
from src.utils.logging_config import get_logger

log = get_logger(__name__, "FORWARDER")

ORCH_URL = os.getenv("ORCH_URL", "http://orchestrator:8000/get-best-node")
ORCH_HEARTBEAT_URL = os.getenv("ORCH_HEARTBEAT_URL", "http://orchestrator:8000/heartbeat")
ORCH_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "5"))
PROBE_INTERVAL_S = int(os.getenv("PROBE_INTERVAL_S", "3600"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "")


class _NodeCfg(TypedDict):
    base: str
    auth: tuple[str, str]


CLOUD_NODES: dict[str, _NodeCfg] = {
    "us-east1": {
        "base": os.getenv("NODE_US_BASE", "http://orthanc-us:8042"),
        "auth": (os.getenv("NODE_US_USER", "orthanc"), os.getenv("NODE_US_PASS", "orthanc")),
    },
    "eu-west1": {
        "base": os.getenv("NODE_EU_BASE", "http://orthanc-eu:8042"),
        "auth": (os.getenv("NODE_EU_USER", "orthanc"), os.getenv("NODE_EU_PASS", "orthanc")),
    },
    "asia-northeast1": {
        "base": os.getenv("NODE_ASIA_BASE", "http://orthanc-asia:8042"),
        "auth": (os.getenv("NODE_ASIA_USER", "orthanc"), os.getenv("NODE_ASIA_PASS", "orthanc")),
    },
    "af-south1": {
        "base": os.getenv("NODE_AF_BASE", "http://orthanc-af:8042"),
        "auth": (os.getenv("NODE_AF_USER", "orthanc"), os.getenv("NODE_AF_PASS", "orthanc")),
    },
}


def _orch_headers() -> dict[str, str]:
    return {"X-API-Key": ORCH_API_KEY} if ORCH_API_KEY else {}


async def route_instance(
    client: httpx.AsyncClient,
    source: DicomSource,
    instance_id: str,
) -> None:
    """Forward to the best node and delete the local copy."""

    # 1. Fetch raw DICOM bytes from the edge buffer.
    try:
        dcm_bytes = await source.fetch(client, instance_id)
    except Exception as exc:
        log.error("instance=%s fetch failed: %s", instance_id, exc)
        return

    # 2. Ask the Orchestrator for the best destination.
    try:
        best_resp = await client.get(
            ORCH_URL, params={"agent_id": os.getenv("AGENT_ID")}, headers=_orch_headers(), timeout=5
        )
        best_resp.raise_for_status()
        best = best_resp.json()
    except Exception as exc:
        log.error("instance=%s orchestrator query failed: %s", instance_id, exc)
        return

    node_id = best.get("node_id")
    if not node_id:
        log.error("instance=%s orchestrator response missing 'node_id'", instance_id)
        return

    node_cfg = CLOUD_NODES.get(node_id)
    if not node_cfg:
        log.error("instance=%s unknown node_id '%s' from orchestrator", instance_id, node_id)
        return

    # 3. POST raw DICOM bytes to the winning cloud node.
    try:
        post_resp = await client.post(
            f"{node_cfg['base']}/instances",
            content=dcm_bytes,
            headers={"Content-Type": "application/dicom"},
            auth=node_cfg["auth"],
            timeout=120,
        )
        post_resp.raise_for_status()
    except Exception as exc:
        log.error("instance=%s forward to %s failed: %s", instance_id, node_id, exc)
        return

    log.info("instance=%s routed -> %s (score=%.4f)", instance_id, node_id, best.get("score", 0))

    # 4. Acknowledge (delete local copy) only after a confirmed successful forward.
    try:
        await source.acknowledge(client, instance_id)
    except Exception as exc:
        log.warning("instance=%s acknowledge failed: %s", instance_id, exc)


async def forward_loop(client: httpx.AsyncClient, source: DicomSource) -> None:
    """Poll the DicomSource every POLL_INTERVAL_S seconds and route new instances."""
    while True:
        try:
            instance_ids = await source.poll_new(client)
            for instance_id in instance_ids:
                await route_instance(client, source, instance_id)
        except Exception as exc:
            log.warning("forward_loop error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_S)


async def latency_probe_loop(client: httpx.AsyncClient) -> None:
    """GET /system on each cloud node once per hour, report RTT to /heartbeat."""
    while True:
        rtt_dict: dict[str, float] = {}
        for node_id, cfg in CLOUD_NODES.items():
            base = cfg["base"]
            auth = cfg["auth"]
            try:
                t0 = time.monotonic()
                resp = await client.get(f"{base}/system", auth=auth, timeout=10)
                rtt_ms = (time.monotonic() - t0) * 1000
                resp.raise_for_status()
                rtt_dict[node_id] = round(rtt_ms, 1)
                log.info("node=%-15s rtt=%.1f ms", node_id, rtt_ms)
            except Exception as exc:
                log.warning("node=%-15s probe failed: %s", node_id, exc)

        if rtt_dict:
            try:
                await client.post(
                    ORCH_HEARTBEAT_URL,
                    json={"agent_id": os.getenv("AGENT_ID"), "rtt_dict": rtt_dict},
                    headers=_orch_headers(),
                    timeout=5,
                )
            except Exception as exc:
                log.warning("heartbeat failed: %s", exc)

        await asyncio.sleep(PROBE_INTERVAL_S)


async def run(source: DicomSource | None = None) -> None:
    verify: str | bool = CA_CERT if CA_CERT else True
    if source is None:
        source = OrthancSource()
    async with httpx.AsyncClient(verify=verify) as client:
        await asyncio.gather(
            forward_loop(client, source),
            latency_probe_loop(client),
        )


if __name__ == "__main__":
    asyncio.run(run())
