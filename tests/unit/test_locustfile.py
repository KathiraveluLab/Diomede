"""Unit tests for tests/load/locustfile.py"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = """
import dotenv, importlib, os, sys
real = dotenv.load_dotenv
dotenv.load_dotenv = lambda *a, **kw: real(dotenv_path=sys.argv[1], **kw)
mod = importlib.import_module("tests.load.locustfile")
print(mod.BURST_COUNT)
"""


@pytest.mark.skipif(importlib.util.find_spec("locust") is None, reason="locust not installed")
def test_environment_overrides_dotenv_for_load_settings(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LOAD_FILE_SIZE_KB=50\nLOAD_BURST_COUNT=0\nLOAD_MAX_P99_MS=50\n"
        "LOAD_MAX_FAIL_RATIO=0.005\nLOAD_MIN_INTERVAL=0.02\nLOAD_MAX_INTERVAL=0.1\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(env_file)],
        cwd=_ROOT,
        env={"PATH": "", "PYTHONPATH": str(_ROOT), "LOAD_BURST_COUNT": "100"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "100"
