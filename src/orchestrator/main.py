"""
FastAPI app for the Diomede orchestrator.

Exposes control-plane endpoints that read live node telemetry and active in-flight
ingestion counts from Redis, enforce optional data-residency routing policies, and
return the best healthy destination for a given edge agent.

When the caller passes a study_id, the first decision for that study is pinned in
Redis (study:{study_id}) so every later instance of the study goes to the same node.
The study is only moved if its pinned node becomes unhealthy, falls outside policy,
or drops out of telemetry. A durable study location index (study_loc:{study_id})
tracks where committed studies reside for cross-node query and retrieval.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, WatchError

from src.utils.env import require_env
from src.utils.logging_config import get_logger

from .baseline_scorers import LeastConnectionsScorer, RoundRobinScorer  # noqa: F401
from .daemon import NODES
from .scorer import get_scorer
from .weighted_scorer import WeightedScorer  # noqa: F401 to trigger self-registration

log = get_logger(__name__, "ORCHESTRATOR")
load_dotenv()

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)
API_KEY = require_env("ORCHESTRATOR_API_KEY")

# How long a study stays pinned to its node after its last routing request.
STUDY_AFFINITY_TTL_S = int(os.getenv("STUDY_AFFINITY_TTL_S", "3600"))

# How long committed study location entries stay in the study index (default 30 days).
STUDY_INDEX_TTL_S = int(os.getenv("STUDY_INDEX_TTL_S", str(30 * 24 * 3600)))

# Lease TTL for active in-flight upload counters so crashed edges self-expire.
INFLIGHT_TTL_S = int(os.getenv("INFLIGHT_TTL_S", "180"))

# Attempts to settle a study's pin when other agents keep changing it concurrently.
_PIN_ATTEMPTS = 3

# How long an agent's RTT measurements stay valid without a new heartbeat. Must exceed
# the edge PROBE_INTERVAL_S (default 3600 s) so a healthy agent never loses its RTTs.
RTT_TTL_S = int(os.getenv("RTT_TTL_S", "10800"))

# Which RTT telemetry source to prefer when scoring: "auto" (DICOM C-ECHO if available,
# otherwise HTTP /system), "dicom", or "http".
RTT_SOURCE = os.getenv("RTT_SOURCE", "auto").strip().lower()


def _load_agent_policies() -> dict[str, dict[str, list[str]]]:
    """Parse optional AGENT_POLICIES JSON mapping agent_id -> policy constraints.

    Example:
        AGENT_POLICIES='{"agent-us": {"jurisdictions": ["US"]},
                         "agent-eu": {"allowed_regions": ["eu-west1"]}}'
    """
    raw = os.getenv("AGENT_POLICIES", "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AGENT_POLICIES must be a JSON object mapping agent_id to policy rules")
    return cast(dict[str, dict[str, list[str]]], parsed)


AGENT_POLICIES: dict[str, dict[str, list[str]]] = _load_agent_policies()


def validate_api_key(api_key_str: str = Security(api_key_header)) -> str:
    # Constant-time comparison, on bytes because compare_digest rejects non-ASCII str.
    if not secrets.compare_digest(api_key_str.encode(), API_KEY.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return api_key_str


class NodeResponse(BaseModel):
    node_id: str
    ae_title: str
    base_url: str
    queue_size: int | None = None
    inflight_uploads: int = 0
    disk_free_mb: float | None = None
    disk_total_mb: int | None = None
    instance_count: int | None = None
    jurisdiction: str | None = None
    healthy: bool
    ts: str


class BestNodeResponse(NodeResponse):
    rtt_ms: float | None = None
    dicom_rtt_ms: float | None = None
    throughput_mbps: float | None = None
    score: float | None = None
    study_id: str | None = None
    rerouted: bool = False


class TransferLeasePayload(BaseModel):
    agent_id: str
    node_id: str
    study_id: str

    @field_validator("node_id")
    @classmethod
    def node_id_valid(cls, v: str) -> str:
        if v not in NODES:
            raise ValueError(f"Invalid node id: {v!r}")
        return v


class StudyCommitPayload(BaseModel):
    agent_id: str
    node_id: str
    transport: str = "rest"
    storage_committed: bool = False
    instance_count: int | None = None

    @field_validator("node_id")
    @classmethod
    def commit_node_valid(cls, v: str) -> str:
        if v not in NODES:
            raise ValueError(f"Invalid node id: {v!r}")
        return v


class StudyLocationResponse(BaseModel):
    study_id: str
    node_id: str
    ae_title: str
    base_url: str
    jurisdiction: str | None = None
    transport: str = "rest"
    storage_committed: bool = False
    instance_count: int | None = None
    updated_at: str


# {"agent_id": "agent-001", "rtt_dict": {"us-east1": 45.0, ...}}
class HeartbeatPayload(BaseModel):
    agent_id: str
    rtt_dict: dict[str, float]
    dicom_rtt_dict: dict[str, float] | None = None
    throughput_mbps_dict: dict[str, float] | None = None
    probe_mode: str | None = None

    @field_validator("rtt_dict")
    @classmethod
    def rtt_must_be_positive(cls, v: dict[str, float]) -> dict[str, float]:
        for node_id, rtt in v.items():
            if not math.isfinite(rtt) or rtt <= 0:
                raise ValueError(f"rtt_ms for {node_id!r} must be a positive number, got {rtt}")
        return v

    @field_validator("rtt_dict")
    @classmethod
    def node_id_must_be_valid(cls, v: dict[str, float]) -> dict[str, float]:
        for node_id in v.keys():
            if node_id not in NODES.keys():
                raise ValueError(f"Invalid node id: {node_id!r}")
        return v

    @field_validator("dicom_rtt_dict")
    @classmethod
    def dicom_rtt_must_be_valid(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        for node_id, rtt in v.items():
            if node_id not in NODES:
                raise ValueError(f"Invalid node id: {node_id!r}")
            if not math.isfinite(rtt) or rtt <= 0:
                raise ValueError(
                    f"dicom_rtt_ms for {node_id!r} must be a positive number, got {rtt}"
                )
        return v

    @field_validator("throughput_mbps_dict")
    @classmethod
    def throughput_must_be_valid(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        for node_id, mbps in v.items():
            if node_id not in NODES:
                raise ValueError(f"Invalid node id: {node_id!r}")
            if not math.isfinite(mbps) or mbps <= 0:
                raise ValueError(
                    f"throughput_mbps for {node_id!r} must be a positive number, got {mbps}"
                )
        return v


REDIS_URL = require_env("REDIS_URL")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _redis
    get_scorer()  # fail at startup, not on the first request, if the scorer config is invalid
    # from_url() skips Redis()'s default retry, so a connection closed by a Redis restart
    # would fail its next command instead of reconnecting.
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True, retry=Retry(NoBackoff(), 1))
    yield
    if _redis:
        await _redis.aclose()


app = FastAPI(title="Diomede Orchestrator", lifespan=lifespan)
_redis: aioredis.Redis | None = None


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, minus input values JSON can't represent (NaN, Infinity)."""
    errors = jsonable_encoder(exc.errors())
    for error in errors:
        try:
            json.dumps(error.get("input"), allow_nan=False)
        except ValueError:
            del error["input"]
    return JSONResponse(status_code=422, content={"detail": errors})


async def _get_inflight_counts(node_ids: list[str]) -> dict[str, int]:
    """Return active (unexpired) in-flight transfer counts per node from Redis."""
    if _redis is None or not node_ids:
        return {}
    now = time.time()
    try:
        async with _redis.pipeline(transaction=False) as pipe:
            for nid in node_ids:
                pipe.zcount(f"inflight:{nid}", now, "+inf")
            counts = await pipe.execute()
    except RedisError:
        return {nid: 0 for nid in node_ids}
    return {nid: int(cnt or 0) for nid, cnt in zip(node_ids, counts, strict=True)}


async def _get_nodes() -> list[dict[str, Any]]:
    """Gets all available telemetry nodes from Redis, enriched with live in-flight counts."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    keys: list[str] = [f"node:{k}" for k in NODES.keys()]
    if not keys:
        raise HTTPException(status_code=503, detail="No node telemetry available")

    try:
        raw = await _redis.mget(*keys)
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    log.info(f"Fetched raw nodes: {raw}")

    nodes = [json.loads(node) for node in raw if node is not None]
    inflight = await _get_inflight_counts([n["node_id"] for n in nodes])
    for node in nodes:
        nid = node["node_id"]
        node["inflight_uploads"] = inflight.get(nid, 0)
        if "jurisdiction" not in node and nid in NODES:
            node["jurisdiction"] = NODES[nid].get("jurisdiction")
    return nodes


def _apply_residency_policy(
    nodes: list[dict[str, Any]],
    agent_id: str,
    allowed_regions: list[str] | None,
    jurisdictions: list[str] | None,
) -> list[dict[str, Any]]:
    """Restrict candidate nodes to those permitted by data-residency policies."""
    policy = AGENT_POLICIES.get(agent_id, {})
    policy_regions = set(policy.get("allowed_regions", []))
    policy_jurisdictions = {j.upper() for j in policy.get("jurisdictions", [])}
    req_regions = set(allowed_regions or [])
    req_jurisdictions = {j.upper() for j in (jurisdictions or [])}

    filtered: list[dict[str, Any]] = []
    for node in nodes:
        nid = node["node_id"]
        node_jur = (node.get("jurisdiction") or "").upper()
        if policy_regions and nid not in policy_regions:
            continue
        if policy_jurisdictions and node_jur not in policy_jurisdictions:
            continue
        if req_regions and nid not in req_regions:
            continue
        if req_jurisdictions and node_jur not in req_jurisdictions:
            continue
        filtered.append(node)
    return filtered


@app.get("/nodes")
async def get_nodes(api_key: str = Depends(validate_api_key)) -> list[NodeResponse]:
    """Return the latest telemetry for all nodes"""
    node_list = await _get_nodes()
    log.info(f"Node list: {node_list}")

    node_responses: list[NodeResponse] = []
    for node in node_list:
        if node is not None:
            node_response = NodeResponse.model_validate(node)
            node_responses.append(node_response)
    return node_responses


@app.get("/get-best-node")
async def get_best_node(
    agent_id: str,
    study_id: str | None = Query(default=None, max_length=128, pattern=r"^[A-Za-z0-9.\-]+$"),
    exclude: Annotated[list[str] | None, Query()] = None,
    allowed_regions: Annotated[list[str] | None, Query()] = None,
    jurisdiction: Annotated[list[str] | None, Query()] = None,
    api_key: str = Depends(validate_api_key),
) -> BestNodeResponse:
    """Return the highest-scoring healthy, policy-compliant node in Redis.

    With study_id, return the node the study is pinned to while it stays healthy and
    policy-compliant. Nodes in exclude are skipped, so a study pinned to one of them is moved.
    """
    node_list = await _get_nodes()

    scorer = get_scorer()
    skip = set(exclude or [])
    healthy = [n for n in node_list if n.get("healthy") is True and n["node_id"] not in skip]
    healthy = _apply_residency_policy(healthy, agent_id, allowed_regions, jurisdiction)
    if not healthy:
        raise HTTPException(status_code=503, detail="No healthy nodes available")

    assert _redis is not None  # checked by _get_nodes()
    try:
        raw_rtt, raw_dicom_rtt, raw_tp = await _redis.mget(
            f"rtt:{agent_id}",
            f"dicom_rtt:{agent_id}",
            f"throughput:{agent_id}",
        )
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    if raw_rtt is None:
        log.warning("No RTT data for agent %s; falling back to default scoring", agent_id)
        agent_rtt: dict[str, float] = {}
    else:
        agent_rtt = json.loads(raw_rtt)
    agent_dicom_rtt: dict[str, float] = json.loads(raw_dicom_rtt) if raw_dicom_rtt else {}
    agent_tp: dict[str, float] = json.loads(raw_tp) if raw_tp else {}

    for node in healthy:
        nid = node["node_id"]
        http_rtt = agent_rtt.get(nid)
        dicom_rtt = agent_dicom_rtt.get(nid)
        if dicom_rtt is not None:
            node["dicom_rtt_ms"] = dicom_rtt
        if nid in agent_tp:
            node["throughput_mbps"] = agent_tp[nid]
        if RTT_SOURCE == "dicom" and dicom_rtt is not None:
            selected_rtt: float | None = dicom_rtt
        elif RTT_SOURCE == "http":
            selected_rtt = http_rtt
        else:
            selected_rtt = dicom_rtt if dicom_rtt is not None else http_rtt
        log.info(f"Node {nid} has RTT {selected_rtt} ms for agent {agent_id}")
        if selected_rtt is not None:
            node["rtt_ms"] = selected_rtt
            log.info(f"Node rtt_ms: {nid} = {node['rtt_ms']}")

    best_node = max(healthy, key=scorer.score)
    if isinstance(scorer, RoundRobinScorer):
        scorer.record_selection(best_node["node_id"])

    if study_id is None:
        return BestNodeResponse.model_validate(best_node)

    try:
        return await _route_study(study_id, best_node, {n["node_id"]: n for n in healthy})
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc


async def _record_study_location(
    redis: aioredis.Redis,
    study_id: str,
    node: dict[str, Any],
    transport: str = "rest",
    storage_committed: bool = False,
    instance_count: int | None = None,
) -> dict[str, Any]:
    """Write or update the durable study location index entry in Redis."""
    entry: dict[str, Any] = {
        "study_id": study_id,
        "node_id": node["node_id"],
        "ae_title": node["ae_title"],
        "base_url": node["base_url"],
        "jurisdiction": node.get("jurisdiction"),
        "transport": transport,
        "storage_committed": storage_committed,
        "instance_count": instance_count,
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    await redis.set(f"study_loc:{study_id}", json.dumps(entry), ex=STUDY_INDEX_TTL_S)
    return entry


async def _route_study(
    study_id: str, best_node: dict[str, Any], healthy: dict[str, dict[str, Any]]
) -> BestNodeResponse:
    """Return the node study_id is pinned to, pinning or re-pinning it to best_node if needed."""
    redis = _redis
    assert redis is not None  # checked by _get_nodes()
    key = f"study:{study_id}"
    best_id = best_node["node_id"]

    for _ in range(_PIN_ATTEMPTS):
        # The client uses decode_responses=True.
        pinned = cast(str | None, await redis.get(key))
        if pinned is None:
            # NX: when two agents race on a new study, the first writer wins.
            if await redis.set(key, best_id, nx=True, ex=STUDY_AFFINITY_TTL_S):
                log.info("study=%s pinned to %s", study_id, best_id)
                await _record_study_location(redis, study_id, best_node)
                return BestNodeResponse.model_validate({**best_node, "study_id": study_id})
            continue

        if pinned in healthy:
            await redis.expire(key, STUDY_AFFINITY_TTL_S)
            return BestNodeResponse.model_validate({**healthy[pinned], "study_id": study_id})

        # Pinned node is unhealthy, disallowed by policy, or its telemetry expired: move the
        # study, unless another agent already moved it since we read the pin.
        if await _replace_pin(redis, key, pinned, best_id):
            log.warning(
                "study=%s re-pinned from unavailable node %s to %s", study_id, pinned, best_id
            )
            await _record_study_location(redis, study_id, best_node)
            return BestNodeResponse.model_validate(
                {**best_node, "study_id": study_id, "rerouted": True}
            )

    raise HTTPException(status_code=503, detail="Study assignment is changing, retry later")


async def _replace_pin(redis: aioredis.Redis, key: str, old: str, new: str) -> bool:
    """Atomically set key to new only if it still holds old (compare-and-set)."""
    async with redis.pipeline(transaction=True) as pipe:
        try:
            await pipe.watch(key)
            if await pipe.get(key) != old:
                return False
            pipe.multi()  # type: ignore[no-untyped-call]
            pipe.set(key, new, ex=STUDY_AFFINITY_TTL_S)
            await pipe.execute()
        except WatchError:
            return False
    return True


@app.post("/transfers/start", status_code=204)
async def transfer_start(
    payload: TransferLeasePayload,
    api_key: str = Depends(validate_api_key),
) -> None:
    """Register an active in-flight transfer lease for live ingestion load tracking."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    now = time.time()
    expiry = now + INFLIGHT_TTL_S
    key = f"inflight:{payload.node_id}"
    member = f"{payload.agent_id}:{payload.study_id}"
    try:
        async with _redis.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zadd(key, {member: expiry})
            pipe.expire(key, INFLIGHT_TTL_S)
            await pipe.execute()
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc


@app.post("/transfers/finish", status_code=204)
async def transfer_finish(
    payload: TransferLeasePayload,
    api_key: str = Depends(validate_api_key),
) -> None:
    """Release an active in-flight transfer lease once a study upload finishes or aborts."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    key = f"inflight:{payload.node_id}"
    member = f"{payload.agent_id}:{payload.study_id}"
    try:
        await _redis.zrem(key, member)
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc


@app.post("/studies/{study_id}/commit")
async def commit_study(
    study_id: str,
    payload: StudyCommitPayload,
    api_key: str = Depends(validate_api_key),
) -> StudyLocationResponse:
    """Record confirmed study delivery and optional DICOM Storage Commitment in the index."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    node_cfg = NODES[payload.node_id]
    node_meta = {
        "node_id": payload.node_id,
        "ae_title": node_cfg["ae_title"],
        "base_url": node_cfg["base"],
        "jurisdiction": node_cfg.get("jurisdiction"),
    }
    try:
        entry = await _record_study_location(
            _redis,
            study_id,
            node_meta,
            transport=payload.transport,
            storage_committed=payload.storage_committed,
            instance_count=payload.instance_count,
        )
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    return StudyLocationResponse.model_validate(entry)


@app.get("/studies/{study_id}/location")
async def get_study_location(
    study_id: str,
    api_key: str = Depends(validate_api_key),
) -> StudyLocationResponse:
    """Return the regional node where study_id was routed and/or committed."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    try:
        raw = await _redis.get(f"study_loc:{study_id}")
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Study {study_id!r} not found in index")
    return StudyLocationResponse.model_validate(json.loads(raw))


@app.get("/studies")
async def list_studies(
    api_key: str = Depends(validate_api_key),
) -> list[StudyLocationResponse]:
    """Return all indexed study locations across regional nodes."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    try:
        keys = [k async for k in _redis.scan_iter(match="study_loc:*")]
        if not keys:
            return []
        raw_entries = await _redis.mget(*keys)
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    return [StudyLocationResponse.model_validate(json.loads(r)) for r in raw_entries if r]


@app.post("/heartbeat", status_code=204)
async def heartbeat(
    payload: HeartbeatPayload,
    api_key: str = Depends(validate_api_key),
) -> None:
    """Store RTT and throughput probes from an Edge Forwarder in Redis, shared by all replicas."""
    if _redis is None:
        raise HTTPException(status_code=503, detail="Redis client not initialized")
    try:
        await _redis.set(f"rtt:{payload.agent_id}", json.dumps(payload.rtt_dict), ex=RTT_TTL_S)
        if payload.dicom_rtt_dict is not None:
            await _redis.set(
                f"dicom_rtt:{payload.agent_id}",
                json.dumps(payload.dicom_rtt_dict),
                ex=RTT_TTL_S,
            )
        if payload.throughput_mbps_dict is not None:
            await _redis.set(
                f"throughput:{payload.agent_id}",
                json.dumps(payload.throughput_mbps_dict),
                ex=RTT_TTL_S,
            )
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc
    log.info(
        "rtt agent=%s mode=%s http=%s dicom=%s",
        payload.agent_id,
        payload.probe_mode or "http",
        payload.rtt_dict,
        payload.dicom_rtt_dict,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
