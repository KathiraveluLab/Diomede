"""
WeightedScorer registers itself into scorer._REGISTRY on import.

Adding a new strategy follows the same pattern: subclass NodeScorer in a new
module, then append _REGISTRY["<name>"] = <Class> at the bottom.
"""

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
        w_queue: float = 0.5,
        w_disk: float = 0.15,
        w_rtt: float = 0.35,
        rtt_ref_ms: float = 100.0,
    ) -> None:
        """
        Args:
            w_queue: Weight for queue-depth signal. Should dominate since a long queue
                     means the node is already overloaded.
            w_disk:  Weight for free-disk signal. Lower priority because most nodes
                     have plenty of headroom until they don't.
            w_rtt:   Weight for round-trip-time signal. High weight because latency
                     directly affects transfer speed.
            rtt_ref_ms: Reference RTT value for normalization.
        """
        self.w_queue = w_queue
        self.w_disk = w_disk
        self.w_rtt = w_rtt
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
        disk_free = float(raw_free if raw_free is not None else 0.0)
        raw_total = node.get("disk_total_mb")
        disk_total = max(int(raw_total if raw_total is not None else 10_000), 1)
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


_REGISTRY["weighted"] = WeightedScorer
