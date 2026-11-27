"""Fill the ${orthanc_user} and ${orthanc_password} placeholders of an Orthanc config template.

The templates in config/orthanc are JSON, so both values are JSON-escaped before they are
inserted: any password, including one with quotes, backslashes, slashes or ampersands, ends up
in the config exactly as it is written in .env. Uses the standard library only, so the cloud
node containers can run it as a plain script.

When invoked with ``--nodes-file PATH`` (or when ``CLOUD_NODES`` is provided with
``--inject-dicom-modalities``), the renderer also populates TLS-enabled ``DicomModalities``
entries (with ``UseDicomTls: true`` and ``AllowStorageCommitment: true``) for every regional
node so Edge Orthanc can perform mutual-TLS DIMSE C-STORE, C-ECHO, and Storage Commitment.

Usage:
  render_orthanc_config.py TEMPLATE OUTPUT [--nodes-file NODES_JSON] [--inject-dicom-modalities]
"""

from __future__ import annotations

import json
import os
import sys
from string import Template
from typing import Any
from urllib.parse import urlsplit


def _escape(value: str) -> str:
    """Escape value for use inside a JSON string."""
    return json.dumps(value)[1:-1]


def render(template: str, user: str, password: str) -> str:
    """Return template with the user and password placeholders filled in (a single pass)."""
    return Template(template).safe_substitute(
        orthanc_user=_escape(user), orthanc_password=_escape(password)
    )


def inject_dicom_modalities(rendered_json: str, nodes_raw: str) -> str:
    """Merge TLS-enabled DicomModalities for each cloud node into an Orthanc JSON config."""
    cfg: dict[str, Any] = json.loads(rendered_json)
    entries = json.loads(nodes_raw)
    if not isinstance(entries, list):
        raise ValueError("Node list must be a JSON array")

    modalities: dict[str, Any] = dict(cfg.get("DicomModalities", {}))
    for entry in entries:
        name = entry.get("name")
        ae_title = entry.get("ae_title")
        base = entry.get("base", "")
        if not name or not ae_title or not base:
            continue
        host = entry.get("dicom_host") or urlsplit(base).hostname
        port = int(entry.get("dicom_port", 4242))
        if not host:
            continue
        modalities[name] = {
            "AET": ae_title,
            "Host": host,
            "Port": port,
            "UseDicomTls": True,
            "AllowStorageCommitment": True,
        }
    cfg["DicomModalities"] = modalities
    return json.dumps(cfg, indent=4)


def main(argv: list[str]) -> int:
    args = argv[1:]
    nodes_file: str | None = None
    inject_modalities = False
    positional: list[str] = []

    i = 0
    while i < len(args):
        if args[i] == "--nodes-file" and i + 1 < len(args):
            nodes_file = args[i + 1]
            inject_modalities = True
            i += 2
        elif args[i] == "--inject-dicom-modalities":
            inject_modalities = True
            i += 1
        elif args[i].startswith("--"):
            print(f"unknown option: {args[i]}", file=sys.stderr)
            return 2
        else:
            positional.append(args[i])
            i += 1

    if len(positional) != 2:
        print(
            f"usage: {argv[0]} TEMPLATE OUTPUT [--nodes-file PATH] [--inject-dicom-modalities]",
            file=sys.stderr,
        )
        return 2
    try:
        user = os.environ["ORTHANC_USER"]
        password = os.environ["ORTHANC_PASSWORD"]
    except KeyError as exc:
        print(f"{exc.args[0]} environment variable must be set", file=sys.stderr)
        return 1
    with open(positional[0], encoding="utf-8") as template_file:
        template = template_file.read()
    rendered = render(template, user, password)
    if inject_modalities:
        nodes_raw = os.environ.get("CLOUD_NODES", "").strip()
        if not nodes_raw and nodes_file and os.path.exists(nodes_file):
            with open(nodes_file, encoding="utf-8") as nf:
                nodes_raw = nf.read()
        if nodes_raw:
            rendered = inject_dicom_modalities(rendered, nodes_raw)
    with open(positional[1], "w", encoding="utf-8") as output_file:
        output_file.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
