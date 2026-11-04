"""Unit tests for src/benchmark/failover_time.py"""

import ssl
import sys

import certifi
import httpx
import pytest
import respx
from httpx import Response

import src.benchmark.failover_time as bench

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _ca_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())


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


def test_missing_ca_bundle_stops_the_benchmark_before_it_touches_a_node(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(
        sys, "argv", ["failover_time", "--regions", "us-east1", "--modes", "process_stop"]
    )
    trials = []
    monkeypatch.setattr(bench, "run_single_trial", lambda *args: trials.append(args))

    with pytest.raises(RuntimeError, match="gen_certs"):
        bench.main()

    assert trials == []


def test_orchestrator_is_queried_with_a_context_that_verifies_against_the_ca_bundle(monkeypatch):
    seen = []
    real_client = httpx.Client
    monkeypatch.setattr(
        bench.httpx, "Client", lambda **kwargs: seen.append(kwargs) or real_client(**kwargs)
    )
    monkeypatch.setattr(bench, "run_single_trial", lambda *args: (1.0, 1.0))
    monkeypatch.setattr(
        sys,
        "argv",
        ["failover_time", "--regions", "us-east1", "--modes", "process_stop", "--trials", "1"],
    )

    bench.main()

    assert len(seen) == 1
    context = seen[0]["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED


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
