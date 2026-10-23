"""Unit tests for src/benchmark/routing_correctness.py"""

import pytest

import src.benchmark.routing_correctness as bench

pytestmark = pytest.mark.unit


def test_every_permutation_has_a_single_least_loaded_node():
    for perm in bench.generate_permutations():
        queues = sorted(perm.values())
        assert queues[0] < queues[1], perm
