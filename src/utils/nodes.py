"""The cloud node list shared by the telemetry daemon, the orchestrator and the forwarder."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import urlsplit

from src.utils.env import require_env
from src.utils.logging_config import get_logger

log = get_logger(__name__, "NODES")

NODES_FILE = Path(__file__).parents[2] / "config" / "nodes.json"
_REQUIRED_FIELDS = ("name", "base", "ae_title", "auth_env")
_REMOVED_VARS = re.compile(r"NODE_(?!INT_)\w+_BASE|REGION\d+_(NAME|AET)")


class NodeConfig(TypedDict):
    base: str
    ae_title: str
    auth: tuple[str, str]
    jurisdiction: NotRequired[str]
    dicom_host: NotRequired[str]
    dicom_port: NotRequired[int]


def load_nodes() -> dict[str, NodeConfig]:
    """Return the nodes in the CLOUD_NODES env variable, or in config/nodes.json if it is unset.

    A node's auth_env names the variables that hold its login (NODE_US means NODE_US_USER and
    NODE_US_PASS), which keeps passwords out of the list. Optional fields (jurisdiction,
    dicom_host, dicom_port) are preserved when specified in the list entry.
    """
    removed = sorted(name for name in os.environ if _REMOVED_VARS.fullmatch(name))
    if removed:
        log.warning("%s no longer set the cloud nodes; use the node list", ", ".join(removed))

    raw = os.getenv("CLOUD_NODES")
    source = "CLOUD_NODES"
    if not raw:
        source = str(NODES_FILE)
        try:
            raw = NODES_FILE.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Set CLOUD_NODES or create {NODES_FILE}: {exc}") from exc

    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"{source} must be a non-empty JSON list of nodes")

    nodes: dict[str, NodeConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(f"Each node in {source} must be an object")
        for field in _REQUIRED_FIELDS:
            if not entry.get(field) or not isinstance(entry[field], str):
                raise RuntimeError(f"Node {entry.get('name')!r} in {source} needs a {field}")
        if entry["name"] in nodes:
            raise RuntimeError(f"Node {entry['name']!r} appears more than once in {source}")
        url = urlsplit(entry["base"])
        if url.scheme not in ("http", "https") or not url.netloc:
            raise RuntimeError(f"Node {entry['name']!r} in {source} needs an http(s) base URL")
        node_cfg: NodeConfig = {
            "base": entry["base"].rstrip("/"),
            "ae_title": entry["ae_title"],
            "auth": (
                require_env(f"{entry['auth_env']}_USER"),
                require_env(f"{entry['auth_env']}_PASS"),
            ),
        }
        jurisdiction = entry.get("jurisdiction")
        if jurisdiction is not None:
            if not isinstance(jurisdiction, str) or not jurisdiction.strip():
                raise RuntimeError(
                    f"Node {entry['name']!r} in {source} has an invalid jurisdiction"
                )
            node_cfg["jurisdiction"] = jurisdiction.strip()
        dicom_host = entry.get("dicom_host")
        if dicom_host is not None:
            if not isinstance(dicom_host, str) or not dicom_host.strip():
                raise RuntimeError(f"Node {entry['name']!r} in {source} has an invalid dicom_host")
            node_cfg["dicom_host"] = dicom_host.strip()
        dicom_port = entry.get("dicom_port")
        if dicom_port is not None:
            if (
                isinstance(dicom_port, bool)
                or not isinstance(dicom_port, int)
                or not (1 <= dicom_port <= 65535)
            ):
                raise RuntimeError(f"Node {entry['name']!r} in {source} has an invalid dicom_port")
            node_cfg["dicom_port"] = dicom_port
        nodes[entry["name"]] = node_cfg
    return nodes
