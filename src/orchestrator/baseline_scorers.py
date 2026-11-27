"""Baseline node scoring strategies for comparative evaluation.

Registers ``round_robin`` and ``least_connections`` into ``scorer._REGISTRY`` so
the orchestrator can switch between telemetry-driven weighted scoring and standard
baseline policies via the ``SCORER`` environment variable.
"""

from __future__ import annotations

from typing import Any

from .scorer import _REGISTRY, NodeScorer


class RoundRobinScorer(NodeScorer):
    """Cycles deterministically across healthy nodes regardless of load or latency.

    Tracks how many times each node has been scored/selected and prefers the
    least-recently-chosen node, producing even round-robin distribution across
    calls without inspecting queue depth, disk headroom, or RTT.
    """

    def __init__(self) -> None:
        self._last_selected: dict[str, int] = {}
        self._tick: int = 0

    def score(self, node: dict[str, Any]) -> float:
        node_id = str(node.get("node_id", ""))
        last = self._last_selected.get(node_id, -1)
        # Higher score for the node with the smallest (oldest) last-selected tick.
        val = float(-last)
        node["score"] = val
        return val

    def record_selection(self, node_id: str) -> None:
        """Advance the round-robin cursor after a node is selected."""
        self._tick += 1
        self._last_selected[node_id] = self._tick


class LeastConnectionsScorer(NodeScorer):
    """Selects the healthy node with the fewest active transfers and queued jobs.

    Ignores network latency and storage headroom, modeling a standard
    least-connections load balancer.
    """

    def score(self, node: dict[str, Any]) -> float:
        queue_jobs = int(node.get("queue_size") or 0)
        inflight = int(node.get("inflight_uploads") or 0)
        active = queue_jobs + inflight
        val = 1.0 / (float(active) + 1.0)
        node["score"] = val
        return val


_REGISTRY["round_robin"] = RoundRobinScorer
_REGISTRY["least_connections"] = LeastConnectionsScorer
