"""Fill the ${orthanc_user} and ${orthanc_password} placeholders of an Orthanc config template.

The templates in config/orthanc are JSON, so both values are JSON-escaped before they are
inserted: any password, including one with quotes, backslashes, slashes or ampersands, ends up
in the config exactly as it is written in .env. Uses the standard library only, so the cloud
node containers can run it as a plain script.

Usage: render_orthanc_config.py TEMPLATE OUTPUT   (reads ORTHANC_USER and ORTHANC_PASSWORD)
"""

from __future__ import annotations

import json
import os
import sys
from string import Template


def _escape(value: str) -> str:
    """Escape value for use inside a JSON string."""
    return json.dumps(value)[1:-1]


def render(template: str, user: str, password: str) -> str:
    """Return template with the user and password placeholders filled in (a single pass)."""
    return Template(template).safe_substitute(
        orthanc_user=_escape(user), orthanc_password=_escape(password)
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} TEMPLATE OUTPUT", file=sys.stderr)
        return 2
    try:
        user = os.environ["ORTHANC_USER"]
        password = os.environ["ORTHANC_PASSWORD"]
    except KeyError as exc:
        print(f"{exc.args[0]} environment variable must be set", file=sys.stderr)
        return 1
    with open(argv[1], encoding="utf-8") as template_file:
        template = template_file.read()
    with open(argv[2], "w", encoding="utf-8") as output_file:
        output_file.write(render(template, user, password))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
