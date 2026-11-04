"""Unit tests for src/utils/ca_bundle.py"""

import ssl

import certifi
import pytest

from src.utils import ca_bundle
from src.utils.ca_bundle import ca_ssl_context, resolve_ca_bundle

pytestmark = pytest.mark.unit


@pytest.fixture
def repo_ca(tmp_path, monkeypatch):
    """A stand-in for the repo's certs/ca.pem, away from the real repo."""
    path = tmp_path / "repo" / "certs" / "ca.pem"
    path.parent.mkdir(parents=True)
    path.write_text("cert")
    monkeypatch.setattr(ca_bundle, "REPO_CA_BUNDLE", path)
    return path


def test_unset_bundle_uses_the_repo_ca(monkeypatch, repo_ca):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    assert resolve_ca_bundle() == str(repo_ca)


def test_container_path_from_env_example_is_remapped_to_the_repo_ca(monkeypatch, repo_ca):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/ca.pem")

    assert resolve_ca_bundle() == str(repo_ca)


def test_repo_ca_is_found_from_any_working_directory(monkeypatch, tmp_path, repo_ca):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    assert resolve_ca_bundle() == str(repo_ca)


def test_a_bundle_set_by_the_user_is_used_as_given(monkeypatch, tmp_path, repo_ca):
    mine = tmp_path / "mine.pem"
    mine.write_text("cert")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(mine))

    assert resolve_ca_bundle() == str(mine)


@pytest.mark.parametrize("configured", [None, "/certs/ca.pem"])
def test_missing_repo_ca_is_an_error_that_points_at_gen_certs(monkeypatch, tmp_path, configured):
    monkeypatch.setattr(ca_bundle, "REPO_CA_BUNDLE", tmp_path / "certs" / "ca.pem")
    if configured is None:
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    else:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", configured)

    with pytest.raises(RuntimeError, match="scripts/gen_certs.sh"):
        resolve_ca_bundle()


def test_missing_bundle_set_by_the_user_is_an_error_not_the_repo_ca(monkeypatch, tmp_path, repo_ca):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "typo.pem"))

    with pytest.raises(RuntimeError, match="typo.pem"):
        resolve_ca_bundle()


def test_ssl_context_verifies_against_the_resolved_bundle(monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())

    context = ca_ssl_context()

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.get_ca_certs()


def test_ssl_context_is_an_error_when_there_is_no_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "missing.pem"))

    with pytest.raises(RuntimeError, match="gen_certs"):
        ca_ssl_context()
