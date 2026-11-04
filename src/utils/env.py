import math
import os


def require_env(name: str) -> str:
    """Return the environment variable's value, or raise if it's unset/empty."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} environment variable must be set")
    return value


def env_non_negative_float(name: str, default: float) -> float:
    """Return a finite, non-negative number from the environment, or default if unset or empty."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative number, got {raw!r}")
    return value


def env_positive_int(name: str, default: int | None = None) -> int:
    """Return a whole number above zero from the environment.

    With a default, an unset or empty variable gives the default (which is not checked).
    Without one, the variable is required.
    """
    if default is not None and not os.getenv(name):
        return default
    raw = require_env(name)
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number above zero, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be a whole number above zero, got {raw!r}")
    return value


def env_positive_float(name: str, default: float | None = None) -> float:
    """Return a finite number above zero from the environment.

    With a default, an unset or empty variable gives the default (which is not checked).
    Without one, the variable is required.
    """
    if default is not None and not os.getenv(name):
        return default
    raw = require_env(name)
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number above zero, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number above zero, got {raw!r}")
    return value
