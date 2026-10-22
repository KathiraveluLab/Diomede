"""Every long-running service in docker-compose.yml must be restarted by Docker after a crash."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

EXPECTED_SERVICES = {
    "orthanc-us",
    "orthanc-eu",
    "orthanc-asia",
    "orthanc-af",
    "redis",
    "telemetry-daemon",
    "orchestrator",
    "agent-001",
}


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


def test_the_compose_file_defines_the_expected_services():
    assert set(_restart_policies()) == EXPECTED_SERVICES


@pytest.mark.parametrize("service", sorted(EXPECTED_SERVICES))
def test_service_is_restarted_unless_stopped_on_purpose(service):
    # "unless-stopped" restarts after a crash but respects `docker stop`, which the failover
    # tests and benchmarks use to take a node down.
    assert _restart_policies()[service] == "unless-stopped"
