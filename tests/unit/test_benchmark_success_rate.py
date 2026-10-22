"""Unit tests for src/benchmark/success_rate.py"""

import itertools
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

import src.benchmark.success_rate as sr

pytestmark = pytest.mark.unit

EDGE = sr.DEFAULT_EDGE_URL
REGIONS = list(sr.CLOUD_URLS)


class FakeDocker:
    """Stands in for subprocess.run; records calls and answers with a chosen exit code."""

    def __init__(self, returncode: int = 0, missing: bool = False) -> None:
        self.returncode = returncode
        self.missing = missing
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.missing:
            raise FileNotFoundError("docker")
        return SimpleNamespace(returncode=self.returncode, stderr="no such container")


def _run(argv, before, after, fake_docker=None, status=200):
    """Run the benchmark against mocked edge and cloud nodes.

    `before` and `after` are the CountInstances each cloud node reports before and after the
    transfers; None means the node does not answer.
    """
    fake_docker = fake_docker or FakeDocker()
    ids = (f"id{i}" for i in itertools.count())
    phase = {"now": before}

    def statistics(region):
        def reply(request):
            count = phase["now"][region]
            if count is None:
                return Response(status)
            return Response(200, json={"CountInstances": count})

        return reply

    def edge_listing(request):
        phase["now"] = after  # the transfers are over by the time the edge is polled
        return Response(200, json=[])

    with (
        respx.mock(assert_all_called=False) as router,
        patch.object(
            sys, "argv", ["success_rate", "--file-size-kb", "1", "--timeout-s", "2", *argv]
        ),
        patch.object(sr.time, "sleep"),
        patch.object(sr.subprocess, "run", fake_docker),
    ):
        router.post(f"{EDGE}/instances").mock(
            side_effect=lambda r: Response(200, json={"ID": next(ids)})
        )
        router.get(f"{EDGE}/instances").mock(side_effect=edge_listing)
        for region, url in sr.CLOUD_URLS.items():
            router.get(f"{url}/statistics").mock(side_effect=statistics(region))
        code = sr.run_benchmark()
    return code, fake_docker


def _counts(**per_region):
    return {region: per_region.get(region, 0) for region in REGIONS}


# --- delivery is confirmed on the cloud nodes ---------------------------------------------------


def test_success_when_the_cloud_nodes_gained_every_instance(capsys):
    code, _ = _run(["--burst-count", "4"], _counts(), {**_counts(), REGIONS[0]: 3, REGIONS[1]: 1})

    out = capsys.readouterr().out
    assert code == 0
    assert "Confirmed on Cloud:     4" in out
    assert "Delivery Success Rate:  100.00%" in out


def test_instances_that_left_the_edge_but_never_reached_the_cloud_are_a_failure(capsys):
    code, _ = _run(["--burst-count", "4"], _counts(), _counts())

    out = capsys.readouterr().out
    assert code == 1
    assert "Cleared from Edge:      4" in out
    assert "Confirmed on Cloud:     0" in out
    assert "Delivery Success Rate:  0.00%" in out


def test_partial_loss_is_reported_as_a_partial_rate(capsys):
    code, _ = _run(["--burst-count", "4"], _counts(), {**_counts(), REGIONS[2]: 3})

    assert code == 1
    assert "Delivery Success Rate:  75.00%" in capsys.readouterr().out


def test_a_node_that_cannot_be_read_is_reported_not_dropped(capsys):
    after = {**_counts(), REGIONS[0]: 4, REGIONS[3]: None}

    code, _ = _run(["--burst-count", "4"], _counts(), after, status=500)

    out = capsys.readouterr().out
    assert code == 1
    assert f"{REGIONS[3]:<18}: unreadable" in out
    assert "could not compare" in out


# --- the outage is only reported when it happened ----------------------------------------------


def test_outage_is_applied_and_the_node_restarted(capsys):
    code, docker = _run(
        ["--burst-count", "4", "--simulate-outage"],
        _counts(),
        {**_counts(), REGIONS[1]: 4},
    )

    assert code == 0
    assert docker.calls == [["docker", "stop", "orthanc-us"], ["docker", "start", "orthanc-us"]]
    assert "Mid-Transfer Outage:    True" in capsys.readouterr().out


@pytest.mark.parametrize(
    "fake_docker",
    [FakeDocker(returncode=1), FakeDocker(missing=True)],
    ids=["docker-stop-fails", "docker-missing"],
)
def test_failed_outage_is_not_reported_as_simulated(capsys, fake_docker):
    code, _ = _run(
        ["--burst-count", "4", "--simulate-outage"],
        _counts(),
        {**_counts(), REGIONS[1]: 4},
        fake_docker,
    )

    out = capsys.readouterr().out
    assert code == 2
    assert "Mid-Transfer Outage:    NOT APPLIED" in out
    assert "Mid-Transfer Outage:    True" not in out


def test_no_restart_is_attempted_when_the_node_was_never_stopped():
    _, docker = _run(
        ["--burst-count", "4", "--simulate-outage"],
        _counts(),
        {**_counts(), REGIONS[1]: 4},
        FakeDocker(returncode=1),
    )

    assert [call[1] for call in docker.calls] == ["stop"]


def test_the_stopped_node_is_back_before_the_final_counts_are_read():
    order: list[str] = []
    recorder = FakeDocker()

    def docker_run(cmd, **kwargs):
        order.append(f"docker {cmd[1]}")
        return recorder(cmd, **kwargs)

    real_final = sr.get_final_instance_counts

    def final_counts(client, attempts=10):
        order.append("final counts")
        return real_final(client, attempts)

    with patch.object(sr, "get_final_instance_counts", final_counts):
        _run(
            ["--burst-count", "4", "--simulate-outage"],
            _counts(),
            {**_counts(), REGIONS[1]: 4},
            docker_run,
        )

    assert order.index("docker start") < order.index("final counts")


# --- helpers --------------------------------------------------------------------------------------


def test_confirmed_on_cloud_ignores_nodes_that_lost_count_and_flags_unreadable_ones():
    initial = {"a": 5, "b": 5, "c": -1}
    final = {"a": 9, "b": 4, "c": 7}

    assert sr.confirmed_on_cloud(initial, final) == (4, ["c"])


def test_docker_helper_reports_failure_and_missing_binary():
    with patch.object(sr.subprocess, "run", FakeDocker(returncode=1)):
        assert sr.docker("stop", "x") is False
    with patch.object(sr.subprocess, "run", FakeDocker(missing=True)):
        assert sr.docker("stop", "x") is False
    with patch.object(sr.subprocess, "run", FakeDocker()):
        assert sr.docker("stop", "x") is True


def test_final_counts_wait_for_a_node_that_is_still_restarting():
    answers = iter([-1, -1, 7])

    with (
        patch.object(sr, "get_instance_counts", lambda client: {"us": next(answers)}),
        patch.object(sr.time, "sleep"),
    ):
        assert sr.get_final_instance_counts(httpx.Client()) == {"us": 7}
