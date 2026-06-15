"""
WeightedScorer registers itself into scorer._REGISTRY on import.

Adding a new strategy follows the same pattern: subclass NodeScorer in a new
module, then append _REGISTRY["<name>"] = <Class> at the bottom.
"""

from typing import Any

from .scorer import _REGISTRY, NodeScorer


class WeightedScorer(NodeScorer):
    """Ranks nodes by a weighted sum of three inverse-cost signals:
    queue depth, disk space, and RTT.

    Default weights favour queue depth (0.5) and RTT (0.35) over disk (0.15), reflecting that
    a backlogged or slow node is a worse destination than a nearly-full one.
    """

    def __init__(self, w_queue: float = 0.5, w_disk: float = 0.15, w_rtt: float = 0.35) -> None:
        """
        Args:
            w_queue: Weight for queue-depth signal. Should dominate since a long queue
                     means the node is already overloaded.
            w_disk:  Weight for free-disk signal. Lower priority because most nodes
                     have plenty of headroom until they don't.
            w_rtt:   Weight for round-trip-time signal. High weight because latency
                     directly affects transfer speed.
        """
        self.w_queue = w_queue
        self.w_disk = w_disk
        self.w_rtt = w_rtt

    def score(self, node: dict[str, Any]) -> float:
        """Compute score = w_queue*(1/(q+1)) + w_disk*(free/total) + w_rtt*(1/(rtt+1))."""
        q_size = node.get("queue_size")
        q_score = 1.0 / (float(q_size if q_size is not None else 0) + 1)
        raw_free = node.get("disk_free_mb")
        disk_free = float(raw_free if raw_free is not None else 0.0)
        raw_total = node.get("disk_total_mb")
        disk_total = max(int(raw_total if raw_total is not None else 10_000), 1)
        disk_score = disk_free / disk_total
        raw_rtt = node.get("rtt_ms")
        rtt_ms = max(float(raw_rtt if raw_rtt is not None else 250.0), 1.0)
        rtt_score = 1.0 / (rtt_ms + 1)
        return self.w_queue * q_score + self.w_disk * disk_score + self.w_rtt * rtt_score


_REGISTRY["weighted"] = WeightedScorer
