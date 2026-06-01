"""
Integration tests for src/simulator/send_dicom_rest.py

Requires the Docker Compose stack to be running:
    docker compose up -d orthanc-us

"""

import httpx
import pytest

from src.simulator.generate_dicom import _RTT_SOP_UID
from src.simulator.send_dicom_rest import send
from tests.integration.conftest import _SSL_CTX, CA_CERT, ORTHANC_AUTH, ORTHANC_URL

pytestmark = pytest.mark.integration

_REST_DEFAULTS = dict(
    base_url=ORTHANC_URL,
    user=ORTHANC_AUTH[0],
    password=ORTHANC_AUTH[1],
    ca_cert=CA_CERT,
)


def _send(**overrides) -> None:
    send(**{**_REST_DEFAULTS, **overrides})


class TestRestSend:
    def test_instance_stored(self):
        """POST /instances succeeds and the instance appears in Orthanc."""
        _send()

        resp = httpx.post(
            f"{ORTHANC_URL}/tools/find",
            json={"Level": "Instance", "Query": {"SOPInstanceUID": _RTT_SOP_UID}},
            auth=ORTHANC_AUTH,
            verify=_SSL_CTX,
        )
        resp.raise_for_status()
        assert len(resp.json()) == 1

    def test_idempotent_second_send(self):
        """Sending the same instance twice succeeds — Orthanc returns 200 both times."""
        _send()
        _send()  # must not raise SystemExit

    def test_wrong_credentials_exits(self):
        """Wrong HTTP Basic Auth credentials → Orthanc returns 401 → exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _send(user="wrong", password="wrong")
        assert exc_info.value.code == 1

    def test_wrong_ca_cert_exits(self):
        """Client uses wrong CA cert → TLS handshake fails → exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _send(ca_cert="certs/diomede-client/client.crt")
        assert exc_info.value.code == 1

    def test_unreachable_url_exits(self):
        """No server at the given URL → connection refused → exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _send(base_url="https://127.0.0.1:9999")
        assert exc_info.value.code == 1
