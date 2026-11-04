"""Unit tests for src/utils/env.py"""

import pytest

from src.utils.env import env_non_negative_float, env_positive_float, env_positive_int, require_env

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


@pytest.mark.parametrize(("value", "expected"), [("30", 30), ("1", 1)])
def test_positive_int_reads_a_whole_number(monkeypatch, value, expected):
    monkeypatch.setenv("SOME_SETTING", value)

    assert env_positive_int("SOME_SETTING", 7) == expected


@pytest.mark.parametrize(("value", "expected"), [("5", 5.0), ("0.25", 0.25)])
def test_positive_float_reads_a_positive_number(monkeypatch, value, expected):
    monkeypatch.setenv("SOME_SETTING", value)

    assert env_positive_float("SOME_SETTING", 7.0) == expected


@pytest.mark.parametrize("value", [None, ""])
def test_positive_helpers_give_the_default_when_unset_or_empty(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SOME_SETTING", raising=False)
    else:
        monkeypatch.setenv("SOME_SETTING", value)

    assert env_positive_int("SOME_SETTING", 7) == 7
    assert env_positive_float("SOME_SETTING", 7.5) == 7.5


@pytest.mark.parametrize("value", ["abc", "0", "-1", "2.5"])
def test_positive_int_rejects_bad_values_and_names_the_setting(monkeypatch, value):
    monkeypatch.setenv("SOME_SETTING", value)

    with pytest.raises(ValueError, match="SOME_SETTING"):
        env_positive_int("SOME_SETTING", 7)


@pytest.mark.parametrize("value", ["abc", "0", "-1", "inf"])
def test_positive_float_rejects_bad_values_and_names_the_setting(monkeypatch, value):
    monkeypatch.setenv("SOME_SETTING", value)

    with pytest.raises(ValueError, match="SOME_SETTING"):
        env_positive_float("SOME_SETTING", 7.0)


@pytest.mark.parametrize("helper", [env_positive_int, env_positive_float])
def test_positive_helpers_without_a_default_are_required_and_validated(monkeypatch, helper):
    monkeypatch.delenv("SOME_SETTING", raising=False)
    with pytest.raises(RuntimeError, match="SOME_SETTING"):
        helper("SOME_SETTING")

    monkeypatch.setenv("SOME_SETTING", "0")
    with pytest.raises(ValueError, match="SOME_SETTING"):
        helper("SOME_SETTING")
