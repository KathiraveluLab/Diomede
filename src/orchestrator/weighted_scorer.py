"""
WeightedScorer registers itself into scorer._REGISTRY on import.

Adding a new strategy follows the same pattern: subclass NodeScorer in a new
module, then append _REGISTRY["<name>"] = <Class> at the bottom.
"""

import math
import os
from typing import Any

from src.utils.logging_config import get_logger

from .scorer import _REGISTRY, NodeScorer

log = get_logger(__name__, "ORCHESTRATOR")


class WeightedScorer(NodeScorer):
    """Ranks nodes by a weighted sum of three inverse-cost signals:
    queue depth, disk space, and RTT.

    Default weights favour queue depth (0.5) and RTT (0.35) over disk (0.15), reflecting that
    a backlogged or slow node is a worse destination than a nearly-full one.
    """

    def __init__(
        self,
        w_queue: float | None = None,
        w_disk: float | None = None,
        w_rtt: float | None = None,
        rtt_ref_ms: float = 100.0,
    ) -> None:
        """
        Weights not passed in are read from W_QUEUE, W_DISK and W_RTT, falling back to
        0.5, 0.15 and 0.35.

        Args:
            w_queue: Weight for queue-depth signal. Should dominate since a long queue
                     means the node is already overloaded.
            w_disk:  Weight for free-disk signal. Lower priority because most nodes
                     have plenty of headroom until they don't.
            w_rtt:   Weight for round-trip-time signal. High weight because latency
                     directly affects transfer speed.
            rtt_ref_ms: Reference RTT value for normalization.
        """
        self.w_queue = w_queue if w_queue is not None else _env_weight("W_QUEUE", 0.5)
        self.w_disk = w_disk if w_disk is not None else _env_weight("W_DISK", 0.15)
        self.w_rtt = w_rtt if w_rtt is not None else _env_weight("W_RTT", 0.35)
        self.rtt_ref_ms = rtt_ref_ms
        log.info(
            "WeightedScorer initialized with weights:\n"
            "w_queue=%f, w_disk=%f, w_rtt=%f, rtt_ref_ms=%f",
            self.w_queue,
            self.w_disk,
            self.w_rtt,
            self.rtt_ref_ms,
        )

    def score(self, node: dict[str, Any]) -> float:
        """Compute score = w_queue*(1/(q+1)) + w_disk*(free/total) + w_rtt*(1/(rtt+1))."""
        q_size = node.get("queue_size")
        q_score = 1.0 / (float(q_size if q_size is not None else 0) + 1)
        raw_free = node.get("disk_free_mb")
        raw_total = node.get("disk_total_mb")
        if raw_free is None or raw_total is None:
            # No storage quota (Orthanc MaximumStorageSize = 0): nothing limits the node.
            disk_free, disk_total, disk_score = None, None, 1.0
        else:
            disk_free = float(raw_free)
            disk_total = max(int(raw_total), 1)
            disk_score = disk_free / disk_total
        raw_rtt = node.get("rtt_ms")
        rtt_ms = max(float(raw_rtt if raw_rtt is not None else 250.0), 1.0)
        rtt_score = 1.0 / (rtt_ms / self.rtt_ref_ms + 1)

        total_score = self.w_queue * q_score + self.w_disk * disk_score + self.w_rtt * rtt_score
        node["score"] = total_score

        log.info(
            f"Scoring node {node.get('node_id', 'unknown')}:\n"
            f"  q_size={q_size}, q_score={q_score:.4f}, disk_free={disk_free}, \n"
            f"  disk_total={disk_total}, disk_score={disk_score:.4f},\n"
            f"  rtt_ms={rtt_ms}, rtt_score={rtt_score:.4f}"
        )
        log.info(f"TOTAL SCORE: {node.get('node_id', 'unknown')} = {total_score:.4f}")

        return total_score


def _env_weight(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        weight = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(weight) or weight < 0:
        raise ValueError(f"{name} must be a finite, non-negative number, got {raw!r}")
    return weight


_REGISTRY["weighted"] = WeightedScorer
