"""Unit tests for how scripts/gen_certs.sh reads the cloud node list."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "gen_certs.sh"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX bash"),
]

_US = {"name": "us", "base": "https://orthanc-us:8042", "ae_title": "US", "auth_env": "NODE_US"}
_EU = {"name": "eu", "base": "https://orthanc-eu:8042", "ae_title": "EU", "auth_env": "NODE_EU"}


def _run(cwd, command, cloud_nodes=None):
    env = {k: v for k, v in os.environ.items() if k != "CLOUD_NODES"}
    if cloud_nodes is not None:
        env["CLOUD_NODES"] = cloud_nodes
    return subprocess.run(["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True)


def _hosts(cwd, cloud_nodes=None):
    return _run(cwd, f'source "{SCRIPT}" && node_hosts', cloud_nodes).stdout.split()


def _write_file(cwd, nodes):
    (cwd / "config").mkdir()
    (cwd / "config" / "nodes.json").write_text(json.dumps(nodes, indent=2))


def _old_certs(cwd):
    (cwd / "certs" / "orthanc-us").mkdir(parents=True)
    (cwd / "certs" / "ca.key").write_text("keep me")
    return cwd / "certs" / "ca.key"


def test_hosts_come_from_the_file(tmp_path):
    _write_file(tmp_path, [_US, _EU])

    assert _hosts(tmp_path) == ["orthanc-eu", "orthanc-us"]


@pytest.mark.parametrize(
    "line",
    [
        "CLOUD_NODES={}\n",
        "CLOUD_NODES='{}'\n",
        "export CLOUD_NODES={}\n",
        "  CLOUD_NODES = {}\n",
        "CLOUD_NODES={}\r\n",
    ],
)
def test_dotenv_wins_over_the_file(tmp_path, line):
    _write_file(tmp_path, [_US])
    (tmp_path / ".env").write_text("# CLOUD_NODES=ignored\n" + line.format(json.dumps([_EU])))

    assert _hosts(tmp_path) == ["orthanc-eu"]


def test_empty_value_in_dotenv_means_the_file(tmp_path):
    _write_file(tmp_path, [_US])
    (tmp_path / ".env").write_bytes(b"CLOUD_NODES=\r\n")

    assert _hosts(tmp_path) == ["orthanc-us"]


def test_shell_variable_wins_over_dotenv(tmp_path):
    (tmp_path / ".env").write_text(f"CLOUD_NODES={json.dumps([_EU])}\n")

    assert _hosts(tmp_path, json.dumps([_US])) == ["orthanc-us"]


def test_empty_shell_variable_means_the_file_like_the_services(tmp_path):
    _write_file(tmp_path, [_US])
    (tmp_path / ".env").write_text(f"CLOUD_NODES={json.dumps([_EU])}\n")

    assert _hosts(tmp_path, "") == ["orthanc-us"]


@pytest.mark.parametrize(
    ("base", "host"),
    [
        ("https://pacs.example.org", "pacs.example.org"),
        ("https://pacs.example.org:8042/orthanc", "pacs.example.org"),
        ("http://10.0.0.5:8042", "10.0.0.5"),
        ("https://user:secret@pacs.example.org:8042", "pacs.example.org"),
    ],
)
def test_host_is_taken_from_the_address(tmp_path, base, host):
    assert _hosts(tmp_path, json.dumps([{**_US, "base": base}])) == [host]


@pytest.mark.parametrize("spacing", ['"base":"{}"', '"base" : "{}"', '"base":\t"{}"'])
def test_any_json_spacing_is_read(tmp_path, spacing):
    node = "[{" + spacing.format("https://orthanc-us:8042") + "}]"

    assert _hosts(tmp_path, node) == ["orthanc-us"]


def test_nodes_sharing_a_host_get_one_certificate(tmp_path):
    assert _hosts(tmp_path, json.dumps([_US, {**_US, "name": "us2"}])) == ["orthanc-us"]


@pytest.mark.parametrize(
    "nodes",
    [
        "[]",
        "not json",
        json.dumps([{**_US, "base": "orthanc-us:8042"}]),
        json.dumps([{**_US, "base": "https://[2001:db8::1]:8042"}]),
        json.dumps([{**_US, "base": "https://${REGION1_HOST}:8042"}]),
        json.dumps([{**_US, "base": "https://..:8042"}]),
    ],
)
def test_bad_list_stops_before_anything_is_signed(tmp_path, nodes):
    old = _old_certs(tmp_path)

    result = _run(tmp_path, f'bash "{SCRIPT}"', nodes)

    assert result.returncode != 0
    assert "CLOUD_NODES" in result.stderr
    assert old.read_text() == "keep me"
    assert not (tmp_path / "certs" / ".new").exists()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_failed_signing_keeps_the_old_certificates(tmp_path):
    old = _old_certs(tmp_path)
    nodes = json.dumps([{**_US, "base": "https://1.2.3:8042"}])

    result = _run(tmp_path, f'bash "{SCRIPT}"', nodes)

    assert result.returncode != 0
    assert "kept" in result.stderr
    assert old.read_text() == "keep me"
    assert (tmp_path / "certs" / "orthanc-us").is_dir()
    assert not (tmp_path / "certs" / ".new").exists()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_full_run_replaces_the_old_certificates(tmp_path):
    old = _old_certs(tmp_path)
    nodes = json.dumps([{**_US, "base": "https://10.0.0.5:8042"}])

    result = _run(tmp_path, f'bash "{SCRIPT}"', nodes)

    assert result.returncode == 0, result.stderr
    assert "keep me" not in old.read_text()
    assert not (tmp_path / "certs" / "orthanc-us").exists()
    assert not (tmp_path / "certs" / ".new").exists()
    for name in ("orchestrator", "agent-001", "10.0.0.5"):
        assert (tmp_path / "certs" / name / "combined.pem").is_file()
    san = subprocess.run(
        ["openssl", "x509", "-in", "server.crt", "-noout", "-ext", "subjectAltName"],
        cwd=tmp_path / "certs" / "10.0.0.5",
        capture_output=True,
        text=True,
    ).stdout
    assert "IP Address:10.0.0.5" in san
