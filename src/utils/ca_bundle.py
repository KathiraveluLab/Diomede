"""The CA bundle the benchmark scripts use to verify the compose stack's TLS certificates."""

import os
import ssl
from pathlib import Path

CONTAINER_CA_BUNDLE = "/certs/ca.pem"
# parents[2] is the repo root, which assumes a source checkout (the scripts run as python -m).
REPO_CA_BUNDLE = Path(__file__).parents[2] / "certs" / "ca.pem"


def resolve_ca_bundle() -> str:
    """Return the CA bundle to verify the stack with, or raise if there is none.

    .env sets REQUESTS_CA_BUNDLE to the path inside the containers, which does not exist on the
    host, so that path (or none) means the repo's certs/ca.pem. The benchmarks always talk to the
    TLS compose stack and send its API key, so a missing bundle is an error, never a reason to
    skip verification.
    """
    configured = os.getenv("REQUESTS_CA_BUNDLE")
    path = Path(configured) if configured and configured != CONTAINER_CA_BUNDLE else REPO_CA_BUNDLE
    if not path.is_file():
        raise RuntimeError(
            f"CA certificate not found at {path}. Run scripts/gen_certs.sh to create it, or set "
            "REQUESTS_CA_BUNDLE to the CA that signed the stack's certificates."
        )
    return str(path)


def ca_ssl_context() -> ssl.SSLContext:
    """The context to pass as httpx's verify=, which no longer takes a path without a warning."""
    return ssl.create_default_context(cafile=resolve_ca_bundle())
