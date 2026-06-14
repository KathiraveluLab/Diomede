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
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.utils.logging_config import get_logger

from .daemon import NODES
from .scorer import get_scorer
from .weighted_scorer import WeightedScorer  # noqa: F401 to trigger self-registration

log = get_logger(__name__, "ORCHESTRATOR")


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
async def get_nodes() -> list[NodeResponse]:
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
async def get_best_node() -> NodeResponse:
    """Return the highest-scoring healthy node in Redis."""
    node_list = await _get_nodes()

    scorer = get_scorer()
    healthy = [n for n in node_list if n.get("healthy") is True]
    if not healthy:
        raise HTTPException(status_code=503, detail="No healthy nodes available")

    return NodeResponse.model_validate(max(healthy, key=scorer.score))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
