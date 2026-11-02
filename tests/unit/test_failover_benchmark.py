"""Unit tests for src/benchmark/failover_time.py"""

import sys

import httpx
import pytest
import respx
from httpx import Response

import src.benchmark.failover_time as bench

pytestmark = pytest.mark.unit


def test_summary_reports_measured_recovery_times(monkeypatch, capsys):
    times = iter([(10.0, 6.0), (12.0, 8.0)])
    monkeypatch.setattr(bench, "run_single_trial", lambda *args: next(times))
    monkeypatch.setattr(
        sys,
        "argv",
        ["failover_time", "--regions", "us-east1", "--modes", "process_stop", "--trials", "2"],
    )

    bench.main()

    summary = capsys.readouterr().out.splitlines()[-3]
    assert "11.00 ± 1.41 s" in summary
    assert "7.00 ± 1.41 s" in summary


@respx.mock
def test_unreadable_node_list_is_not_reported_as_unhealthy():
    respx.get(f"{bench.ORCH_URL}/nodes").mock(return_value=Response(503))

    with httpx.Client() as client:
        assert bench.is_node_healthy(client, "us-east1") is None


def test_orchestrator_errors_are_not_timed_as_a_failover(monkeypatch):
    monkeypatch.setattr(bench, "is_node_healthy", lambda *args: None)
    monkeypatch.setattr(bench, "inject_failure", lambda *args: None)
    monkeypatch.setattr(bench, "restore_node", lambda *args: None)
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)

    failover, recovery = bench.run_single_trial(
        None, "us-east1", "orthanc-us", "process_stop", 0, 0.01
    )

    assert (failover, recovery) == (0.01, 0.01)
