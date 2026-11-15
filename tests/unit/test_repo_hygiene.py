"""Files the .gitignore excludes must not be tracked in the repository."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout"
)
def test_no_tracked_file_matches_gitignore():
    # A generated file committed before its ignore rule keeps showing up as modified.
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--ignored", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.split() == []
