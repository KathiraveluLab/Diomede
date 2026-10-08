from __future__ import annotations

import asyncio
import os
import time
from typing import TypedDict

import httpx
from dotenv import load_dotenv

from src.edge.orthanc_source import OrthancSource
from src.edge.transport import DicomSource
from src.utils.env import require_env
from src.utils.logging_config import get_logger

log = get_logger(__name__, "FORWARDER")
load_dotenv()

ORCHESTRATOR_BASE = require_env("ORCHESTRATOR_BASE")
ORCH_URL = f"{ORCHESTRATOR_BASE}/get-best-node"
ORCH_HEARTBEAT_URL = f"{ORCHESTRATOR_BASE}/heartbeat"
ORCH_API_KEY = require_env("ORCHESTRATOR_API_KEY")
AGENT_ID = require_env("AGENT_ID")
POLL_INTERVAL_S = int(require_env("FORWARDER_POLL_INTERVAL_S"))
PROBE_INTERVAL_S = int(require_env("PROBE_INTERVAL_S"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE")

_VERIFY = CA_CERT if CA_CERT else True


class _NodeCfg(TypedDict):
    base: str
    auth: tuple[str, str]


CLOUD_NODES: dict[str, _NodeCfg] = {
    require_env("REGION1_NAME"): {
        "base": require_env("NODE_US_BASE"),
        "auth": (require_env("NODE_US_USER"), require_env("NODE_US_PASS")),
    },
    require_env("REGION2_NAME"): {
        "base": require_env("NODE_EU_BASE"),
        "auth": (require_env("NODE_EU_USER"), require_env("NODE_EU_PASS")),
    },
    require_env("REGION3_NAME"): {
        "base": require_env("NODE_ASIA_BASE"),
        "auth": (require_env("NODE_ASIA_USER"), require_env("NODE_ASIA_PASS")),
    },
    require_env("REGION4_NAME"): {
        "base": require_env("NODE_AF_BASE"),
        "auth": (require_env("NODE_AF_USER"), require_env("NODE_AF_PASS")),
    },
}


_pending_ack: set[str] = set()


def _orch_headers() -> dict[str, str]:
    return {"X-API-Key": ORCH_API_KEY}


async def _acknowledge(client: httpx.AsyncClient, source: DicomSource, instance_id: str) -> None:
    try:
        await source.acknowledge(client, instance_id)
    except Exception as exc:
        log.warning("instance=%s acknowledge failed: %s", instance_id, exc)
        _pending_ack.add(instance_id)
    else:
        _pending_ack.discard(instance_id)


async def route_study(
    client: httpx.AsyncClient,
    source: DicomSource,
    study_id: str,
    instance_ids: list[str],
) -> None:
    """Forward every instance of one study to a single node and delete the local copies.

    The orchestrator pins the study to one node, so instances left on the edge after a
    failure follow the same node on the next poll (or a new one if it became unhealthy).
    """

    # Already forwarded: only retry the delete, never upload twice.
    to_forward: list[str] = []
    for instance_id in instance_ids:
        if instance_id in _pending_ack:
            await _acknowledge(client, source, instance_id)
        else:
            to_forward.append(instance_id)
    if not to_forward:
        return

    # 1. Ask the Orchestrator for the study's destination (before downloading anything).
    try:
        best_resp = await client.get(
            ORCH_URL,
            params={"agent_id": AGENT_ID, "study_id": study_id},
            headers=_orch_headers(),
            timeout=5,
        )
        best_resp.raise_for_status()
        best = best_resp.json()
    except Exception as exc:
        log.error("study=%s orchestrator query failed: %s", study_id, exc)
        return

    node_id = best.get("node_id")
    if not node_id:
        log.error("study=%s orchestrator response missing 'node_id'", study_id)
        return

    node_cfg = CLOUD_NODES.get(node_id)
    if not node_cfg:
        log.error("study=%s unknown node_id '%s' from orchestrator", study_id, node_id)
        return

    if best.get("rerouted"):
        log.warning(
            "study=%s re-pinned to %s; its earlier node became unavailable", study_id, node_id
        )

    for i, instance_id in enumerate(to_forward):
        # 2. Stream the DICOM straight from the edge buffer to the cloud node so the file
        #    is never fully buffered in memory (safe for arbitrarily large instances).
        try:
            async with source.open_stream(client, instance_id) as body:
                post_resp = await client.post(
                    f"{node_cfg['base']}/instances",
                    content=body,
                    headers={"Content-Type": "application/dicom"},
                    auth=node_cfg["auth"],
                    timeout=120,
                )
                post_resp.raise_for_status()
        except Exception as exc:
            # Leave this and the remaining instances on the edge for the next poll.
            log.error(
                "study=%s instance=%s forward to %s failed: %s (%d instances left on edge)",
                study_id,
                instance_id,
                node_id,
                exc,
                len(to_forward) - i,
            )
            return

        log.info(
            "study=%s instance=%s routed -> %s (score=%.4f)",
            study_id,
            instance_id,
            node_id,
            best.get("score") or 0,
        )

        # 3. Acknowledge (delete local copy) only after a confirmed successful forward.
        await _acknowledge(client, source, instance_id)


async def forward_loop(source: DicomSource) -> None:
    """Poll the DicomSource every POLL_INTERVAL_S seconds and route new studies."""
    while True:
        try:
            async with httpx.AsyncClient(verify=_VERIFY) as client:
                studies = await source.poll_new(client)
                _pending_ack.intersection_update(
                    instance_id for ids in studies.values() for instance_id in ids
                )
                for study_id, instance_ids in studies.items():
                    await route_study(client, source, study_id, instance_ids)
        except Exception as exc:
            log.warning("forward_loop error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_S)


async def latency_probe_loop() -> None:
    """GET /system on each cloud node once per hour, report RTT to /heartbeat."""
    while True:
        rtt_dict: dict[str, float] = {}
        async with httpx.AsyncClient(verify=_VERIFY) as client:
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
                        json={"agent_id": AGENT_ID, "rtt_dict": rtt_dict},
                        headers=_orch_headers(),
                        timeout=5,
                    )
                except Exception as exc:
                    log.warning("heartbeat failed: %s", exc)

        await asyncio.sleep(PROBE_INTERVAL_S)


async def run(source: DicomSource | None = None) -> None:
    if source is None:
        source = OrthancSource()
    await asyncio.gather(
        forward_loop(source),
        latency_probe_loop(),
    )


if __name__ == "__main__":
    asyncio.run(run())
