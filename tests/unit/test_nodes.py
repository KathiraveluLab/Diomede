"""Unit tests for the shared cloud node list."""

import json
import os

import pytest

from src.utils import nodes as nodes_module
from src.utils.nodes import load_nodes

pytestmark = pytest.mark.unit

_US = {
    "name": "us-east1",
    "base": "https://orthanc-us:8042",
    "ae_title": "Orthanc_US",
    "auth_env": "NODE_US",
}
_EU = {
    "name": "eu-west1",
    "base": "https://orthanc-eu:8042",
    "ae_title": "Orthanc_EU",
    "auth_env": "NODE_EU",
}


@pytest.fixture(autouse=True)
def _credentials_and_no_env_list(monkeypatch):
    monkeypatch.delenv("CLOUD_NODES", raising=False)
    for name in os.environ:
        if nodes_module._REMOVED_VARS.fullmatch(name):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NODE_US_USER", "us-user")
    monkeypatch.setenv("NODE_US_PASS", "us-pass")
    monkeypatch.setenv("NODE_EU_USER", "eu-user")
    monkeypatch.setenv("NODE_EU_PASS", "eu-pass")


@pytest.fixture
def nodes_file(tmp_path, monkeypatch):
    path = tmp_path / "nodes.json"
    monkeypatch.setattr(nodes_module, "NODES_FILE", path)
    return path


def test_env_variable_wins_over_the_file(monkeypatch, nodes_file):
    nodes_file.write_text(json.dumps([_US]))
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_EU]))

    assert list(load_nodes()) == ["eu-west1"]


@pytest.mark.parametrize("unset", [None, ""])
def test_file_is_used_when_the_env_variable_is_not_set(monkeypatch, nodes_file, unset):
    nodes_file.write_text(json.dumps([_US, _EU]))
    if unset is not None:
        monkeypatch.setenv("CLOUD_NODES", unset)

    assert list(load_nodes()) == ["us-east1", "eu-west1"]


def test_any_number_of_nodes_is_allowed(monkeypatch):
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_US]))

    assert list(load_nodes()) == ["us-east1"]


def test_login_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_US]))

    assert load_nodes()["us-east1"] == {
        "base": "https://orthanc-us:8042",
        "ae_title": "Orthanc_US",
        "auth": ("us-user", "us-pass"),
    }


def test_missing_password_names_the_variable(monkeypatch):
    monkeypatch.delenv("NODE_US_PASS")
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_US]))

    with pytest.raises(RuntimeError, match="NODE_US_PASS"):
        load_nodes()


def test_missing_file_says_what_to_set(nodes_file):
    with pytest.raises(RuntimeError, match="CLOUD_NODES"):
        load_nodes()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "non-empty JSON list"),
        ('{"name": "us-east1"}', "non-empty JSON list"),
        ('["us-east1"]', "must be an object"),
        (json.dumps([{**_US, "base": ""}]), "base"),
        (json.dumps([{k: v for k, v in _US.items() if k != "auth_env"}]), "auth_env"),
        (json.dumps([_US, _US]), "more than once"),
    ],
)
def test_bad_list_is_rejected_at_startup(monkeypatch, value, message):
    monkeypatch.setenv("CLOUD_NODES", value)

    with pytest.raises(RuntimeError, match=message):
        load_nodes()


@pytest.mark.parametrize("base", ["orthanc-us:8042", "htps://orthanc-us:8042", "https://"])
def test_base_that_is_not_a_url_is_rejected(monkeypatch, base):
    monkeypatch.setenv("CLOUD_NODES", json.dumps([{**_US, "base": base}]))

    with pytest.raises(RuntimeError, match="http"):
        load_nodes()


def test_trailing_slash_on_base_is_dropped(monkeypatch):
    monkeypatch.setenv("CLOUD_NODES", json.dumps([{**_US, "base": "https://orthanc-us:8042/"}]))

    assert load_nodes()["us-east1"]["base"] == "https://orthanc-us:8042"


def test_bad_entry_is_not_printed_in_the_error(monkeypatch):
    monkeypatch.setenv("CLOUD_NODES", '["a-password"]')

    with pytest.raises(RuntimeError) as exc:
        load_nodes()
    assert "a-password" not in str(exc.value)


@pytest.mark.parametrize("name", ["NODE_EU_BASE", "REGION1_NAME", "REGION4_AET"])
def test_warns_about_variables_that_no_longer_set_the_nodes(monkeypatch, name):
    warnings = []
    monkeypatch.setattr(nodes_module.log, "warning", lambda msg, *a: warnings.append(msg % a))
    monkeypatch.setenv(name, "custom")
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_US]))

    load_nodes()

    assert len(warnings) == 1
    assert name in warnings[0]


def test_no_warning_for_variables_still_in_use(monkeypatch):
    warnings = []
    monkeypatch.setattr(nodes_module.log, "warning", lambda msg, *a: warnings.append(msg % a))
    monkeypatch.setenv("NODE_INT_BASE", "https://localhost:8042")
    monkeypatch.setenv("REGION1_HOST", "orthanc-us")
    monkeypatch.setenv("CLOUD_NODES", json.dumps([_US]))

    load_nodes()

    assert warnings == []
