"""Every long-running service in docker-compose.yml must be restarted by Docker after a crash."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _restart_policies() -> dict[str, str | None]:
    """Map each service to its restart policy, reading the file line by line."""
    policies: dict[str, str | None] = {}
    in_services = False
    current: str | None = None
    for line in COMPOSE.read_text().splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line) and not line.startswith("#"):
            break  # next top-level key
        service = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if in_services and service:
            current = service.group(1)
            policies[current] = None
        elif current:
            policy = re.match(r"^    restart:\s*(\S+)\s*$", line)
            if policy:
                policies[current] = policy.group(1)
    return policies


def test_the_compose_file_defines_services():
    # Guards the parser: a file it cannot read would make the policy test below pass vacuously.
    assert _restart_policies()


def test_every_service_is_restarted_unless_stopped_on_purpose():
    # "unless-stopped" restarts after a crash but respects `docker stop`, which the failover
    # tests and benchmarks use to take a node down. Checking every service the file defines
    # means a service added later (more Orthanc nodes, for example) is covered too.
    missing = {
        service: policy
        for service, policy in _restart_policies().items()
        if policy != "unless-stopped"
    }

    assert missing == {}
