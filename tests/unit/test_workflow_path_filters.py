"""The CI jobs must run when a file they install, copy or execute changes."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Files each job consumes without them being under src/ or tests/.
CONSUMED = {
    "ci-unit.yml": ["pyproject.toml", "docker-compose.yml"],
    "ci-integration.yml": [
        "pyproject.toml",
        "docker-compose.yml",
        ".env.example",
        "scripts/gen_certs.sh",
        "config/orchestrator/Dockerfile",
        "config/edge/start.sh",
        "config/orthanc/us.template.json",
    ],
}


def _paths(workflow: str, event: str) -> list[str]:
    """The `paths:` globs under one trigger, reading the file line by line."""
    globs: list[str] = []
    in_event = in_paths = False
    for line in (WORKFLOWS / workflow).read_text().splitlines():
        if re.match(r"^  \w+:\s*$", line):
            in_event, in_paths = line.strip() == f"{event}:", False
        elif in_event and re.match(r"^    paths:\s*$", line):
            in_paths = True
        elif in_event and in_paths:
            item = re.match(r"^      - '(.+)'\s*$", line)
            if not item:
                break
            globs.append(item.group(1))
    return globs


def _matches(glob: str, path: str) -> bool:
    pattern = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    return re.fullmatch(pattern.replace(r"\*", "[^/]*"), path) is not None


def test_the_path_reader_finds_the_filters():
    # Guards the parser: no globs would make the checks below pass vacuously.
    assert "src/**" in _paths("ci-unit.yml", "push")
    assert "**/requirements.txt" in _paths("ci-integration.yml", "pull_request")


@pytest.mark.parametrize("workflow", sorted(CONSUMED))
@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_every_consumed_file_triggers_the_workflow(workflow, event):
    globs = _paths(workflow, event)

    unwatched = [p for p in CONSUMED[workflow] if not any(_matches(g, p) for g in globs)]

    assert unwatched == []
