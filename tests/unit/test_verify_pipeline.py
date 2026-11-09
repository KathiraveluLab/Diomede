"""Unit tests for src/utils/verify_pipeline.py"""

import copy
import os
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pydicom
import pytest
import requests
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from src.simulator.generate_dicom import make_ct_8x8
from src.utils.verify_pipeline import (
    compare_datasets,
    create_secure_session,
    download_remote_dicom,
    main,
    parse_args,
    upload_dicom_to_orthanc,
)

pytestmark = pytest.mark.unit

_GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"


@pytest.fixture(autouse=True)
def _remote_env(monkeypatch):
    """require_env() lookups made at call time by the module under test."""
    monkeypatch.setenv("EDGE_AGENT1_HTTPS_URL", "https://edge:8042")
    monkeypatch.setenv("NODE_US_HTTPS_URL", "https://us:8042")
    monkeypatch.setenv("NODE_EU_HTTPS_URL", "https://eu:8042")
    monkeypatch.setenv("NODE_ASIA_HTTPS_URL", "https://asia:8042")
    monkeypatch.setenv("NODE_AF_HTTPS_URL", "https://af:8042")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")


def _save(ds: pydicom.Dataset, path) -> str:
    """Persist a dataset as a Part-10 file and return its path."""
    ds.save_as(str(path), enforce_file_format=True)
    return str(path)


def _round_trip(ds: pydicom.Dataset, path) -> tuple[str, pydicom.Dataset]:
    """Return (local path, an in-memory dataset identical to what dcmread yields)."""
    local_path = _save(ds, path)
    return local_path, pydicom.dcmread(local_path)


def _gsps_dataset() -> pydicom.Dataset:
    """Grayscale Softcopy Presentation State — no pixel payload, annotation sequences only."""
    ds = make_ct_8x8()
    del ds.PixelData
    ds.SOPClassUID = _GSPS_SOP_CLASS_UID
    ds.file_meta.MediaStorageSOPClassUID = _GSPS_SOP_CLASS_UID

    ref_series = Dataset()
    ref_series.SeriesInstanceUID = "1.2.826.0.1.3680043.0.5.0.3"
    ds.ReferencedSeriesSequence = Sequence([ref_series])
    return ds


def _mock_response(status_code: int, chunks: list[bytes] | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = ""
    resp.iter_content.return_value = chunks or []
    return resp


def _get_context(response: MagicMock) -> MagicMock:
    """Wrap a response so it works with `with session.get(...) as response`."""
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    ctx.__exit__.return_value = False
    return ctx


class TestCreateSecureSession:
    def test_container_path_rewritten_to_local_relative(self, tmp_path, monkeypatch):
        """The in-container /certs/ca.pem is remapped onto the repo-relative bundle."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "certs").mkdir()
        (tmp_path / "certs" / "ca.pem").write_text("cert")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/ca.pem")

        session = create_secure_session("/certs/ca.pem", ("user", "pass"))

        assert session.verify == "./certs/ca.pem"

    def test_container_path_also_sets_curl_bundle(self, tmp_path, monkeypatch):
        """CURL_CA_BUNDLE must follow REQUESTS_CA_BUNDLE or curl-level fallbacks win."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "certs").mkdir()
        (tmp_path / "certs" / "ca.pem").write_text("cert")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/ca.pem")

        create_secure_session("/certs/ca.pem", ("user", "pass"))

        assert os.environ["REQUESTS_CA_BUNDLE"] == "./certs/ca.pem"
        assert os.environ["CURL_CA_BUNDLE"] == "./certs/ca.pem"

    def test_container_path_without_local_cert_exits(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/certs/ca.pem")

        with pytest.raises(SystemExit) as exc_info:
            create_secure_session("/certs/ca.pem", ("user", "pass"))
        assert exc_info.value.code == 1

    def test_explicit_bundle_path_used_as_is(self, tmp_path, monkeypatch):
        ca_file = tmp_path / "custom-ca.pem"
        ca_file.write_text("cert")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))

        session = create_secure_session(str(ca_file), ("user", "pass"))

        assert session.verify == str(ca_file)

    def test_missing_bundle_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "nope.pem"))

        with pytest.raises(SystemExit) as exc_info:
            create_secure_session(str(tmp_path / "nope.pem"), ("user", "pass"))
        assert exc_info.value.code == 1

    def test_auth_attached_to_session(self, tmp_path, monkeypatch):
        ca_file = tmp_path / "ca.pem"
        ca_file.write_text("cert")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))

        session = create_secure_session(str(ca_file), ("orthanc", "secret"))

        assert isinstance(session, requests.Session)
        assert session.auth == ("orthanc", "secret")


class TestUploadDicomToOrthanc:
    def test_returns_instance_id_on_success(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        response = _mock_response(200)
        response.json.return_value = {"ID": "abc-123"}
        session.post.return_value = response

        result = upload_dicom_to_orthanc(session, local_path, ("edge", "pass"))

        assert result == "abc-123"

    def test_posts_raw_dicom_to_instances_endpoint(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        response = _mock_response(200)
        response.json.return_value = {"ID": "abc-123"}
        session.post.return_value = response

        upload_dicom_to_orthanc(session, local_path, ("edge", "pass"))

        args, kwargs = session.post.call_args
        assert args[0] == "https://edge:8042/instances"
        assert kwargs["headers"] == {"Content-Type": "application/dicom"}
        assert kwargs["auth"] == ("edge", "pass")
        # 128-byte preamble followed by the DICM magic
        assert kwargs["data"][128:132] == b"DICM"

    def test_missing_local_file_returns_none(self, tmp_path):
        session = MagicMock()

        result = upload_dicom_to_orthanc(session, str(tmp_path / "absent.dcm"), ("edge", "pass"))

        assert result is None
        session.post.assert_not_called()

    def test_non_200_status_returns_none(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        session.post.return_value = _mock_response(500)

        assert upload_dicom_to_orthanc(session, local_path, ("edge", "pass")) is None

    def test_response_without_id_key_returns_none(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        response = _mock_response(200)
        response.json.return_value = {}
        session.post.return_value = response

        assert upload_dicom_to_orthanc(session, local_path, ("edge", "pass")) is None

    def test_ssl_error_returns_none(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        session.post.side_effect = requests.exceptions.SSLError("bad cert")

        assert upload_dicom_to_orthanc(session, local_path, ("edge", "pass")) is None

    def test_connection_error_returns_none(self, tmp_path):
        local_path = _save(make_ct_8x8(), tmp_path / "in.dcm")
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ConnectionError("refused")

        assert upload_dicom_to_orthanc(session, local_path, ("edge", "pass")) is None


class TestDownloadRemoteDicom:
    @staticmethod
    def _dicom_bytes(tmp_path) -> bytes:
        return (tmp_path / "src.dcm").read_bytes()

    def test_returns_dataset_from_first_node(self, tmp_path):
        _save(make_ct_8x8(), tmp_path / "src.dcm")
        payload = self._dicom_bytes(tmp_path)
        session = MagicMock()
        # An empty keep-alive chunk must be filtered out rather than written
        session.get.return_value = _get_context(_mock_response(200, [payload, b""]))

        result = download_remote_dicom(session, "abc-123")

        assert result.PatientID == "SIM001"
        assert result.pixel_array.shape == (8, 8)

    def test_requests_file_endpoint_for_instance(self, tmp_path):
        _save(make_ct_8x8(), tmp_path / "src.dcm")
        session = MagicMock()
        session.get.return_value = _get_context(_mock_response(200, [self._dicom_bytes(tmp_path)]))

        download_remote_dicom(session, "abc-123")

        args, kwargs = session.get.call_args
        assert args[0] == "https://us:8042/instances/abc-123/file"
        assert kwargs["auth"] == ("orthanc", "CHANGE_IN_PRODUCTION")
        assert kwargs["stream"] is True

    def test_falls_through_404_to_next_node(self, tmp_path):
        _save(make_ct_8x8(), tmp_path / "src.dcm")
        session = MagicMock()
        session.get.side_effect = [
            _get_context(_mock_response(404)),
            _get_context(_mock_response(200, [self._dicom_bytes(tmp_path)])),
        ]

        result = download_remote_dicom(session, "abc-123")

        assert result.PatientID == "SIM001"
        assert session.get.call_count == 2
        assert session.get.call_args_list[1].args[0] == "https://eu:8042/instances/abc-123/file"

    def test_unexpected_status_skips_node(self, tmp_path):
        """A 500 is neither a hit nor a miss — the loop moves on to the next node."""
        _save(make_ct_8x8(), tmp_path / "src.dcm")
        session = MagicMock()
        session.get.side_effect = [
            _get_context(_mock_response(500)),
            _get_context(_mock_response(200, [self._dicom_bytes(tmp_path)])),
        ]

        assert download_remote_dicom(session, "abc-123").PatientID == "SIM001"

    @patch("src.utils.verify_pipeline.time.sleep")
    def test_retries_all_nodes_then_gives_up(self, mock_sleep):
        session = MagicMock()
        session.get.return_value = _get_context(_mock_response(404))

        assert download_remote_dicom(session, "missing-id") is None
        # 10 attempts across 4 nodes, with a backoff sleep after each full sweep
        assert session.get.call_count == 40
        assert mock_sleep.call_count == 10

    def test_http_error_propagates(self):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.HTTPError("boom")

        with pytest.raises(requests.exceptions.HTTPError):
            download_remote_dicom(session, "abc-123")

    def test_unexpected_exception_propagates(self):
        session = MagicMock()
        session.get.side_effect = ValueError("corrupt stream")

        with pytest.raises(ValueError):
            download_remote_dicom(session, "abc-123")


class TestCompareDatasetsMetadata:
    def test_identical_files_pass(self, tmp_path):
        local_path, remote = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")

        assert compare_datasets(local_path, remote) is True

    def test_tag_missing_remotely_fails(self, tmp_path):
        local_path, remote = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        remote = copy.deepcopy(remote)
        del remote.PatientID

        assert compare_datasets(local_path, remote) is False

    def test_tag_added_remotely_fails(self, tmp_path):
        local_path, remote = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        remote = copy.deepcopy(remote)
        remote.StudyDescription = "ADDED BY PIPELINE"

        assert compare_datasets(local_path, remote) is False

    def test_tag_altered_remotely_fails(self, tmp_path):
        local_path, remote = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        remote = copy.deepcopy(remote)
        remote.PatientName = "ANON^ANON"

        assert compare_datasets(local_path, remote) is False


class TestCompareDatasetsPixels:
    def test_no_pixel_data_in_either_file_passes(self, tmp_path):
        ds = make_ct_8x8()
        del ds.PixelData
        local_path, remote = _round_trip(ds, tmp_path / "a.dcm")

        assert compare_datasets(local_path, remote) is True

    def test_pixel_data_missing_on_one_side_fails(self, tmp_path):
        local_path, remote = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        remote = copy.deepcopy(remote)
        del remote.PixelData

        assert compare_datasets(local_path, remote) is False

    def test_shape_mismatch_fails(self, tmp_path):
        local_path, _ = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        smaller = make_ct_8x8()
        smaller.Rows = 4
        smaller.Columns = 4
        smaller.PixelData = bytes(16)

        assert compare_datasets(local_path, smaller) is False

    def test_pixel_value_drift_fails(self, tmp_path):
        local_path, _ = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        mutated = pydicom.dcmread(local_path)
        mutated.PixelData = bytes([7]) + bytes(63)

        assert compare_datasets(local_path, mutated) is False

    def test_pixel_drift_stats_reported(self, tmp_path):
        """Deviation stats are logged so a lossy pipeline can be quantified, not just flagged."""
        local_path, _ = _round_trip(make_ct_8x8(), tmp_path / "a.dcm")
        mutated = pydicom.dcmread(local_path)
        mutated.PixelData = bytes([7]) + bytes(63)

        with patch("src.utils.verify_pipeline.log") as mock_log:
            compare_datasets(local_path, mutated)

        messages = " ".join(str(call.args[0]) for call in mock_log.info.call_args_list)
        assert "Maximum pixel deviation:   7" in messages
        assert "1 / 64 (1.56%)" in messages


class TestCompareDatasetsGsps:
    def test_matching_presentation_state_passes(self, tmp_path):
        local_path, remote = _round_trip(_gsps_dataset(), tmp_path / "gsps.dcm")

        assert compare_datasets(local_path, remote) is True

    def test_referenced_series_dropped_remotely_fails(self, tmp_path):
        local_path, remote = _round_trip(_gsps_dataset(), tmp_path / "gsps.dcm")
        remote = copy.deepcopy(remote)
        del remote.ReferencedSeriesSequence

        assert compare_datasets(local_path, remote) is False

    def test_referenced_series_relinked_fails(self, tmp_path):
        local_path, remote = _round_trip(_gsps_dataset(), tmp_path / "gsps.dcm")
        remote = copy.deepcopy(remote)
        remote.ReferencedSeriesSequence[0].SeriesInstanceUID = "9.9.9.9"

        assert compare_datasets(local_path, remote) is False

    def test_presentation_state_without_references_passes(self, tmp_path):
        ds = _gsps_dataset()
        del ds.ReferencedSeriesSequence
        local_path, remote = _round_trip(ds, tmp_path / "gsps.dcm")

        assert compare_datasets(local_path, remote) is True

    def test_matching_annotation_layers_pass(self, tmp_path):
        ds = _gsps_dataset()
        layer = Dataset()
        layer.GraphicLayer = "LAYER1"
        layer.GraphicLayerOrder = 1
        ds.GraphicLayerSequence = Sequence([layer])
        local_path, remote = _round_trip(ds, tmp_path / "gsps.dcm")

        assert compare_datasets(local_path, remote) is True

    def test_altered_annotation_layer_fails(self, tmp_path):
        ds = _gsps_dataset()
        layer = Dataset()
        layer.GraphicLayer = "LAYER1"
        layer.GraphicLayerOrder = 1
        ds.GraphicLayerSequence = Sequence([layer])
        local_path, remote = _round_trip(ds, tmp_path / "gsps.dcm")
        remote = copy.deepcopy(remote)
        remote.GraphicLayerSequence[0].GraphicLayer = "RENAMED"

        assert compare_datasets(local_path, remote) is False

    def test_annotation_layer_dropped_remotely_fails(self, tmp_path):
        ds = _gsps_dataset()
        text = Dataset()
        text.UnformattedTextValue = "NOTE"
        ds.TextObjectSequence = Sequence([text])
        local_path, remote = _round_trip(ds, tmp_path / "gsps.dcm")
        remote = copy.deepcopy(remote)
        del remote.TextObjectSequence

        assert compare_datasets(local_path, remote) is False

    def test_pixel_checks_bypassed_for_presentation_state(self, tmp_path):
        """GSPS objects carry no pixel matrix; the pixel phase must not be entered."""
        local_path, remote = _round_trip(_gsps_dataset(), tmp_path / "gsps.dcm")

        with patch("src.utils.verify_pipeline.log") as mock_log:
            compare_datasets(local_path, remote)

        messages = " ".join(str(call.args[0]) for call in mock_log.info.call_args_list)
        assert "Bypassing pixel checks" in messages


@pytest.mark.parametrize("presentation_state", [False, True], ids=["no-pixels", "gsps"])
@pytest.mark.parametrize("drift", ["removed", "added", "altered"])
def test_metadata_drift_without_pixels_fails(tmp_path, presentation_state, drift):
    """Skipping pixel checks must not discard the metadata audit result."""
    if presentation_state:
        ds = _gsps_dataset()
    else:
        ds = make_ct_8x8()
        del ds.PixelData
    local_path, remote = _round_trip(ds, tmp_path / "original.dcm")

    if drift == "removed":
        del remote.PatientID
    elif drift == "added":
        remote.StudyDescription = "SYNTHETIC ADDED DESCRIPTION"
    else:
        remote.PatientName = "SYNTHETIC^CHANGED"

    assert compare_datasets(local_path, remote) is False


@pytest.mark.parametrize("presentation_state", [False, True], ids=["no-pixels", "gsps"])
@pytest.mark.parametrize("metadata_changed", [False, True], ids=["unchanged", "changed"])
def test_main_exit_status_without_pixels(tmp_path, presentation_state, metadata_changed):
    """Exercise the real comparison through main, with only transport mocked."""
    if presentation_state:
        ds = _gsps_dataset()
    else:
        ds = make_ct_8x8()
        del ds.PixelData
    local_path, remote = _round_trip(ds, tmp_path / "original.dcm")
    if metadata_changed:
        remote.PatientName = "SYNTHETIC^CHANGED"

    with (
        patch("sys.argv", ["verify_pipeline.py", "--local-path", local_path]),
        patch("src.utils.verify_pipeline.create_secure_session"),
        patch("src.utils.verify_pipeline.upload_dicom_to_orthanc", return_value="synthetic-id"),
        patch("src.utils.verify_pipeline.download_remote_dicom", return_value=remote),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == (1 if metadata_changed else 0)


class TestParseArgs:
    def test_local_path_parsed(self):
        with patch("sys.argv", ["verify_pipeline.py", "--local-path", "/data/scan.dcm"]):
            args = parse_args()
        assert args.local_path == "/data/scan.dcm"

    def test_local_path_is_required(self):
        with patch("sys.argv", ["verify_pipeline.py"]), pytest.raises(SystemExit) as exc_info:
            parse_args()
        assert exc_info.value.code == 2


@patch("src.utils.verify_pipeline.compare_datasets")
@patch("src.utils.verify_pipeline.download_remote_dicom")
@patch("src.utils.verify_pipeline.upload_dicom_to_orthanc")
@patch("src.utils.verify_pipeline.create_secure_session")
@patch("src.utils.verify_pipeline.parse_args")
class TestMain:
    @staticmethod
    def _args() -> Namespace:
        return Namespace(local_path="/data/scan.dcm")

    def test_exits_zero_when_verification_passes(
        self, mock_parse, _session, _upload, _download, mock_compare
    ):
        mock_parse.return_value = self._args()
        mock_compare.return_value = True

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_exits_one_when_variance_detected(
        self, mock_parse, _session, _upload, _download, mock_compare
    ):
        mock_parse.return_value = self._args()
        mock_compare.return_value = False

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_uploaded_instance_id_is_reused_for_download(
        self, mock_parse, _session, mock_upload, mock_download, mock_compare
    ):
        """The instance UID is stable end-to-end, so the upload ID addresses the remote copy."""
        mock_parse.return_value = self._args()
        mock_upload.return_value = "abc-123"
        mock_compare.return_value = True

        with pytest.raises(SystemExit):
            main()

        assert mock_download.call_args.args[1] == "abc-123"
        assert mock_compare.call_args.args[0] == "/data/scan.dcm"

    def test_sessions_built_for_edge_then_node_credentials(
        self, mock_parse, mock_session, _upload, _download, mock_compare
    ):
        mock_parse.return_value = self._args()
        mock_compare.return_value = True

        with pytest.raises(SystemExit):
            main()

        assert mock_session.call_count == 2
        assert mock_session.call_args_list[0].kwargs["auth"] == ("orthanc", "orthanc")
        assert mock_session.call_args_list[1].kwargs["auth"] == (
            "orthanc",
            "CHANGE_IN_PRODUCTION",
        )

    def test_missing_local_file_exits_one(
        self, mock_parse, _session, _upload, mock_download, _compare
    ):
        mock_parse.return_value = self._args()
        mock_download.side_effect = FileNotFoundError("no such file")

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_runtime_error_exits_one(self, mock_parse, _session, _upload, mock_download, _compare):
        mock_parse.return_value = self._args()
        mock_download.side_effect = RuntimeError("orthanc unreachable")

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
