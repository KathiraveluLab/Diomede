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
