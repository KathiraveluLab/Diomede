"""Unit tests for src/simulator/send_dicom_rest.py"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.simulator.send_dicom_rest import _parse_args, send

pytestmark = pytest.mark.unit


def _mock_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = ""
    resp.json.return_value = json_body or {}
    return resp


class TestParseArgs:
    def test_defaults(self):
        with patch("sys.argv", ["send_dicom_rest.py"]), patch.dict("os.environ", {}, clear=True):
            args = _parse_args()
        assert args.base_url == "https://localhost:8042"
        assert args.user == "orthanc"
        assert args.password == "CHANGE_IN_PRODUCTION"
        assert args.ca_cert == "certs/ca.pem"

    def test_env_var_credentials(self):
        with (
            patch("sys.argv", ["send_dicom_rest.py"]),
            patch.dict("os.environ", {"ORTHANC_USER": "admin", "ORTHANC_PASSWORD": "secret"}),
        ):
            args = _parse_args()
        assert args.user == "admin"
        assert args.password == "secret"

    def test_cli_overrides_env_vars(self):
        with (
            patch(
                "sys.argv", ["send_dicom_rest.py", "--user", "cli_user", "--password", "cli_pass"]
            ),
            patch.dict("os.environ", {"ORTHANC_USER": "env_user", "ORTHANC_PASSWORD": "env_pass"}),
        ):
            args = _parse_args()
        assert args.user == "cli_user"
        assert args.password == "cli_pass"

    def test_custom_values(self):
        with patch(
            "sys.argv",
            [
                "send_dicom_rest.py",
                "--base-url",
                "https://10.0.0.1:8042",
                "--user",
                "myuser",
                "--password",
                "mypass",
                "--ca-cert",
                "/tmp/ca.pem",
            ],
        ):
            args = _parse_args()
        assert args.base_url == "https://10.0.0.1:8042"
        assert args.user == "myuser"
        assert args.password == "mypass"
        assert args.ca_cert == "/tmp/ca.pem"


class TestSend:
    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_successful_send(self, mock_ssl, mock_post):
        mock_post.return_value = _mock_response(200, {"Status": "Success"})

        send("https://localhost:8042", "orthanc", "pass", "ca.pem")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == "https://localhost:8042/instances"
        assert call_kwargs.kwargs["headers"] == {"Content-Type": "application/dicom"}
        assert call_kwargs.kwargs["auth"] == ("orthanc", "pass")

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_already_stored_succeeds(self, _mock_ssl, mock_post):
        """Orthanc returns HTTP 200 with Status=AlreadyStored for duplicates."""
        mock_post.return_value = _mock_response(200, {"Status": "AlreadyStored"})

        send("https://localhost:8042", "orthanc", "pass", "ca.pem")  # must not raise

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_auth_failure_exits(self, mock_ssl, mock_post):
        mock_post.return_value = _mock_response(401)

        with pytest.raises(SystemExit) as exc_info:
            send("https://localhost:8042", "wrong", "wrong", "ca.pem")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_server_error_exits(self, mock_ssl, mock_post):
        mock_post.return_value = _mock_response(500)

        with pytest.raises(SystemExit) as exc_info:
            send("https://localhost:8042", "orthanc", "pass", "ca.pem")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_connect_error_exits(self, mock_ssl, mock_post):
        mock_post.side_effect = httpx.ConnectError("connection refused")

        with pytest.raises(SystemExit) as exc_info:
            send("https://127.0.0.1:9999", "orthanc", "pass", "ca.pem")
        assert exc_info.value.code == 1

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_ssl_context_uses_ca_cert(self, mock_ssl, mock_post):
        mock_post.return_value = _mock_response(200)

        send("https://localhost:8042", "orthanc", "pass", "certs/ca.pem")

        mock_ssl.assert_called_once_with(cafile="certs/ca.pem")
        assert mock_post.call_args.kwargs["verify"] == mock_ssl.return_value

    @patch("src.simulator.send_dicom_rest.httpx.post")
    @patch("src.simulator.send_dicom_rest.ssl.create_default_context")
    def test_dicom_bytes_posted(self, mock_ssl, mock_post):
        mock_post.return_value = _mock_response(200)

        send("https://localhost:8042", "orthanc", "pass", "ca.pem")

        content = mock_post.call_args.kwargs["content"]
        # DICOM files start with 128-byte preamble followed by "DICM" magic
        assert content[128:132] == b"DICM"
