"""
FastAPI app for the Diomede orchestrator.

Exposes a single endpoint that reads the latest node telemetry from Redis
(written by the telemetry daemon) and returns the best healthy destination.

When the caller passes a study_id, the first decision for that study is pinned in
Redis (study:{study_id}) so every later instance of the study goes to the same node.
The study is only moved if its pinned node becomes unhealthy or drops out of telemetry.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator
from redis.exceptions import RedisError

from src.utils.env import require_env
from src.utils.logging_config import get_logger

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

_rtt_cache: dict[str, dict[str, float]] = {}


def validate_api_key(api_key_str: str = Security(api_key_header)) -> str:
    if api_key_str != API_KEY:
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
    disk_free_mb: float | None = None
    disk_total_mb: int | None = None
    instance_count: int | None = None
    healthy: bool
    ts: str


class BestNodeResponse(NodeResponse):
    rtt_ms: float | None = None
    score: float | None = None
    study_id: str | None = None
    rerouted: bool = False


# {"agent_id": {"us-east1": 10000, "eu-west1": 10000, "af-south1": 10000, "asia-northeast1": 10}}
class HeartbeatPayload(BaseModel):
    agent_id: str
    rtt_dict: dict[str, float]

    @field_validator("rtt_dict")
    @classmethod
    def rtt_must_be_positive(cls, v: dict[str, float]) -> dict[str, float]:
        for node_id, rtt in v.items():
            if rtt <= 0:
                raise ValueError(f"rtt_ms for {node_id!r} must be positive, got {rtt}")
        return v

    @field_validator("rtt_dict")
    @classmethod
    def node_id_must_be_valid(cls, v: dict[str, float]) -> dict[str, float]:
        for node_id in v.keys():
            if node_id not in NODES.keys():
                raise ValueError(f"Invalid node id: {node_id!r}")
        return v


REDIS_URL = require_env("REDIS_URL")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    yield
    if _redis:
        await _redis.close()


app = FastAPI(title="Diomede Orchestrator", lifespan=lifespan)
_redis: aioredis.Redis[str] | None = None


async def _get_nodes() -> list[dict[str, Any]]:
    """Gets all available telemetry nodes from Redis"""
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
    return nodes


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
    api_key: str = Depends(validate_api_key),
) -> BestNodeResponse:
    """Return the highest-scoring healthy node in Redis.

    With study_id, return the node the study is pinned to while it stays healthy.
    """
    node_list = await _get_nodes()

    scorer = get_scorer()
    healthy = [n for n in node_list if n.get("healthy") is True]
    if not healthy:
        raise HTTPException(status_code=503, detail="No healthy nodes available")

    agent_rtt = _rtt_cache.get(agent_id)
    if agent_rtt is None:
        log.warning("No RTT data for agent %s; falling back to default scoring", agent_id)
        agent_rtt = {}

    for node in healthy:
        rtt = agent_rtt.get(node["node_id"])
        log.info(f"Node {node['node_id']} has RTT {rtt} ms for agent {agent_id}")
        if rtt is not None:
            node["rtt_ms"] = rtt
            log.info(f"Node rtt_ms: {node['node_id']} = {node['rtt_ms']}")
    best_node = max(healthy, key=scorer.score)
    if study_id is None:
        return BestNodeResponse.model_validate(best_node)

    try:
        return await _route_study(study_id, best_node, {n["node_id"]: n for n in healthy})
    except RedisError as exc:
        log.warning("Redis unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Telemetry store unavailable") from exc


async def _route_study(
    study_id: str, best_node: dict[str, Any], healthy: dict[str, dict[str, Any]]
) -> BestNodeResponse:
    """Return the node study_id is pinned to, pinning or re-pinning it to best_node if needed."""
    assert _redis is not None  # checked by _get_nodes()
    key = f"study:{study_id}"

    pinned = await _redis.get(key)
    if pinned is None:
        # NX: when two agents race on a new study, the first writer wins.
        if await _redis.set(key, best_node["node_id"], nx=True, ex=STUDY_AFFINITY_TTL_S):
            log.info("study=%s pinned to %s", study_id, best_node["node_id"])
            return BestNodeResponse.model_validate({**best_node, "study_id": study_id})
        pinned = await _redis.get(key)

    if pinned in healthy:
        await _redis.expire(key, STUDY_AFFINITY_TTL_S)
        return BestNodeResponse.model_validate({**healthy[pinned], "study_id": study_id})

    # Pinned node is unhealthy or its telemetry expired: move the rest of the study.
    await _redis.set(key, best_node["node_id"], ex=STUDY_AFFINITY_TTL_S)
    log.warning(
        "study=%s re-pinned from unavailable node %s to %s",
        study_id,
        pinned,
        best_node["node_id"],
    )
    return BestNodeResponse.model_validate({**best_node, "study_id": study_id, "rerouted": True})


@app.post("/heartbeat", status_code=204)
async def heartbeat(
    payload: HeartbeatPayload,
    api_key: str = Depends(validate_api_key),
) -> None:
    """RTT probe from the Forwarder Daemon and update the cache."""
    _rtt_cache[payload.agent_id] = payload.rtt_dict
    log.info(f"rtt cache {_rtt_cache}")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
