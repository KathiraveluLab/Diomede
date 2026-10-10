"""
Integration tests for orchestrator.

Requires the Docker Compose stack to be running:
    docker compose up -d
"""

import os
import ssl
import subprocess
import time

import httpx
import pytest
from dotenv import load_dotenv

from tests.integration.settings import CA_CERT, ORCH_URL

load_dotenv()

pytestmark = pytest.mark.integration

_SSL_CTX = ssl.create_default_context(cafile=CA_CERT)
_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")
_AUTH_HEADERS = {"X-API-Key": _API_KEY}

# node_id (Docker container name)
_NODE_CONTAINER = {
    "us-east1": "orthanc-us",
    "eu-west1": "orthanc-eu",
    "asia-northeast1": "orthanc-asia",
    "af-south1": "orthanc-af",
}

_POLL_INTERVAL_S = 10
_HTTP_TIMEOUT_S = 5
_FAILOVER_TIMEOUT_S = _POLL_INTERVAL_S + _HTTP_TIMEOUT_S + 5


def _get_nodes() -> list[dict]:
    resp = httpx.get(f"{ORCH_URL}/nodes", headers=_AUTH_HEADERS, verify=_SSL_CTX, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_best_node(agent_id: str = "agent-001") -> dict:
    resp = httpx.get(
        f"{ORCH_URL}/get-best-node",
        params={"agent_id": agent_id},
        headers=_AUTH_HEADERS,
        verify=_SSL_CTX,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _stop_container(name: str) -> None:
    subprocess.run(["docker", "stop", name], check=True, capture_output=True)


def _start_container(name: str) -> None:
    subprocess.run(["docker", "start", name], check=True, capture_output=True)


def _kill_daemon(container: str) -> None:
    script = (
        "import os, signal\n"
        "for p in filter(str.isdigit, os.listdir('/proc')):\n"
        "    cmd = open(f'/proc/{p}/cmdline', 'rb').read()\n"
        "    if int(p) != os.getpid() and b'src.orchestrator.daemon' in cmd:\n"
        "        os.kill(int(p), signal.SIGKILL)\n"
    )
    subprocess.run(["docker", "exec", container, "python3", "-c", script], capture_output=True)


def _restart_count(container: str) -> int:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.RestartCount}}", container],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout)


def test_nodes_returns_non_empty_list():
    nodes = _get_nodes()
    assert isinstance(nodes, list)
    assert len(nodes) > 0


def test_nodes_all_items_have_required_fields():
    required = {
        "node_id",
        "ae_title",
        "base_url",
        "queue_size",
        "disk_free_mb",
        "disk_total_mb",
        "instance_count",
        "healthy",
        "ts",
    }
    for node in _get_nodes():
        missing = required - node.keys()
        assert not missing, f"Node {node.get('node_id')} missing fields: {missing}"


def test_nodes_healthy_field_is_bool():
    for node in _get_nodes():
        assert isinstance(node["healthy"], bool), (
            f"Node {node.get('node_id')} has non-bool healthy: {node['healthy']!r}"
        )


def test_nodes_includes_all_registered_nodes():
    ids = {n["node_id"] for n in _get_nodes()}
    assert ids == set(_NODE_CONTAINER.keys())


def test_get_best_node_returns_healthy_node():
    node = _get_best_node()
    assert node["healthy"] is True
    assert node["node_id"] in _NODE_CONTAINER


def test_failover_when_best_node_goes_down():
    best = _get_best_node()
    best_id = best["node_id"]
    container = _NODE_CONTAINER[best_id]

    try:
        _stop_container(container)

        # Poll until the daemon reaches an unhealthy node and chooses a different node
        deadline = time.monotonic() + _FAILOVER_TIMEOUT_S
        new_node = None
        while time.monotonic() < deadline:
            try:
                candidate = _get_best_node()
                if candidate["node_id"] != best_id:
                    new_node = candidate
                    break
            except httpx.HTTPStatusError:
                pass  # 503 while daemon hasn't completed its next cycle yet
            time.sleep(2)

        assert new_node is not None, (
            f"Orchestrator still returned {best_id} after {_FAILOVER_TIMEOUT_S}s"
        )
    finally:
        _start_container(container)


def test_unknown_agent_id_uses_default_scoring():
    unknown_id = "agent-unknown"

    resp = httpx.get(
        f"{ORCH_URL}/get-best-node",
        params={"agent_id": unknown_id},
        headers=_AUTH_HEADERS,
        verify=_SSL_CTX,
        timeout=10,
    )
    assert resp.status_code == 200
    node = resp.json()
    assert node["healthy"] is True
    assert node["rtt_ms"] is None


def test_routing_survives_telemetry_daemon_crash():
    """A crashed telemetry daemon is restarted before node telemetry expires in Redis."""
    restarts = _restart_count("telemetry-daemon")
    _kill_daemon("telemetry-daemon")

    deadline = time.monotonic() + int(os.environ.get("REDIS_TTL_S", "30")) + 15
    while time.monotonic() < deadline:
        assert _get_best_node()["healthy"] is True
        time.sleep(5)
    assert _restart_count("telemetry-daemon") > restarts
