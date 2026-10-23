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


def _on(*placements: str | None) -> dict[str, str | None]:
    """Where each instance the edge accepts ends up: instance i is stored on the given region,
    or on none (None)."""
    return {f"id{i}": region for i, region in enumerate(placements)}


def _run(argv, holders, fake_docker=None, failing_regions=()):
    """Run the benchmark against mocked edge and cloud nodes.

    `holders` maps each instance ID the edge hands out (id0, id1, ...) to the cloud node that
    stores it. `failing_regions` answer every request with a 500.
    """
    fake_docker = fake_docker or FakeDocker()
    ids = (f"id{i}" for i in itertools.count())

    def instance_lookup(region):
        def reply(request):
            instance_id = request.url.path.rsplit("/", 1)[-1]
            return Response(200 if holders.get(instance_id) == region else 404)

        return reply

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
        router.get(f"{EDGE}/instances").mock(return_value=Response(200, json=[]))
        for region, url in sr.CLOUD_URLS.items():
            if region in failing_regions:
                router.route(url__startswith=url).mock(return_value=Response(500))
            else:
                router.get(f"{url}/statistics").mock(return_value=Response(200, json={}))
                router.get(url__regex=rf"^{url}/instances/.+").mock(
                    side_effect=instance_lookup(region)
                )
        code = sr.run_benchmark()
    return code, fake_docker


# --- delivery is confirmed instance by instance on the cloud nodes -----------------------------


def test_success_when_every_sent_instance_is_stored_on_a_cloud_node(capsys):
    code, _ = _run(["--burst-count", "4"], _on(REGIONS[0], REGIONS[0], REGIONS[1], REGIONS[1]))

    out = capsys.readouterr().out
    assert code == 0
    assert "Confirmed on Cloud:     4" in out
    assert "Missing from Cloud:     0" in out
    assert "Delivery Success Rate:  100.00%" in out
    assert f"{REGIONS[0]:<18}: 2 instances" in out
    assert f"{REGIONS[1]:<18}: 2 instances" in out


def test_instances_that_left_the_edge_but_never_reached_the_cloud_are_a_failure(capsys):
    code, _ = _run(["--burst-count", "4"], _on(None, None, None, None))

    out = capsys.readouterr().out
    assert code == 1
    assert "Cleared from Edge:      4" in out
    assert "Confirmed on Cloud:     0" in out
    assert "Missing from Cloud:     4" in out
    assert "Delivery Success Rate:  0.00%" in out


def test_partial_loss_is_reported_as_a_partial_rate(capsys):
    code, _ = _run(["--burst-count", "4"], _on(REGIONS[2], REGIONS[2], REGIONS[3], None))

    out = capsys.readouterr().out
    assert code == 1
    assert "Missing from Cloud:     1" in out
    assert "Delivery Success Rate:  75.00%" in out


def test_instances_left_by_earlier_runs_do_not_hide_a_loss(capsys):
    """The cloud nodes hold plenty of other instances, but none of the ones this run sent."""
    holders = {f"leftover-{i}": REGIONS[0] for i in range(10)}

    code, _ = _run(["--burst-count", "4"], holders)

    out = capsys.readouterr().out
    assert code == 1
    assert "Confirmed on Cloud:     0" in out


def test_an_instance_found_on_another_node_counts_even_if_a_node_is_down(capsys):
    code, _ = _run(
        ["--burst-count", "2"],
        _on(REGIONS[1], REGIONS[2]),
        failing_regions={REGIONS[0]},
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Confirmed on Cloud:     2" in out
    assert "could not be queried" not in out


def test_a_missing_instance_with_a_node_down_warns_that_it_may_be_there(capsys):
    code, _ = _run(
        ["--burst-count", "2"],
        _on(REGIONS[1], None),
        failing_regions={REGIONS[0]},
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "Missing from Cloud:     1" in out
    assert f"WARNING: {REGIONS[0]} could not be queried" in out


# --- the outage is only reported when it happened ----------------------------------------------


def test_outage_is_applied_and_the_node_restarted(capsys):
    code, docker = _run(["--burst-count", "4", "--simulate-outage"], _on(*[REGIONS[1]] * 4))

    assert code == 0
    assert docker.calls == [["docker", "stop", "orthanc-us"], ["docker", "start", "orthanc-us"]]
    assert "Mid-Transfer Outage:    True" in capsys.readouterr().out


@pytest.mark.parametrize(
    "fake_docker",
    [FakeDocker(returncode=1), FakeDocker(missing=True)],
    ids=["docker-stop-fails", "docker-missing"],
)
def test_failed_outage_is_not_reported_as_simulated(capsys, fake_docker):
    code, _ = _run(["--burst-count", "4", "--simulate-outage"], _on(*[REGIONS[1]] * 4), fake_docker)

    out = capsys.readouterr().out
    assert code == 2
    assert "Mid-Transfer Outage:    NOT APPLIED" in out
    assert "Mid-Transfer Outage:    True" not in out


def test_no_restart_is_attempted_when_the_node_was_never_stopped():
    _, docker = _run(
        ["--burst-count", "4", "--simulate-outage"],
        _on(*[REGIONS[1]] * 4),
        FakeDocker(returncode=1),
    )

    assert [call[1] for call in docker.calls] == ["stop"]


def test_the_stopped_node_is_back_before_the_instances_are_looked_up():
    order: list[str] = []
    recorder = FakeDocker()

    def docker_run(cmd, **kwargs):
        order.append(f"docker {cmd[1]}")
        return recorder(cmd, **kwargs)

    real_locate = sr.locate_on_cloud

    def locate(client, instance_ids):
        order.append("locate")
        return real_locate(client, instance_ids)

    with patch.object(sr, "locate_on_cloud", locate):
        _run(["--burst-count", "4", "--simulate-outage"], _on(*[REGIONS[1]] * 4), docker_run)

    assert order.index("docker start") < order.index("locate")


# --- helpers --------------------------------------------------------------------------------------


@respx.mock
def test_locate_on_cloud_credits_an_instance_once_and_reports_unreadable_nodes():
    first, second = REGIONS[0], REGIONS[1]
    respx.get(url__regex=rf"^{sr.CLOUD_URLS[first]}/instances/.+").mock(return_value=Response(200))
    respx.get(url__regex=rf"^{sr.CLOUD_URLS[second]}/instances/.+").mock(return_value=Response(200))
    for region in REGIONS[2:]:
        respx.get(url__regex=rf"^{sr.CLOUD_URLS[region]}/instances/.+").mock(
            return_value=Response(500)
        )

    with httpx.Client() as client:
        per_node, missing, unreadable = sr.locate_on_cloud(client, ["a", "b"])

    assert per_node[first] == 2  # found on the first node, so the others are not asked
    assert per_node[second] == 0
    assert missing == []
    assert unreadable == set()


def test_docker_helper_reports_failure_and_missing_binary():
    with patch.object(sr.subprocess, "run", FakeDocker(returncode=1)):
        assert sr.docker("stop", "x") is False
    with patch.object(sr.subprocess, "run", FakeDocker(missing=True)):
        assert sr.docker("stop", "x") is False
    with patch.object(sr.subprocess, "run", FakeDocker()):
        assert sr.docker("stop", "x") is True


@respx.mock
def test_wait_for_cloud_nodes_waits_for_a_node_that_is_still_restarting():
    slow = REGIONS[0]
    answers = iter([Response(500), Response(500), Response(200, json={})])
    for region, url in sr.CLOUD_URLS.items():
        if region == slow:
            respx.get(f"{url}/statistics").mock(side_effect=lambda r: next(answers))
        else:
            respx.get(f"{url}/statistics").mock(return_value=Response(200, json={}))

    with patch.object(sr.time, "sleep"), httpx.Client() as client:
        assert sr.wait_for_cloud_nodes(client) == []


@respx.mock
def test_wait_for_cloud_nodes_reports_a_node_that_never_answers():
    down = REGIONS[0]
    for region, url in sr.CLOUD_URLS.items():
        status = 500 if region == down else 200
        respx.get(f"{url}/statistics").mock(return_value=Response(status, json={}))

    with patch.object(sr.time, "sleep"), httpx.Client() as client:
        assert sr.wait_for_cloud_nodes(client, attempts=3) == [down]
