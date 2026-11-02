from __future__ import annotations

import asyncio
import os
import time
from typing import Any, TypedDict

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
MAX_UPLOADS_PER_NODE = int(os.getenv("FORWARDER_MAX_UPLOADS_PER_NODE", "4"))
if MAX_UPLOADS_PER_NODE < 1:
    raise ValueError("FORWARDER_MAX_UPLOADS_PER_NODE must be at least 1")
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


# Nodes whose last latency probe failed. The orchestrator scores a node with no measurement at a
# default RTT, so a node left out of the heartbeat could still win; instead it is excluded from
# routing until a probe succeeds or an upload to it does.
_probe_failed: set[str] = set()

_pending_ack: set[str] = set()

# Upload statuses that mean the node will never accept this particular file, unlike 401/404/429.
_REJECTED_STATUSES = {400, 413, 415, 422}
_REJECTED_RETRY_S = 3600
_rejected: dict[str, float] = {}

# Limited per node, so a node that stops answering only ties up its own uploads.
_node_slots: dict[str, asyncio.Semaphore] = {}


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
        elif _rejected.get(instance_id, 0.0) <= time.monotonic():
            to_forward.append(instance_id)
    if not to_forward:
        return

    # 1. Ask the Orchestrator for the study's destination (before downloading anything).
    dest = await _pick_destination(client, study_id, failed=set())
    if dest is None:
        return
    node_id, node_cfg, best = dest
    failed_nodes: list[str] = []

    i = 0
    while i < len(to_forward):
        instance_id = to_forward[i]
        # 2. Stream the DICOM straight from the edge buffer to the cloud node so the file
        #    is never fully buffered in memory (safe for arbitrarily large instances).
        slots = _node_slots.setdefault(node_id, asyncio.Semaphore(MAX_UPLOADS_PER_NODE))
        try:
            async with slots, source.open_stream(client, instance_id) as body:
                post_resp = await client.post(
                    f"{node_cfg['base']}/instances",
                    content=body,
                    headers={"Content-Type": "application/dicom"},
                    auth=node_cfg["auth"],
                    timeout=120,
                )
                post_resp.raise_for_status()
        except Exception as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if status in _REJECTED_STATUSES:
                _rejected[instance_id] = time.monotonic() + _REJECTED_RETRY_S
                log.error(
                    "study=%s instance=%s rejected by %s (HTTP %d); kept on the edge, retrying "
                    "in %ds, forwarding the rest of the study",
                    study_id,
                    instance_id,
                    node_id,
                    status,
                    _REJECTED_RETRY_S,
                )
                i += 1
                continue
            if _node_failed(exc, node_cfg["base"]):
                failed_nodes.append(node_id)
                log.warning(
                    "study=%s upload to %s failed (%s); asking for another node",
                    study_id,
                    node_id,
                    exc,
                )
                dest = await _pick_destination(client, study_id, failed=set(failed_nodes))
                if dest is not None and dest[0] not in failed_nodes:
                    node_id, node_cfg, best = dest
                    continue
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

        _probe_failed.discard(node_id)
        log.info(
            "study=%s instance=%s routed -> %s (score=%.4f)",
            study_id,
            instance_id,
            node_id,
            best.get("score") or 0,
        )

        # 3. Acknowledge (delete local copy) only after a confirmed successful forward.
        await _acknowledge(client, source, instance_id)
        i += 1


async def _pick_destination(
    client: httpx.AsyncClient, study_id: str, failed: set[str]
) -> tuple[str, _NodeCfg, dict[str, Any]] | None:
    """Ask where the study goes, skipping nodes that failed an upload and, if another node
    is left, nodes that failed their latency probe.

    A node that failed its probe is only avoided, not forbidden: when excluding it leaves no
    node, ask again without it excluded rather than stop forwarding. Both the first request and
    the failover after a failed upload go through here, so the fallback applies to both.
    """
    dest = await _destination(client, study_id, exclude=sorted(_probe_failed | failed))
    if dest is None and _probe_failed - failed:
        dest = await _destination(client, study_id, exclude=sorted(failed))
    return dest


async def _destination(
    client: httpx.AsyncClient, study_id: str, exclude: list[str]
) -> tuple[str, _NodeCfg, dict[str, Any]] | None:
    """Ask the orchestrator where the study goes, skipping nodes in exclude."""
    try:
        best_resp = await client.get(
            ORCH_URL,
            params={"agent_id": AGENT_ID, "study_id": study_id, "exclude": exclude},
            headers=_orch_headers(),
            timeout=5,
        )
        best_resp.raise_for_status()
        best: dict[str, Any] = best_resp.json()
    except Exception as exc:
        log.error("study=%s orchestrator query failed: %s", study_id, exc)
        return None

    node_id = best.get("node_id")
    if not node_id:
        log.error("study=%s orchestrator response missing 'node_id'", study_id)
        return None

    node_cfg = CLOUD_NODES.get(node_id)
    if not node_cfg:
        log.error("study=%s unknown node_id '%s' from orchestrator", study_id, node_id)
        return None

    if best.get("rerouted"):
        log.warning(
            "study=%s re-pinned to %s; its earlier node became unavailable", study_id, node_id
        )
    return node_id, node_cfg, best


def _node_failed(exc: Exception, node_base: str) -> bool:
    """True when the cloud node refused the upload or could not be reached.

    Timeouts after sending, and 502/504 from a proxy in front of the node, are left out:
    the node may have stored the file, and sending it elsewhere would leave a copy on both.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (502, 504):
            return False
    elif not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    try:
        return exc.request.url.netloc == httpx.URL(node_base).netloc
    except RuntimeError:
        return False


async def forward_loop(source: DicomSource) -> None:
    """Poll the DicomSource every POLL_INTERVAL_S seconds and route new studies.

    Studies are routed side by side, so a slow node only holds up its own studies.
    A study that is still uploading is skipped by later polls, so no instance is
    sent twice.
    """
    in_flight: set[str] = set()

    async def _route(client: httpx.AsyncClient, study_id: str, instance_ids: list[str]) -> None:
        try:
            await route_study(client, source, study_id, instance_ids)
        except Exception as exc:
            log.warning("study=%s routing error: %s", study_id, exc)
        finally:
            in_flight.discard(study_id)

    async with httpx.AsyncClient(verify=_VERIFY) as client, asyncio.TaskGroup() as tasks:
        while True:
            try:
                studies = await source.poll_new(client)
                buffered = {instance_id for ids in studies.values() for instance_id in ids}
                _pending_ack.intersection_update(buffered)
                for instance_id in _rejected.keys() - buffered:
                    del _rejected[instance_id]
                for study_id, instance_ids in studies.items():
                    if study_id not in in_flight:
                        in_flight.add(study_id)
                        tasks.create_task(_route(client, study_id, instance_ids))
            except Exception as exc:
                log.warning("forward_loop error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_S)


async def latency_probe_loop() -> None:
    """GET /system on each cloud node once per hour, report RTT to /heartbeat.

    A node that cannot be probed is left out of the report and excluded from routing.
    """
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
                    _probe_failed.discard(node_id)
                    log.info("node=%-15s rtt=%.1f ms", node_id, rtt_ms)
                except Exception as exc:
                    _probe_failed.add(node_id)
                    log.warning(
                        "node=%-15s probe failed: %s; excluding it from routing", node_id, exc
                    )

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
