"""Unit tests for src/utils/env.py"""

import pytest

from src.utils.env import env_non_negative_float, require_env

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(("value", "expected"), [("500", 500.0), ("0", 0.0), ("0.25", 0.25)])
def test_reads_a_non_negative_number(monkeypatch, value, expected):
    monkeypatch.setenv("SOME_SETTING", value)

    assert env_non_negative_float("SOME_SETTING", 7.0) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_gives_the_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SOME_SETTING", raising=False)
    else:
        monkeypatch.setenv("SOME_SETTING", value)

    assert env_non_negative_float("SOME_SETTING", 7.0) == 7.0


@pytest.mark.parametrize("value", ["abc", "-1", "nan", "inf", "-inf"])
def test_invalid_values_are_rejected_and_name_the_setting(monkeypatch, value):
    monkeypatch.setenv("SOME_SETTING", value)

    with pytest.raises(ValueError, match="SOME_SETTING"):
        env_non_negative_float("SOME_SETTING", 7.0)


def test_require_env_raises_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_SETTING", raising=False)

    with pytest.raises(RuntimeError, match="SOME_SETTING"):
        require_env("SOME_SETTING")
