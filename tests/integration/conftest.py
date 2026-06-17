"""
Shared fixtures and helpers for all integration tests.

Requires the Docker Compose stack to be running:
    docker compose up -d orthanc-us
"""

import os
import ssl
import time

import httpx
import pytest

from src.simulator.generate_dicom import _RTT_SOP_UID

ORTHANC_URL = "https://localhost:8042"
ORTHANC_AUTH = (
    os.environ.get("ORTHANC_USER", "orthanc"),
    os.environ.get("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION"),
)
CA_CERT = "certs/ca.pem"
_SSL_CTX = ssl.create_default_context(cafile=CA_CERT)


def _delete_instance(sop_uid: str, retries: int = 5, delay: float = 3.0) -> None:
    for attempt in range(retries):
        try:
            resp = httpx.post(
                f"{ORTHANC_URL}/tools/find",
                json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_uid}, "Expand": True},
                auth=ORTHANC_AUTH,
                verify=_SSL_CTX,
            )
            resp.raise_for_status()
            for item in resp.json():
                httpx.delete(
                    f"{ORTHANC_URL}/instances/{item['ID']}",
                    auth=ORTHANC_AUTH,
                    verify=_SSL_CTX,
                ).raise_for_status()
            return
        except httpx.ConnectError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


@pytest.fixture(autouse=True)
def clean_instance():
    """Remove the fixed-UID test instance before and after each test."""
    _delete_instance(_RTT_SOP_UID)
    yield
    _delete_instance(_RTT_SOP_UID)
