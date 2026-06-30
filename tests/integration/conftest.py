"""
Shared fixtures and helpers for all integration tests.

Requires the Docker Compose stack to be running:
    docker compose up -d
"""

import os
import ssl
import time

import httpx
import pytest

from src.simulator.generate_dicom import _RTT_SOP_UID

ORTHANC_URL = "https://localhost:8042"
EDGE_URL = "https://localhost:8046"
ORTHANC_AUTH = (
    os.environ.get("ORTHANC_USER", "orthanc"),
    os.environ.get("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION"),
)
CA_CERT = "certs/ca.pem"
_SSL_CTX = ssl.create_default_context(cafile=CA_CERT)


@pytest.fixture(scope="session", autouse=True)
def require_stack():
    """Skip the entire integration suite when the Docker Compose stack is not reachable."""
    try:
        httpx.get(f"{ORTHANC_URL}/system", auth=ORTHANC_AUTH, verify=_SSL_CTX, timeout=3)
    except Exception:
        pytest.skip("Docker Compose stack not reachable — run: docker compose up -d")


def _delete_instance(
    base_url: str, sop_uid: str, auth: tuple, verify, retries: int = 5, delay: float = 3.0
) -> None:
    for attempt in range(retries):
        try:
            resp = httpx.post(
                f"{base_url}/tools/find",
                json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_uid}, "Expand": True},
                auth=auth,
                verify=verify,
            )
            resp.raise_for_status()
            for item in resp.json():
                httpx.delete(
                    f"{base_url}/instances/{item['ID']}",
                    auth=auth,
                    verify=verify,
                ).raise_for_status()
            return
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


@pytest.fixture(autouse=True)
def clean_instance(require_stack):
    """Remove the fixed-UID test instance from orthanc-us before and after each test."""
    _delete_instance(ORTHANC_URL, _RTT_SOP_UID, ORTHANC_AUTH, _SSL_CTX)
    yield
    _delete_instance(ORTHANC_URL, _RTT_SOP_UID, ORTHANC_AUTH, _SSL_CTX)
