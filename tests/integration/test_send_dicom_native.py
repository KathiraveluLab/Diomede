"""
Integration tests for native DICOM operations (C-STORE, C-FIND) against Orthanc via DIMSE-TLS.

Requires the Docker Compose stack to be running:
    docker compose up -d orthanc-us

"""

import os
import ssl

import httpx
import pytest
from pydicom import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import (
    SecondaryCaptureImageStorage,
    StudyRootQueryRetrieveInformationModelFind,
)

from src.simulator.generate_dicom import _RTT_SOP_UID, _RTT_STUDY_UID
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

_DICOM_DEFAULTS = dict(
    host="localhost",
    port=4242,
    called_aet="Orthanc_US",
    calling_aet="Simulator",
    ca_cert=CA_CERT,
    client_cert=CLIENT_CERT,
    client_key=CLIENT_KEY,
)


def _delete_instance(sop_uid: str) -> None:
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


@pytest.fixture(autouse=True)
def clean_instance():
    """Remove the fixed-UID test instance before and after each test."""
    _delete_instance(_RTT_SOP_UID)
    yield
    _delete_instance(_RTT_SOP_UID)


def _send(**overrides) -> None:
    send(**{**_DICOM_DEFAULTS, **overrides})


def _cfind(query_ds: Dataset, **overrides) -> list[Dataset]:
    """Issue a C-FIND against Orthanc and return all pending-status identifiers."""
    cfg = {**_DICOM_DEFAULTS, **overrides}

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.load_verify_locations(cfg["ca_cert"])
    ssl_ctx.load_cert_chain(certfile=cfg["client_cert"], keyfile=cfg["client_key"])
    ssl_ctx.check_hostname = False

    ae = AE(ae_title=cfg["calling_aet"])
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)

    assoc = ae.associate(
        cfg["host"],
        cfg["port"],
        ae_title=cfg["called_aet"],
        tls_args=(ssl_ctx, cfg["host"]),
    )
    if not assoc.is_established:
        raise RuntimeError(
            f"Association rejected by {cfg['called_aet']} at {cfg['host']}:{cfg['port']}"
        )

    results: list[Dataset] = []
    try:
        for status, identifier in assoc.send_c_find(
            query_ds, StudyRootQueryRetrieveInformationModelFind
        ):
            code = getattr(status, "Status", None)
            if code is None:
                raise RuntimeError("C-FIND failed: presentation context not accepted")
            if code in (0xFF00, 0xFF01):
                if identifier is not None:
                    results.append(identifier)
            elif code != 0x0000:
                raise RuntimeError(f"C-FIND failed with status 0x{code:04X}")
    finally:
        assoc.release()

    return results


class TestCStore:
    def test_wrong_called_aet_rejected(self):
        """DicomCheckCalledAet=true: mismatched called AET causes association rejection."""
        with pytest.raises(SystemExit) as exc_info:
            _send(called_aet="WRONG_AET")
        assert exc_info.value.code == 1

    def test_wrong_calling_aet_accepted(self):
        # DicomCheckCallingAet is not enforced for C-STORE; Orthanc accepts the
        # association regardless of whether the calling AET is registered.
        _send(calling_aet="WRONG_AET")

    def test_unreachable_port_exits(self):
        """No listener on port → association fails, exits with code 1."""
        with pytest.raises(SystemExit) as exc_info:
            _send(port=9999)
        assert exc_info.value.code == 1


class TestCFind:
    def test_find_stored_instance(self):
        """C-FIND at IMAGE level returns the instance after C-STORE."""
        _send()

        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.PatientID = "SIM001"
        query.StudyInstanceUID = _RTT_STUDY_UID
        query.SeriesInstanceUID = ""  # required hierarchy key at IMAGE level
        query.SOPInstanceUID = ""  # empty = return all matching values

        results = _cfind(query)
        found = [str(r.SOPInstanceUID) for r in results if hasattr(r, "SOPInstanceUID")]
        assert _RTT_SOP_UID in found

    def test_find_returns_empty_before_store(self):
        """C-FIND returns no results when the instance has not been stored yet."""
        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.PatientID = "SIM001"
        query.SOPInstanceUID = _RTT_SOP_UID

        assert _cfind(query) == []

    def test_wrong_called_aet_rejected(self):
        """DicomCheckCalledAet=true: mismatched called AET causes association rejection."""
        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.SOPInstanceUID = _RTT_SOP_UID

        with pytest.raises(RuntimeError, match="Association rejected"):
            _cfind(query, called_aet="WRONG_AET")

    def test_wrong_calling_aet_rejected(self):
        # Orthanc accepts the association but rejects the C-FIND presentation
        # context for unregistered calling AETs (enforced at context negotiation,
        # not at the association level).
        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.SOPInstanceUID = _RTT_SOP_UID

        with pytest.raises(RuntimeError, match="presentation context not accepted"):
            _cfind(query, calling_aet="WRONG_AET")

    def test_unreachable_port_raises(self):
        """No listener on port → associate fails with RuntimeError."""
        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.SOPInstanceUID = _RTT_SOP_UID

        with pytest.raises(RuntimeError, match="Association rejected"):
            _cfind(query, port=9999)


class TestSSL:
    def test_cstore_wrong_ca_cert(self):
        """C-STORE fails when the client cannot verify the server cert (wrong CA)."""
        # CLIENT_CERT is a leaf cert, not a CA — load_verify_locations accepts it
        # but the TLS handshake fails because it cannot verify the server's cert chain.
        with pytest.raises(SystemExit) as exc_info:
            _send(ca_cert=CLIENT_CERT)
        assert exc_info.value.code == 1

    def test_cstore_no_client_cert(self):
        """
        C-STORE fails when no client cert is presented and DicomTlsRemoteCertificateRequired=true.
        """
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.load_verify_locations(CA_CERT)
        ssl_ctx.check_hostname = False

        ae = AE(ae_title="Simulator")
        ae.add_requested_context(SecondaryCaptureImageStorage)
        assoc = ae.associate(
            "localhost", 4242, ae_title="Orthanc_US", tls_args=(ssl_ctx, "localhost")
        )
        assert not assoc.is_established

    def test_cfind_wrong_ca_cert(self):
        """C-FIND fails when the client cannot verify the server cert (wrong CA)."""
        query = Dataset()
        query.QueryRetrieveLevel = "IMAGE"
        query.SOPInstanceUID = _RTT_SOP_UID

        with pytest.raises(RuntimeError, match="Association rejected"):
            _cfind(query, ca_cert=CLIENT_CERT)

    def test_cfind_no_client_cert(self):
        """
        C-FIND fails when no client cert is presented and DicomTlsRemoteCertificateRequired=true.
        """
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.load_verify_locations(CA_CERT)
        ssl_ctx.check_hostname = False

        ae = AE(ae_title="Simulator")
        ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind)
        assoc = ae.associate(
            "localhost", 4242, ae_title="Orthanc_US", tls_args=(ssl_ctx, "localhost")
        )
        assert not assoc.is_established
