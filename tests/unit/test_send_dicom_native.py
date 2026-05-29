"""Unit tests for src/simulator/send_dicom_native.py"""

import ssl
from unittest.mock import MagicMock, patch

import pytest

from src.simulator.send_dicom_native import _parse_args, send

pytestmark = pytest.mark.unit


class TestParseArgs:
    def test_defaults(self):
        with patch("sys.argv", ["send_dicom_native.py"]):
            args = _parse_args()
        assert args.host == "localhost"
        assert args.port == 4242
        assert args.called_aet == "Orthanc_US"
        assert args.calling_aet == "Simulator"
        assert args.ca_cert == "certs/ca.pem"
        assert args.client_cert == "certs/diomede-client/client.crt"
        assert args.client_key == "certs/diomede-client/client.key"

    def test_custom_values(self):
        with patch(
            "sys.argv",
            [
                "send_dicom_native.py",
                "--host",
                "10.0.0.1",
                "--port",
                "11112",
                "--called-aet",
                "REMOTE",
                "--calling-aet",
                "LOCAL",
                "--ca-cert",
                "/tmp/ca.pem",
                "--client-cert",
                "/tmp/client.crt",
                "--client-key",
                "/tmp/client.key",
            ],
        ):
            args = _parse_args()
        assert args.host == "10.0.0.1"
        assert args.port == 11112
        assert args.called_aet == "REMOTE"
        assert args.calling_aet == "LOCAL"
        assert args.ca_cert == "/tmp/ca.pem"
        assert args.client_cert == "/tmp/client.crt"
        assert args.client_key == "/tmp/client.key"


class TestSend:
    def _make_mock_assoc(self, status_code=0x0000, is_established=True):
        status = MagicMock()
        status.Status = status_code
        assoc = MagicMock()
        assoc.is_established = is_established
        assoc.send_c_store.return_value = status
        return assoc

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_successful_send(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = self._make_mock_assoc(status_code=0x0000)
        mock_ae = mock_ae_cls.return_value
        mock_ae.associate.return_value = assoc

        send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")

        mock_ae.associate.assert_called_once_with(
            "localhost",
            4242,
            ae_title="ORTHANC",
            tls_args=(mock_ssl_ctx_cls.return_value, "localhost"),
        )
        assoc.send_c_store.assert_called_once()
        assoc.release.assert_called_once()

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_association_rejected_exits(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = self._make_mock_assoc(is_established=False)
        mock_ae_cls.return_value.associate.return_value = assoc

        with pytest.raises(SystemExit) as exc_info:
            send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_c_store_failure_exits(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = self._make_mock_assoc(status_code=0xA700)
        mock_ae_cls.return_value.associate.return_value = assoc

        with pytest.raises(SystemExit) as exc_info:
            send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_c_store_none_status_exits(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = MagicMock()
        assoc.is_established = True
        assoc.send_c_store.return_value = None
        mock_ae_cls.return_value.associate.return_value = assoc

        with pytest.raises(SystemExit) as exc_info:
            send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_release_called_even_on_c_store_failure(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = self._make_mock_assoc(status_code=0xA700)
        mock_ae_cls.return_value.associate.return_value = assoc

        with pytest.raises(SystemExit):
            send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")
        assoc.release.assert_called_once()

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_ssl_context_configured(self, mock_ae_cls, mock_ssl_ctx_cls):
        assoc = self._make_mock_assoc()
        mock_ae_cls.return_value.associate.return_value = assoc
        mock_ssl_ctx = mock_ssl_ctx_cls.return_value

        send("localhost", 4242, "ORTHANC", "SIM", "ca.pem", "c.crt", "c.key")

        mock_ssl_ctx_cls.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        mock_ssl_ctx.load_verify_locations.assert_called_once_with("ca.pem")
        mock_ssl_ctx.load_cert_chain.assert_called_once_with(certfile="c.crt", keyfile="c.key")
        assert mock_ssl_ctx.check_hostname is False

    @patch("src.simulator.send_dicom_native.ssl.SSLContext")
    @patch("src.simulator.send_dicom_native.AE")
    def test_ae_title_and_context(self, mock_ae_cls, mock_ssl_ctx_cls):
        from pynetdicom.sop_class import SecondaryCaptureImageStorage

        assoc = self._make_mock_assoc()
        mock_ae = mock_ae_cls.return_value
        mock_ae.associate.return_value = assoc

        send("localhost", 4242, "ORTHANC", "MYSIM", "ca.pem", "c.crt", "c.key")

        mock_ae_cls.assert_called_once_with(ae_title="MYSIM")
        mock_ae.add_requested_context.assert_called_once_with(SecondaryCaptureImageStorage)
