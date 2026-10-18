"""Unit tests for src/utils/render_orthanc_config.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils import render_orthanc_config
from src.utils.render_orthanc_config import render

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "config" / "orthanc").glob("*.template.json"))
SCRIPT = ROOT / "src" / "utils" / "render_orthanc_config.py"

# Characters that broke the old sed and str.replace substitution.
PASSWORDS = [
    "Simple123",
    'pa"ss',
    "pa\\ss",
    "pa/ss",
    "pa&ss",
    "pa|ss",
    "p$ss",
    "pa ss",
    "pässword",
    '"},{"injected":"x',
    "${orthanc_user}",
]


def _registered_users(rendered: str) -> dict[str, str]:
    users: dict[str, str] = json.loads(rendered)["RegisteredUsers"]
    return users


def test_templates_are_found():
    assert {t.name for t in TEMPLATES} == {
        "af.template.json",
        "asia.template.json",
        "edge.template.json",
        "eu.template.json",
        "us.template.json",
    }


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
@pytest.mark.parametrize("password", PASSWORDS)
def test_any_password_is_stored_unchanged(template, password):
    rendered = render(template.read_text(), "orthanc", password)

    assert _registered_users(rendered) == {"orthanc": password}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_any_user_name_is_stored_unchanged(template):
    rendered = render(template.read_text(), 'us"er\\name', "secret")

    assert _registered_users(rendered) == {'us"er\\name': "secret"}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_plain_values_give_the_same_result_as_before(template):
    text = template.read_text()
    old = text.replace("${orthanc_user}", "orthanc").replace("${orthanc_password}", "Simple123")

    assert render(text, "orthanc", "Simple123") == old


def test_a_value_that_looks_like_a_placeholder_is_not_substituted_again():
    rendered = render(
        '{"u": "${orthanc_user}", "p": "${orthanc_password}"}', "${orthanc_password}", "x"
    )

    assert json.loads(rendered) == {"u": "${orthanc_password}", "p": "x"}


def test_main_writes_the_rendered_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ORTHANC_USER", "orthanc")
    monkeypatch.setenv("ORTHANC_PASSWORD", 'pa"s/s&')
    out = tmp_path / "orthanc.json"

    code = render_orthanc_config.main(["prog", str(TEMPLATES[0]), str(out)])

    assert code == 0
    assert _registered_users(out.read_text()) == {"orthanc": 'pa"s/s&'}


def test_main_reports_a_missing_variable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ORTHANC_PASSWORD", raising=False)
    monkeypatch.setenv("ORTHANC_USER", "orthanc")

    code = render_orthanc_config.main(["prog", str(TEMPLATES[0]), str(tmp_path / "out.json")])

    assert code == 1
    assert "ORTHANC_PASSWORD" in capsys.readouterr().err


def test_main_rejects_wrong_arguments(capsys):
    assert render_orthanc_config.main(["prog"]) == 2
    assert "usage" in capsys.readouterr().err


def test_runs_as_a_standalone_script(tmp_path):
    """The cloud node containers run the file directly, without the rest of the repo."""
    out = tmp_path / "orthanc.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(TEMPLATES[0]), str(out)],
        env={"ORTHANC_USER": "orthanc", "ORTHANC_PASSWORD": 'a"b\\c/d&e', "PATH": ""},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert _registered_users(out.read_text()) == {"orthanc": 'a"b\\c/d&e'}
