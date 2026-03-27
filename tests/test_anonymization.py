"""
Tests for the DICOM Anonymization Module.

Covers:
  - In-memory dataset anonymization (PHI blanking, UID remapping, stamps)
  - File-level anonymization (read → anonymize → write)
  - Directory batch anonymization (patient ID mapping, stats)
  - Flask REST API endpoints (/anonymization/file, /anonymization/directory)
"""

import json
import pydicom
import pytest
from pydicom.dataset import FileDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from Diomedex import create_app
from Diomedex.anonymization.core import DICOMAnonymizer, _PHI_TAGS, _UID_TAGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CT_CLASS_UID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage

def _make_dataset(
    patient_name: str = "Doe^John",
    patient_id: str = "P001",
    study_uid: str = None,
    series_uid: str = None,
    sop_uid: str = None,
    **extra,
) -> pydicom.Dataset:
    """Return a minimal in-memory pydicom Dataset populated with test PHI."""
    ds = pydicom.Dataset()
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19800101"
    ds.PatientSex = "M"
    ds.PatientAge = "046Y"
    ds.PatientWeight = "70"
    ds.PatientComments = "test comment"
    ds.InstitutionName = "Test Hospital"
    ds.InstitutionAddress = "123 Main St"
    ds.InstitutionalDepartmentName = "Radiology"
    ds.ReferringPhysicianName = "Smith^Jane"
    ds.PerformingPhysicianName = "Jones^Bob"
    ds.AccessionNumber = "ACC001"
    ds.Modality = "CT"
    ds.StudyDate = "20260101"
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.SOPInstanceUID = sop_uid or generate_uid()
    for key, val in extra.items():
        setattr(ds, key, val)
    return ds


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
def anonymizer():
    return DICOMAnonymizer()

@pytest.fixture
def sample_ds():
    return _make_dataset()

@pytest.fixture
def app(tmp_path_factory):
    """Create a Flask app for testing with a configured STORAGE_PATH."""
    # Use the pytest base temp directory as the allowed storage area for tests.
    storage_root = tmp_path_factory.getbasetemp()
    return create_app(test_config={
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "STORAGE_PATH": str(storage_root),
    })

@pytest.fixture
def client(app):
    return app.test_client()

# ---------------------------------------------------------------------------
# 1. In-memory dataset — PHI blanking
# ---------------------------------------------------------------------------

class TestAnonymizeDataset:

    def test_all_phi_tags_are_blanked(self, anonymizer, sample_ds):
        anonymizer.anonymize_dataset(sample_ds)
        for keyword in _PHI_TAGS:
            if hasattr(sample_ds, keyword):
                assert str(getattr(sample_ds, keyword)) == "", (
                    f"Expected {keyword} to be blank after anonymization"
                )

    def test_patient_id_override_sets_name_and_id(self, anonymizer, sample_ds):
        anonymizer.anonymize_dataset(sample_ds, patient_id="ANON0042")
        assert str(sample_ds.PatientName) == "ANON0042"
        assert str(sample_ds.PatientID) == "ANON0042"

    def test_non_phi_tags_are_preserved(self, anonymizer, sample_ds):
        anonymizer.anonymize_dataset(sample_ds)
        assert sample_ds.Modality == "CT"
        assert sample_ds.StudyDate == "20260101"

    def test_deidentification_stamp_applied(self, anonymizer, sample_ds):
        anonymizer.anonymize_dataset(sample_ds)
        assert sample_ds.PatientIdentityRemoved == "YES"
        assert sample_ds.DeidentificationMethod == "Diomede Basic Profile"

    def test_uids_are_regenerated(self, anonymizer, sample_ds):
        original_uids = {kw: str(getattr(sample_ds, kw)) for kw in _UID_TAGS}
        anonymizer.anonymize_dataset(sample_ds)
        for keyword in _UID_TAGS:
            assert str(getattr(sample_ds, keyword)) != original_uids[keyword], (
                f"{keyword} should have been replaced with a new UID"
            )

    def test_uid_consistency_within_same_session(self, anonymizer):
        """Two datasets sharing a StudyInstanceUID must receive the same new UID."""
        shared_study = generate_uid()
        ds1 = _make_dataset(study_uid=shared_study)
        ds2 = _make_dataset(study_uid=shared_study)

        anonymizer.anonymize_dataset(ds1)
        anonymizer.anonymize_dataset(ds2)

        assert ds1.StudyInstanceUID == ds2.StudyInstanceUID
        assert str(ds1.StudyInstanceUID) != shared_study

    def test_uid_independence_across_sessions(self):
        """Two separate DICOMAnonymizer instances must not share UID mappings."""
        shared_study = generate_uid()
        ds1 = _make_dataset(study_uid=shared_study)
        ds2 = _make_dataset(study_uid=shared_study)

        DICOMAnonymizer().anonymize_dataset(ds1)
        DICOMAnonymizer().anonymize_dataset(ds2)

        # Both get different new UIDs because each instance has its own map.
        assert ds1.StudyInstanceUID != ds2.StudyInstanceUID

    def test_returns_same_dataset_object(self, anonymizer, sample_ds):
        result = anonymizer.anonymize_dataset(sample_ds)
        assert result is sample_ds

    def test_missing_phi_tags_do_not_raise(self, anonymizer):
        """Dataset with no PHI tags must be processed without errors."""
        ds = pydicom.Dataset()
        ds.Modality = "MR"
        ds.StudyInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        ds.SOPInstanceUID = generate_uid()
        # No exception expected
        anonymizer.anonymize_dataset(ds)
        assert ds.PatientIdentityRemoved == "YES"

    def test_idempotent_on_already_blank_dataset(self, anonymizer):
        """Anonymizing an already-anonymized dataset must not raise."""
        ds = _make_dataset()
        anonymizer.anonymize_dataset(ds)
        anonymizer.anonymize_dataset(ds)  # second pass — should be safe


# ---------------------------------------------------------------------------
# 2. File-level anonymization
# ---------------------------------------------------------------------------

class TestAnonymizeFile:

    def test_output_file_is_created(self, anonymizer, tmp_path):
        src, _ = _make_dicom_file(tmp_path / "src", "input.dcm")
        dest = tmp_path / "out" / "anon.dcm"
        assert anonymizer.anonymize_file(src, dest) is True
        assert dest.exists()

    def test_output_file_phi_is_cleared(self, anonymizer, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm", PatientName="Real^Name")
        dest = tmp_path / "anon.dcm"
        anonymizer.anonymize_file(src, dest)
        result = pydicom.dcmread(str(dest))
        assert str(result.PatientName) == ""

    def test_output_file_uid_is_changed(self, anonymizer, tmp_path):
        src, original = _make_dicom_file(tmp_path, "input.dcm")
        dest = tmp_path / "anon.dcm"
        anonymizer.anonymize_file(src, dest)
        result = pydicom.dcmread(str(dest))
        assert str(result.StudyInstanceUID) != str(original.StudyInstanceUID)

    def test_patient_id_written_to_output_file(self, anonymizer, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm")
        dest = tmp_path / "anon.dcm"
        anonymizer.anonymize_file(src, dest, patient_id="TESTID")
        result = pydicom.dcmread(str(dest))
        assert str(result.PatientID) == "TESTID"
        assert str(result.PatientName) == "TESTID"

    def test_invalid_source_returns_false(self, anonymizer, tmp_path):
        bad_file = tmp_path / "bad.dcm"
        bad_file.write_bytes(b"this is not dicom data")
        dest = tmp_path / "out.dcm"
        assert anonymizer.anonymize_file(bad_file, dest) is False
        assert not dest.exists()

    def test_parent_directories_are_created(self, anonymizer, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm")
        dest = tmp_path / "a" / "b" / "c" / "anon.dcm"
        assert anonymizer.anonymize_file(src, dest) is True
        assert dest.exists()

    def test_modality_preserved_in_output_file(self, anonymizer, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm", Modality="MR")
        dest = tmp_path / "anon.dcm"
        anonymizer.anonymize_file(src, dest)
        result = pydicom.dcmread(str(dest))
        assert result.Modality == "MR"


# ---------------------------------------------------------------------------
# 3. Directory batch anonymization
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
        assert stats["skipped"] == 0

    def test_non_dicom_files_are_skipped(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "real.dcm")
        (src / "note.txt").write_text("not dicom")

        stats = DICOMAnonymizer().anonymize_directory(src, dest)
        assert stats["processed"] == 1
        assert stats["skipped"] == 1

    def test_directory_structure_mirrored(self, tmp_path):
        src = tmp_path / "src"
        sub = src / "sub"
        dest = tmp_path / "dest"
        sub.mkdir(parents=True)
        _make_dicom_file(src, "root.dcm")
        _make_dicom_file(sub, "nested.dcm")

        DICOMAnonymizer().anonymize_directory(src, dest)
        assert (dest / "root.dcm").exists()
        assert (dest / "sub" / "nested.dcm").exists()

    def test_same_patient_gets_consistent_anon_id(self, tmp_path):
        shared_pid = "SHARED_PATIENT"
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "a.dcm", PatientID=shared_pid)
        _make_dicom_file(src, "b.dcm", PatientID=shared_pid)

        DICOMAnonymizer().anonymize_directory(src, dest)
        result_a = pydicom.dcmread(str(dest / "a.dcm"))
        result_b = pydicom.dcmread(str(dest / "b.dcm"))
        assert str(result_a.PatientID) == str(result_b.PatientID)
        assert str(result_a.PatientID) != shared_pid

    def test_different_patients_get_different_anon_ids(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "p1.dcm", PatientID="PATIENT_ONE")
        _make_dicom_file(src, "p2.dcm", PatientID="PATIENT_TWO")

        DICOMAnonymizer().anonymize_directory(src, dest)
        result1 = pydicom.dcmread(str(dest / "p1.dcm"))
        result2 = pydicom.dcmread(str(dest / "p2.dcm"))
        assert str(result1.PatientID) != str(result2.PatientID)

    def test_patient_id_prefix_is_applied(self, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "f.dcm")

        DICOMAnonymizer().anonymize_directory(src, dest, patient_id_prefix="STUDY")
        result = pydicom.dcmread(str(dest / "f.dcm"))
        assert str(result.PatientID).startswith("STUDY")

    def test_empty_directory_returns_zero_stats(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        stats = DICOMAnonymizer().anonymize_directory(src, tmp_path / "dest")
        assert stats == {"processed": 0, "skipped": 0, "failed": 0}


# ---------------------------------------------------------------------------
# 4. Flask API endpoints
# ---------------------------------------------------------------------------

class TestAnonymizationAPI:

    def test_file_endpoint_success(self, client, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm")
        dest = tmp_path / "anon.dcm"
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"src": str(src), "dest": str(dest)}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["dest"] == str(dest)

    def test_file_endpoint_missing_src(self, client, tmp_path):
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"dest": str(tmp_path / "out.dcm")}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_file_endpoint_missing_dest(self, client, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm")
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"src": str(src)}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_file_endpoint_invalid_dicom_returns_500(self, client, tmp_path):
        bad = tmp_path / "bad.dcm"
        bad.write_bytes(b"not dicom")
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"src": str(bad), "dest": str(tmp_path / "out.dcm")}),
            content_type="application/json",
        )
        assert resp.status_code == 500

    def test_file_endpoint_with_patient_id(self, client, tmp_path):
        src, _ = _make_dicom_file(tmp_path, "input.dcm")
        dest = tmp_path / "anon.dcm"
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"src": str(src), "dest": str(dest), "patient_id": "APITEST"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        result = pydicom.dcmread(str(dest))
        assert str(result.PatientID) == "APITEST"

    def test_file_endpoint_no_body_returns_400(self, client):
        resp = client.post("/anonymization/file", content_type="application/json")
        assert resp.status_code == 400

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

    def test_directory_endpoint_custom_prefix(self, client, tmp_path):
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        src.mkdir()
        _make_dicom_file(src, "f.dcm")

        resp = client.post(
            "/anonymization/directory",
            data=json.dumps({
                "src": str(src),
                "dest": str(dest),
                "patient_id_prefix": "SITE",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        result = pydicom.dcmread(str(dest / "f.dcm"))
        assert str(result.PatientID).startswith("SITE")

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
        """Verify that the path validation rejects relative paths."""
        resp = client.post(
            "/anonymization/file",
            data=json.dumps({"src": "relative/path.dcm", "dest": str(tmp_path / "out.dcm")}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Path must be absolute" in data["error"]
