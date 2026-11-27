from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv

from src.edge.destinations import (
    VALID_TRANSPORTS,
    StorageCommitmentError,
    StowRsRejectedError,
    forward_dimse_study,
    forward_rest_instance,
    forward_stow_rs_instance,
    probe_dimse_echo,
)
from src.edge.orthanc_source import OrthancSource
from src.edge.transport import DicomSource
from src.utils.env import require_env
from src.utils.logging_config import get_logger
from src.utils.nodes import NodeConfig, load_nodes

log = get_logger(__name__, "FORWARDER")
load_dotenv()

ORCHESTRATOR_BASE = require_env("ORCHESTRATOR_BASE")
ORCH_URL = f"{ORCHESTRATOR_BASE}/get-best-node"
ORCH_HEARTBEAT_URL = f"{ORCHESTRATOR_BASE}/heartbeat"
ORCH_TRANSFER_START_URL = f"{ORCHESTRATOR_BASE}/transfers/start"
ORCH_TRANSFER_FINISH_URL = f"{ORCHESTRATOR_BASE}/transfers/finish"
ORCH_STUDY_COMMIT_URL_TMPL = f"{ORCHESTRATOR_BASE}/studies/{{study_id}}/commit"
ORCH_API_KEY = require_env("ORCHESTRATOR_API_KEY")
AGENT_ID = require_env("AGENT_ID")
POLL_INTERVAL_S = int(require_env("FORWARDER_POLL_INTERVAL_S"))
PROBE_INTERVAL_S = int(require_env("PROBE_INTERVAL_S"))
MAX_UPLOADS_PER_NODE = int(os.getenv("FORWARDER_MAX_UPLOADS_PER_NODE", "4"))
if MAX_UPLOADS_PER_NODE < 1:
    raise ValueError("FORWARDER_MAX_UPLOADS_PER_NODE must be at least 1")
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE")

# Pluggable edge-to-cloud transport: "rest" (default), "stow-rs", or "dimse"
FORWARDER_TRANSPORT = os.getenv("FORWARDER_TRANSPORT", "rest").strip().lower()
if FORWARDER_TRANSPORT not in VALID_TRANSPORTS:
    raise ValueError(
        f"FORWARDER_TRANSPORT must be one of {VALID_TRANSPORTS}, got {FORWARDER_TRANSPORT!r}"
    )

# When true in dimse mode, wait for DICOM Storage Commitment (N-ACTION / N-EVENT-REPORT)
# before deleting local edge copies.
STORAGE_COMMITMENT = os.getenv("FORWARDER_STORAGE_COMMITMENT", "true").lower() == "true"

# When true, hold all instances of a study on the edge until the entire study succeeds
# on a single destination node. If failover occurs mid-study, replay from instance 0 so
# the replacement node receives a complete, unsplit study.
STUDY_ATOMIC_COMMIT = os.getenv("FORWARDER_STUDY_ATOMIC_COMMIT", "false").lower() == "true"

# When true, report active transfer start/finish leases and study commits to orchestrator.
REPORT_INFLIGHT = os.getenv("FORWARDER_REPORT_INFLIGHT", "false").lower() == "true"

# Latency probe mode: "http" (default), "dimse" (C-ECHO), or "hybrid" (both).
PROBE_MODE = os.getenv("FORWARDER_PROBE_MODE", "http").strip().lower()

# Optional data-residency constraints applied on every /get-best-node request.
ALLOWED_REGIONS = [
    r.strip() for r in os.getenv("FORWARDER_ALLOWED_REGIONS", "").split(",") if r.strip()
]
JURISDICTIONS = [
    j.strip() for j in os.getenv("FORWARDER_JURISDICTION", "").split(",") if j.strip()
]

# Optional fallback node used ONLY if the orchestrator is unreachable (default unset = buffer).
FALLBACK_NODE = os.getenv("FORWARDER_FALLBACK_NODE", "").strip() or None

# Co-located Edge Orthanc coordinates (needed by DIMSE C-STORE / C-ECHO / Storage Commitment).
EDGE_BASE = os.getenv("EDGE_BASE", "https://localhost:8042").rstrip("/")
EDGE_AUTH: tuple[str, str] = (
    os.getenv("EDGE_USER", "orthanc"),
    os.getenv("EDGE_PASS", "orthanc"),
)

_VERIFY = CA_CERT if CA_CERT else True


CLOUD_NODES = load_nodes()


# Nodes whose last latency probe failed. The orchestrator scores a node with no measurement at a
# default RTT, so a node left out of the heartbeat could still win; instead it is excluded from
# routing until a probe succeeds or an upload to it does.
_probe_failed: set[str] = set()

_pending_ack: set[str] = set()

# Observed upload throughput EWMA per node (MB/s), reported alongside latency heartbeats.
_node_throughput_mbps: dict[str, float] = {}

# Upload statuses that mean the node will never accept this particular file, unlike 401/404/429.
_REJECTED_STATUSES = {400, 413, 415, 422}
_REJECTED_RETRY_S = 3600
_rejected: dict[str, float] = {}

# A node that failed its latency probe is probed again after this long, not PROBE_INTERVAL_S later.
_PROBE_RETRY_S = 60

# Limited per node, so a node that stops answering only ties up its own uploads.
_node_slots: dict[str, asyncio.Semaphore] = {}


def _orch_headers() -> dict[str, str]:
    return {"X-API-Key": ORCH_API_KEY}


def _update_throughput_ewma(node_id: str, bytes_sent: int, elapsed_s: float) -> None:
    if bytes_sent <= 0 or elapsed_s <= 0:
        return
    mbps = (bytes_sent / (1024.0 * 1024.0)) / elapsed_s
    prev = _node_throughput_mbps.get(node_id)
    _node_throughput_mbps[node_id] = round(mbps if prev is None else 0.3 * mbps + 0.7 * prev, 2)


async def _notify_transfer(
    client: httpx.AsyncClient, url: str, node_id: str, study_id: str
) -> None:
    if not REPORT_INFLIGHT:
        return
    try:
        resp = await client.post(
            url,
            json={"agent_id": AGENT_ID, "node_id": node_id, "study_id": study_id},
            headers=_orch_headers(),
            timeout=3,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.debug("study=%s transfer lease notification failed (%s): %s", study_id, url, exc)


async def _notify_study_commit(
    client: httpx.AsyncClient,
    study_id: str,
    node_id: str,
    transport: str,
    storage_committed: bool,
    instance_count: int,
) -> None:
    if not REPORT_INFLIGHT:
        return
    try:
        resp = await client.post(
            ORCH_STUDY_COMMIT_URL_TMPL.format(study_id=study_id),
            json={
                "agent_id": AGENT_ID,
                "node_id": node_id,
                "transport": transport,
                "storage_committed": storage_committed,
                "instance_count": instance_count,
            },
            headers=_orch_headers(),
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.debug("study=%s commit index update failed: %s", study_id, exc)


async def _acknowledge(client: httpx.AsyncClient, source: DicomSource, instance_id: str) -> None:
    try:
        await source.acknowledge(client, instance_id)
    except Exception as exc:
        log.warning("instance=%s acknowledge failed: %s", instance_id, exc)
        _pending_ack.add(instance_id)
    else:
        _pending_ack.discard(instance_id)


async def _route_study_dimse(
    client: httpx.AsyncClient,
    source: DicomSource,
    study_id: str,
    to_forward: list[str],
    dest: tuple[str, NodeConfig, dict[str, Any]],
) -> None:
    """Forward a complete study via native DIMSE-TLS C-STORE + optional Storage Commitment."""
    node_id, _node_cfg, best = dest
    failed_nodes: list[str] = []

    while True:
        slots = _node_slots.setdefault(node_id, asyncio.Semaphore(MAX_UPLOADS_PER_NODE))
        await _notify_transfer(client, ORCH_TRANSFER_START_URL, node_id, study_id)
        try:
            async with slots:
                committed = await forward_dimse_study(
                    client=client,
                    edge_base=EDGE_BASE,
                    edge_auth=EDGE_AUTH,
                    node_id=node_id,
                    instance_ids=to_forward,
                    require_storage_commitment=STORAGE_COMMITMENT,
                )
        except (httpx.HTTPError, StorageCommitmentError) as exc:
            await _notify_transfer(client, ORCH_TRANSFER_FINISH_URL, node_id, study_id)
            failed_nodes.append(node_id)
            log.warning(
                "study=%s DIMSE forward/commit to %s failed (%s); asking for another node",
                study_id,
                node_id,
                exc,
            )
            next_dest = await _pick_destination(client, study_id, failed=set(failed_nodes))
            if next_dest is not None and next_dest[0] not in failed_nodes:
                node_id, _node_cfg, best = next_dest
                continue
            log.error(
                "study=%s DIMSE forward to %s failed: %s (%d instances left on edge)",
                study_id,
                node_id,
                exc,
                len(to_forward),
            )
            return
        except Exception as exc:
            await _notify_transfer(client, ORCH_TRANSFER_FINISH_URL, node_id, study_id)
            log.error(
                "study=%s DIMSE forward to %s failed: %s (%d instances left on edge)",
                study_id,
                node_id,
                exc,
                len(to_forward),
            )
            return
        else:
            await _notify_transfer(client, ORCH_TRANSFER_FINISH_URL, node_id, study_id)
            _probe_failed.discard(node_id)
            log.info(
                "study=%s instances=%d routed via dimse -> %s (committed=%s score=%.4f)",
                study_id,
                len(to_forward),
                node_id,
                committed,
                best.get("score") or 0,
            )
            await _notify_study_commit(
                client,
                study_id=study_id,
                node_id=node_id,
                transport="dimse",
                storage_committed=committed,
                instance_count=len(to_forward),
            )
            for instance_id in to_forward:
                await _acknowledge(client, source, instance_id)
            return


async def route_study(
    client: httpx.AsyncClient,
    source: DicomSource,
    study_id: str,
    instance_ids: list[str],
) -> None:
    """Forward every instance of one study to a single node and delete the local copies.

    The orchestrator pins the study to one node, so instances left on the edge after a
    failure follow the same node on the next poll (or a new one if it became unhealthy).
    When ``STUDY_ATOMIC_COMMIT`` is enabled (or when using ``dimse`` transport), local
    copies are retained until every instance of the study succeeds on a single node,
    eliminating split studies on mid-transfer failover.
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

    if FORWARDER_TRANSPORT == "dimse":
        await _route_study_dimse(client, source, study_id, to_forward, dest)
        return

    node_id, node_cfg, best = dest
    failed_nodes: list[str] = []
    deferred_ack: list[str] = []

    await _notify_transfer(client, ORCH_TRANSFER_START_URL, node_id, study_id)
    try:
        i = 0
        while i < len(to_forward):
            instance_id = to_forward[i]
            # 2. Stream the DICOM straight from the edge buffer to the cloud node so the file
            #    is never fully buffered in memory (safe for arbitrarily large instances).
            slots = _node_slots.setdefault(node_id, asyncio.Semaphore(MAX_UPLOADS_PER_NODE))
            t0 = time.monotonic()
            try:
                async with slots:
                    if FORWARDER_TRANSPORT == "stow-rs":
                        bytes_sent = await forward_stow_rs_instance(
                            client, source, instance_id, node_cfg
                        )
                    else:
                        bytes_sent = await forward_rest_instance(
                            client, source, instance_id, node_cfg
                        )
            except Exception as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                if status in _REJECTED_STATUSES or isinstance(exc, StowRsRejectedError):
                    _rejected[instance_id] = time.monotonic() + _REJECTED_RETRY_S
                    log.error(
                        "study=%s instance=%s rejected by %s (%s); kept on the edge, retrying "
                        "in %ds, forwarding the rest of the study",
                        study_id,
                        instance_id,
                        node_id,
                        f"HTTP {status}" if status is not None else str(exc),
                        _REJECTED_RETRY_S,
                    )
                    i += 1
                    continue
                if _node_failed(exc, node_cfg["base"]):
                    await _notify_transfer(client, ORCH_TRANSFER_FINISH_URL, node_id, study_id)
                    failed_nodes.append(node_id)
                    log.warning(
                        "study=%s upload to %s failed (%s); asking for another node",
                        study_id,
                        node_id,
                        exc,
                    )
                    next_dest = await _pick_destination(client, study_id, failed=set(failed_nodes))
                    if next_dest is not None and next_dest[0] not in failed_nodes:
                        node_id, node_cfg, best = next_dest
                        await _notify_transfer(client, ORCH_TRANSFER_START_URL, node_id, study_id)
                        if STUDY_ATOMIC_COMMIT:
                            # Replay all instances from 0 onto the replacement node so the
                            # study is never split across regional endpoints.
                            deferred_ack.clear()
                            i = 0
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

            _update_throughput_ewma(node_id, bytes_sent, time.monotonic() - t0)
            _probe_failed.discard(node_id)
            log.info(
                "study=%s instance=%s routed via %s -> %s (score=%.4f)",
                study_id,
                instance_id,
                FORWARDER_TRANSPORT,
                node_id,
                best.get("score") or 0,
            )

            # 3. Acknowledge immediately (per-instance mode) or defer until full study commits.
            if STUDY_ATOMIC_COMMIT:
                deferred_ack.append(instance_id)
            else:
                await _acknowledge(client, source, instance_id)
            i += 1

        if STUDY_ATOMIC_COMMIT and deferred_ack:
            await _notify_study_commit(
                client,
                study_id=study_id,
                node_id=node_id,
                transport=FORWARDER_TRANSPORT,
                storage_committed=False,
                instance_count=len(deferred_ack),
            )
            for instance_id in deferred_ack:
                await _acknowledge(client, source, instance_id)
        elif not STUDY_ATOMIC_COMMIT:
            await _notify_study_commit(
                client,
                study_id=study_id,
                node_id=node_id,
                transport=FORWARDER_TRANSPORT,
                storage_committed=False,
                instance_count=len(to_forward),
            )
    finally:
        await _notify_transfer(client, ORCH_TRANSFER_FINISH_URL, node_id, study_id)


async def _pick_destination(
    client: httpx.AsyncClient, study_id: str, failed: set[str]
) -> tuple[str, NodeConfig, dict[str, Any]] | None:
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
) -> tuple[str, NodeConfig, dict[str, Any]] | None:
    """Ask the orchestrator where the study goes, skipping nodes in exclude."""
    params: dict[str, Any] = {"agent_id": AGENT_ID, "study_id": study_id, "exclude": exclude}
    if ALLOWED_REGIONS:
        params["allowed_regions"] = ALLOWED_REGIONS
    if JURISDICTIONS:
        params["jurisdiction"] = JURISDICTIONS
    try:
        best_resp = await client.get(
            ORCH_URL,
            params=params,
            headers=_orch_headers(),
            timeout=5,
        )
        best_resp.raise_for_status()
        best: dict[str, Any] = best_resp.json()
    except Exception as exc:
        log.error("study=%s orchestrator query failed: %s", study_id, exc)
        if (
            FALLBACK_NODE
            and FALLBACK_NODE not in exclude
            and FALLBACK_NODE in CLOUD_NODES
            and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout))
        ):
            log.warning(
                "study=%s orchestrator unreachable; degrading to configured fallback node %s",
                study_id,
                FALLBACK_NODE,
            )
            return FALLBACK_NODE, CLOUD_NODES[FALLBACK_NODE], {"node_id": FALLBACK_NODE, "score": 0}
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
    """Probe each cloud node over HTTP (/system) and optionally DIMSE C-ECHO, reporting to
    /heartbeat.

    A node that cannot be probed is left out of the report and excluded from routing, and the
    probes are repeated after _PROBE_RETRY_S until it answers.
    """
    while True:
        rtt_dict: dict[str, float] = {}
        dicom_rtt_dict: dict[str, float] = {}
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
                    log.info("node=%-15s http_rtt=%.1f ms", node_id, rtt_ms)
                except Exception as exc:
                    _probe_failed.add(node_id)
                    log.warning(
                        "node=%-15s probe failed: %s; excluding it from routing", node_id, exc
                    )
                    continue

                if PROBE_MODE in ("dimse", "hybrid"):
                    try:
                        d_rtt = await probe_dimse_echo(client, EDGE_BASE, EDGE_AUTH, node_id)
                        dicom_rtt_dict[node_id] = d_rtt
                        log.info("node=%-15s dimse_echo_rtt=%.1f ms", node_id, d_rtt)
                    except Exception as exc:
                        log.warning("node=%-15s DIMSE C-ECHO probe failed: %s", node_id, exc)

            if rtt_dict:
                payload: dict[str, Any] = {"agent_id": AGENT_ID, "rtt_dict": rtt_dict}
                if dicom_rtt_dict:
                    payload["dicom_rtt_dict"] = dicom_rtt_dict
                    payload["probe_mode"] = PROBE_MODE
                if _node_throughput_mbps:
                    payload["throughput_mbps_dict"] = {
                        nid: mbps
                        for nid, mbps in _node_throughput_mbps.items()
                        if nid in CLOUD_NODES
                    }
                try:
                    hb_resp = await client.post(
                        ORCH_HEARTBEAT_URL,
                        json=payload,
                        headers=_orch_headers(),
                        timeout=5,
                    )
                    hb_resp.raise_for_status()
                except Exception as exc:
                    log.warning("heartbeat failed: %s", exc)

        await asyncio.sleep(
            min(PROBE_INTERVAL_S, _PROBE_RETRY_S) if _probe_failed else PROBE_INTERVAL_S
        )


async def run(source: DicomSource | None = None) -> None:
    if source is None:
        source = OrthancSource()
    await asyncio.gather(
        forward_loop(source),
        latency_probe_loop(),
    )


if __name__ == "__main__":
    asyncio.run(run())
