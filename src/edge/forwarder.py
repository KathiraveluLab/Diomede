"""
Forwarder Daemon runs two asyncio loops:
  - probes each cloud node once per hour
  - reports RTT to the Orchestrator via POST /heartbeat.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from src.utils.logging_config import get_logger

log = get_logger(__name__, "FORWARDER")

ORCH_HEARTBEAT_URL = os.getenv("ORCH_HEARTBEAT_URL", "https://orchestrator:8000/heartbeat")
ORCH_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
PROBE_INTERVAL_S = int(os.getenv("PROBE_INTERVAL_S", "3600"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "")

CLOUD_NODES: dict[str, dict[str, str | tuple[str, str]]] = {
    "us-east1": {
        "base": os.getenv("NODE_US_BASE", "https://orthanc-us:8042"),
        "auth": (os.getenv("NODE_US_USER", "orthanc"), os.getenv("NODE_US_PASS", "orthanc")),
    },
    "eu-west1": {
        "base": os.getenv("NODE_EU_BASE", "https://orthanc-eu:8042"),
        "auth": (os.getenv("NODE_EU_USER", "orthanc"), os.getenv("NODE_EU_PASS", "orthanc")),
    },
    "asia-northeast1": {
        "base": os.getenv("NODE_ASIA_BASE", "https://orthanc-asia:8042"),
        "auth": (os.getenv("NODE_ASIA_USER", "orthanc"), os.getenv("NODE_ASIA_PASS", "orthanc")),
    },
    "af-south1": {
        "base": os.getenv("NODE_AF_BASE", "https://orthanc-af:8042"),
        "auth": (os.getenv("NODE_AF_USER", "orthanc"), os.getenv("NODE_AF_PASS", "orthanc")),
    },
}


def _orch_headers() -> dict[str, str]:
    return {"X-API-Key": ORCH_API_KEY} if ORCH_API_KEY else {}


async def latency_probe_loop(client: httpx.AsyncClient) -> None:
    """GET /system on each cloud node, measure RTT, report to /heartbeat."""
    while True:
        for node_id, cfg in CLOUD_NODES.items():
            base = cfg["base"]
            auth = cfg["auth"]
            assert isinstance(auth, tuple)
            try:
                t0 = time.monotonic()
                resp = await client.get(f"{base}/system", auth=auth, timeout=10)
                rtt_ms = (time.monotonic() - t0) * 1000
                resp.raise_for_status()

                await client.post(
                    ORCH_HEARTBEAT_URL,
                    json={"node_id": node_id, "rtt_ms": round(rtt_ms, 1)},
                    headers=_orch_headers(),
                    timeout=5,
                )
                log.info("node=%-15s rtt=%.1f ms", node_id, rtt_ms)
            except Exception as exc:
                log.warning("node=%-15s probe failed: %s", node_id, exc)

        await asyncio.sleep(PROBE_INTERVAL_S)


async def run() -> None:
    verify: str | bool = CA_CERT if CA_CERT else True
    async with httpx.AsyncClient(verify=verify) as client:
        await latency_probe_loop(client)


if __name__ == "__main__":
    asyncio.run(run())
