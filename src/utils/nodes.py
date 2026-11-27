"""The cloud node list shared by the telemetry daemon, the orchestrator and the forwarder."""

import json
import os
import re
from pathlib import Path
from typing import TypedDict
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


def load_nodes() -> dict[str, NodeConfig]:
    """Return the nodes in the CLOUD_NODES env variable, or in config/nodes.json if it is unset.

    A node's auth_env names the variables that hold its login (NODE_US means NODE_US_USER and
    NODE_US_PASS), which keeps passwords out of the list.
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
        nodes[entry["name"]] = {
            "base": entry["base"].rstrip("/"),
            "ae_title": entry["ae_title"],
            "auth": (
                require_env(f"{entry['auth_env']}_USER"),
                require_env(f"{entry['auth_env']}_PASS"),
            ),
        }
    return nodes
