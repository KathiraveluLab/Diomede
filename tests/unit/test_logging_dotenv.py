"""Unit tests for LOG_LEVEL set only in a .env file reaching the service loggers."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Run in a fresh interpreter, because a logger reads LOG_LEVEL once, when it is created.
# load_dotenv is pointed at a temporary .env, so a developer's real .env cannot leak in.
_SCRIPT = """
import importlib
import sys

import dotenv

real_load_dotenv = dotenv.load_dotenv
dotenv.load_dotenv = lambda *args, **kwargs: real_load_dotenv(sys.argv[2])
print(importlib.import_module(sys.argv[1]).log.level)
"""


@pytest.mark.parametrize(
    "module",
    [
        "src.edge.orthanc_source",
        "src.edge.forwarder",
        "src.orchestrator.daemon",
        "src.orchestrator.main",
    ],
)
def test_log_level_from_dotenv_applies_to_the_logger(module, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}

    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, module, str(env_file)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == logging.DEBUG
