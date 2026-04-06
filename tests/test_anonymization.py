"""
Tests for the DICOM Anonymization Module.

Covers:
  - Directory batch anonymization via Niffler
  - Flask REST API endpoint (/anonymization/directory)
"""

import json

import pydicom
import pytest
from pydicom.dataset import FileDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from Diomedex import create_app
from Diomedex.anonymization.core import DICOMAnonymizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CT_CLASS_UID = "1.2.840.10008.5.1.4.1.1.2"


def _make_dicom_file(tmp_path, filename: str = "test.dcm", **tags) -> tuple:
    """Write a minimal valid DICOM Part-10 file and return (path, dataset)."""
    sop_uid = tags.pop("SOPInstanceUID", generate_uid())

    file_meta = pydicom.Dataset()
    file_meta.MediaStorageSOPClassUID = CT_CLASS_UID
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(
        str(tmp_path / filename),
        {},
        file_meta=file_meta,
        preamble=b"\x00" * 128,
    )
    ds.SOPClassUID = CT_CLASS_UID
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = tags.pop("StudyInstanceUID", generate_uid())
    ds.SeriesInstanceUID = tags.pop("SeriesInstanceUID", generate_uid())
    ds.PatientName = tags.pop("PatientName", "Doe^John")
    ds.PatientID = tags.pop("PatientID", "P001")
    ds.PatientBirthDate = tags.pop("PatientBirthDate", "19800101")
    ds.PatientSex = tags.pop("PatientSex", "M")
    ds.InstitutionName = tags.pop("InstitutionName", "Test Hospital")
    ds.ReferringPhysicianName = tags.pop("ReferringPhysicianName", "Smith^Jane")
    ds.AccessionNumber = tags.pop("AccessionNumber", "ACC001")
    ds.Modality = tags.pop("Modality", "CT")
    for key, val in tags.items():
        setattr(ds, key, val)

    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(path))
    return path, ds


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path_factory):
    """Create a Flask app for testing with a configured STORAGE_PATH."""
    storage_root = tmp_path_factory.getbasetemp()
    return create_app(
        test_config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "STORAGE_PATH": str(storage_root),
        }
    )


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. Directory batch anonymization
# ---------------------------------------------------------------------------


class TestAnonymizeDirectory:
    def test_all_dicom_files_processed(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        for i in range(3):
            _make_dicom_file(src, f"f{i}.dcm")

        stats = DICOMAnonymizer().anonymize_directory(src, dest)
        assert stats["processed"] == 3
        assert stats["failed"] == 0

    def test_non_dicom_files_are_ignored(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "real.dcm")
        (src / "note.txt").write_text("not dicom")

        # Niffler's get_dcm_paths only finds *.dcm files so the txt is never seen
        stats = DICOMAnonymizer().anonymize_directory(src, dest)
        assert stats["processed"] == 1
        assert stats["failed"] == 0

    def test_empty_directory_returns_zero_stats(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        stats = DICOMAnonymizer().anonymize_directory(src, tmp_path / "dest")
        assert stats == {"processed": 0, "skipped": 0, "failed": 0}

    def test_same_src_and_dest_raises(self, tmp_path):
        with pytest.raises(ValueError):
            DICOMAnonymizer().anonymize_directory(tmp_path, tmp_path)


# ---------------------------------------------------------------------------
# 2. Flask API endpoint
# ---------------------------------------------------------------------------


class TestAnonymizationAPI:
    def test_directory_endpoint_success(self, client, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "f1.dcm")
        _make_dicom_file(src, "f2.dcm")

        resp = client.post(
            "/anonymization/directory",
            data=json.dumps({"src": str(src), "dest": str(dest)}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["stats"]["processed"] == 2
        assert data["stats"]["failed"] == 0

    def test_directory_endpoint_missing_params(self, client, tmp_path):
        resp = client.post(
            "/anonymization/directory",
            data=json.dumps({"src": str(tmp_path)}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_directory_endpoint_no_body_returns_400(self, client):
        resp = client.post("/anonymization/directory", content_type="application/json")
        assert resp.status_code == 400

    def test_endpoint_rejects_relative_path(self, client, tmp_path):
        resp = client.post(
            "/anonymization/directory",
            data=json.dumps({"src": "relative/path", "dest": str(tmp_path / "out")}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "Path must be absolute" in resp.get_json()["error"]

    def test_endpoint_rejects_dest_outside_storage(self, client, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        resp = client.post(
            "/anonymization/directory",
            data=json.dumps({"src": str(src), "dest": "/tmp/escaped_anon"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "within configured storage area" in resp.get_json()["error"]
