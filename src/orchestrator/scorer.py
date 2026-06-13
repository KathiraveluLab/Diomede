"""
ABC and registry for node scoring.
"""

import os
from abc import ABC, abstractmethod
from typing import Any


class NodeScorer(ABC):
    """Base class for node scoring used by the orchestrator to rank destinations."""

    @abstractmethod
    def score(self, node: dict[str, Any]) -> float:
        """Return a scalar score for the given node — higher means more preferred."""
        ...


# Self-registration pattern: each scorer appends itself on import, so adding a new strategy
#  requires no changes here (open/closed principle).
# Singleton pattern: module-level dict shared across all imports and subclasses (one registry
#  per process) in python
_REGISTRY: dict[str, type[NodeScorer]] = {}


_SCORER_INSTANCE: NodeScorer | None = None


def get_scorer() -> NodeScorer:
    """Instantiate the scorer named by the SCORER env var (default: 'weighted').

    Raises ValueError if the name isn't in the registry. Set global variable only once to
    be optimized.
    """

    global _SCORER_INSTANCE

    if _SCORER_INSTANCE is None:
        name = os.getenv("SCORER", "weighted")
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown scorer '{name}'. Registered: {list(_REGISTRY)}")

        _SCORER_INSTANCE = cls()
    return _SCORER_INSTANCE
