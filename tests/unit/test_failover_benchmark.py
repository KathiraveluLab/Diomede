"""Unit tests for src/benchmark/failover_time.py"""

import itertools
import ssl
import subprocess
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

    assert (failover, recovery) == (None, None)


def _trial(monkeypatch, health):
    """Run one trial against a stand-in for the orchestrator's answers.

    Returns what it measured and the order of the docker actions it took.
    """
    actions = []
    monkeypatch.setattr(bench, "is_node_healthy", health)
    monkeypatch.setattr(bench, "inject_failure", lambda *args: actions.append("inject"))
    monkeypatch.setattr(bench, "restore_node", lambda *args: actions.append("restore"))
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    result = bench.run_single_trial(None, "us-east1", "orthanc-us", "process_stop", 0, 0.01)
    return result, actions


def test_failover_that_is_never_seen_is_not_a_measurement(monkeypatch):
    (failover, recovery), actions = _trial(monkeypatch, lambda *args: True)

    assert failover is None
    # Nothing went down as far as the orchestrator knows, so there is nothing to time the
    # recovery of either; the node is restored all the same.
    assert recovery is None
    assert actions == ["inject", "restore"]


def test_recovery_that_is_never_seen_is_not_a_measurement(monkeypatch):
    answers = itertools.chain([True, False], itertools.repeat(False))

    (failover, recovery), _ = _trial(monkeypatch, lambda *args: next(answers))

    assert failover is not None
    assert recovery is None


def test_failover_and_recovery_that_are_seen_are_measured(monkeypatch):
    answers = itertools.chain([True, False], itertools.repeat(True))

    (failover, recovery), actions = _trial(monkeypatch, lambda *args: next(answers))

    assert failover is not None
    assert recovery is not None
    assert actions == ["inject", "restore"]


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_node_is_restored_when_the_trial_is_cut_short(monkeypatch, error):
    answers = iter([True])

    def health(*args):
        try:
            return next(answers)
        except StopIteration:
            raise error from None

    actions = []
    monkeypatch.setattr(bench, "is_node_healthy", health)
    monkeypatch.setattr(bench, "inject_failure", lambda *args: actions.append("inject"))
    monkeypatch.setattr(bench, "restore_node", lambda *args: actions.append("restore"))
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)

    with pytest.raises(error):
        bench.run_single_trial(None, "us-east1", "orthanc-us", "process_stop", 0, 30.0)

    assert actions == ["inject", "restore"]


def test_node_is_restored_when_injecting_the_failure_fails_partway(monkeypatch):
    actions = []

    def inject(*args):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(bench, "is_node_healthy", lambda *args: True)
    monkeypatch.setattr(bench, "inject_failure", inject)
    monkeypatch.setattr(bench, "restore_node", lambda *args: actions.append("restore"))
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)

    with pytest.raises(subprocess.CalledProcessError):
        bench.run_single_trial(None, "us-east1", "orthanc-us", "process_stop", 0, 30.0)

    assert actions == ["restore"]


def _main(monkeypatch, capsys, trials):
    results = iter(trials)
    monkeypatch.setattr(bench, "run_single_trial", lambda *args: next(results))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "failover_time",
            "--regions",
            "us-east1",
            "--modes",
            "process_stop",
            "--trials",
            str(len(trials)),
        ],
    )
    code = bench.main()
    return code, capsys.readouterr().out


def test_timed_out_trials_are_left_out_of_the_statistics(monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, [(10.0, 12.0), (None, None), (None, None)])

    summary = out.splitlines()[-3]
    assert "10.00 ± 0.00 s" in summary
    assert "12.00 ± 0.00 s" in summary
    # Recovery was only attempted for the one trial whose failover was seen, and it succeeded.
    assert "2/3, 0/1" in summary
    assert code != 0


def test_summary_is_n_a_when_every_trial_timed_out(monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, [(None, None), (None, None)])

    summary = out.splitlines()[-3]
    assert summary.count("n/a") == 2
    assert "2/2, 0/0" in summary
    assert code != 0


def test_recovery_timeouts_are_counted_and_left_out_of_the_statistics(monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, [(10.0, None), (12.0, 8.0)])

    summary = out.splitlines()[-3]
    assert "11.00 ± 1.41 s" in summary
    assert "8.00 ± 0.00 s" in summary
    assert "0/2, 1/2" in summary
    assert code != 0


def test_a_trial_that_timed_out_is_reported_as_such(monkeypatch, capsys):
    _, out = _main(monkeypatch, capsys, [(10.0, None)])

    assert "Trial 1/1: Failover = 10.00 s | Recovery = timed out" in out


def test_recovery_that_was_never_attempted_is_reported_as_not_measured(monkeypatch, capsys):
    _, out = _main(monkeypatch, capsys, [(None, None)])

    assert "Trial 1/1: Failover = timed out | Recovery = not measured" in out


def test_table_rules_are_as_wide_as_the_header(monkeypatch, capsys):
    _, out = _main(monkeypatch, capsys, [(10.0, 6.0)])

    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("Region "))
    assert lines[lines.index(header) - 1] == "=" * len(header)
    assert lines[lines.index(header) + 1] == "-" * len(header)
    assert lines[-2] == "=" * len(header)


def test_exit_code_is_zero_when_no_trial_timed_out(monkeypatch, capsys):
    code, out = _main(monkeypatch, capsys, [(10.0, 6.0), (12.0, 8.0)])

    assert code == 0
    assert "0/2, 0/2" in out.splitlines()[-3]
