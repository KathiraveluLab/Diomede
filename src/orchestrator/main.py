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

from .daemon import NODES
from .scorer import get_scorer
from .weighted_scorer import WeightedScorer  # noqa: F401 to trigger self-registration


class NodeResponse(BaseModel):
    node_id: str
    ae_title: str
    base_url: str
    queue_size: int
    disk_free_mb: float
    disk_total_mb: int
    instance_count: int
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


@app.get("/get-best-node")
async def get_best_node() -> NodeResponse:
    """Return the highest-scoring healthy node in Redis."""
    assert _redis is not None
    keys: list[str] = [f"node:{k}" for k in NODES.keys()]
    if not keys:
        raise HTTPException(status_code=503, detail="No node telemetry available")

    raw = await _redis.mget(*keys)
    nodes: list[dict[str, Any]] = [json.loads(v) for v in raw if v is not None]

    scorer = get_scorer()
    healthy = [n for n in nodes if n.get("healthy") is True]
    if not healthy:
        raise HTTPException(status_code=503, detail="No healthy nodes available")

    return NodeResponse.model_validate(max(healthy, key=scorer.score))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
