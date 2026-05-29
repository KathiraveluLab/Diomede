"""
Integration tests for src/simulator/send_dicom_native.py

Requires the Docker Compose stack to be running:
    docker compose up -d orthanc-us

Validates the full DIMSE-TLS path: simulator → orthanc-us → stored instance.
"""

import os
import ssl

import httpx
import pytest

from src.simulator.generate_dicom import _RTT_SOP_UID
from src.simulator.send_dicom_native import send

pytestmark = pytest.mark.integration

ORTHANC_URL = "https://localhost:8042"
ORTHANC_AUTH = (
    os.environ.get("ORTHANC_USER", "orthanc"),
    os.environ.get("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION"),
)
CA_CERT = "certs/ca.pem"
_SSL_CTX = ssl.create_default_context(cafile=CA_CERT)
CLIENT_CERT = "certs/diomede-client/client.crt"
CLIENT_KEY = "certs/diomede-client/client.key"


def _lookup(sop_uid: str) -> list[dict]:
    resp = httpx.post(
        f"{ORTHANC_URL}/tools/lookup",
        content=sop_uid,
        auth=ORTHANC_AUTH,
        verify=_SSL_CTX,
    )
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def _delete_instance(sop_uid: str) -> None:
    for item in _lookup(sop_uid):
        if item["Type"] == "Instance":
            httpx.delete(
                f"{ORTHANC_URL}{item['Path']}",
                auth=ORTHANC_AUTH,
                verify=_SSL_CTX,
            ).raise_for_status()


@pytest.fixture(autouse=True)
def clean_instance():
    """Remove the fixed-UID test instance before and after each test."""
    _delete_instance(_RTT_SOP_UID)
    yield
    _delete_instance(_RTT_SOP_UID)


def _send_to_us(**kwargs) -> None:
    defaults = dict(
        host="localhost",
        port=4242,
        called_aet="Orthanc_US",
        calling_aet="Simulator",
        ca_cert=CA_CERT,
        client_cert=CLIENT_CERT,
        client_key=CLIENT_KEY,
    )
    send(**{**defaults, **kwargs})


class TestCStore:
    def test_instance_stored(self):
        """C-STORE succeeds and the instance is queryable via Orthanc REST."""
        _send_to_us()
        results = _lookup(_RTT_SOP_UID)
        assert any(item["Type"] == "Instance" for item in results)

    def test_wrong_called_aet_rejected(self):
        """DicomCheckCalledAet=true: mismatched called AET causes association rejection."""
        with pytest.raises(SystemExit) as exc_info:
            _send_to_us(called_aet="WRONG_AET")
        assert exc_info.value.code == 1

    def test_unreachable_port_exits(self):
        """No listener on port → association fails, exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _send_to_us(port=9999)
        assert exc_info.value.code == 1
