"""Unit tests for src/benchmark/failover_time.py"""

import sys

import pytest

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
