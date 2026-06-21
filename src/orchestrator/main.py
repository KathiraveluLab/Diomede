"""
FastAPI app for the Diomede orchestrator.

Exposes a single endpoint that reads the latest node telemetry from Redis
(written by the telemetry daemon) and returns the best healthy destination.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, field_validator

from src.utils.logging_config import get_logger

from .daemon import NODES
from .scorer import get_scorer
from .weighted_scorer import WeightedScorer  # noqa: F401 to trigger self-registration

log = get_logger(__name__, "ORCHESTRATOR")

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=True)
API_KEY = os.getenv("ORCHESTRATOR_API_KEY")
if not API_KEY:
    raise RuntimeError("ORCHESTRATOR_API_KEY environment variable must be set")

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


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


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

    raw = await _redis.mget(*keys)
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
async def get_best_node(agent_id: str, api_key: str = Depends(validate_api_key)) -> NodeResponse:
    """Return the highest-scoring healthy node in Redis."""
    node_list = await _get_nodes()

    scorer = get_scorer()
    healthy = [n for n in node_list if n.get("healthy") is True]
    if not healthy:
        raise HTTPException(status_code=503, detail="No healthy nodes available")

    agent_rtt = _rtt_cache.get(agent_id)
    log.info(f"THIS IS agent_rtt: {agent_rtt}")
    if agent_rtt is not None:
        for node in healthy:
            rtt = agent_rtt.get(node["node_id"])
            log.info(f"Node {node['node_id']} has RTT {rtt} ms for agent {agent_id}")
            if rtt is not None:
                node["rtt_ms"] = rtt
                log.info(f"Node rtt_ms: {node['node_id']} = {node['rtt_ms']}")
    return NodeResponse.model_validate(max(healthy, key=scorer.score))


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
