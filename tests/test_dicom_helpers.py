import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'Diomedex', 'utils'))
from dicom_helpers import validate_dicom_for_album, extract_basic_metadata

import pytest

class TestValidateDicomForAlbum:
    """Tests for album-workflow metadata validation.

    Albums are organised at SeriesInstanceUID level — these tests confirm
    that files missing critical grouping tags are rejected before they can
    silently corrupt album contents or query results.
    """

    def test_all_fields_present_returns_valid(self):
        metadata = {
            "SeriesInstanceUID": "1.2.3.4.5",
            "StudyInstanceUID": "1.2.3.4.6",
            "PatientID": "P001",
            "Modality": "CT",
            "StudyDate": "20200101",
        }
        result = validate_dicom_for_album(metadata)
        assert result["status"] == "valid"
        assert result["missing_critical"] == []
        assert result["missing_optional"] == []

    def test_missing_series_uid_returns_invalid(self):
        # SeriesInstanceUID is the atomic unit of album grouping —
        # a file without it cannot be placed into any album
        metadata = {
            "SeriesInstanceUID": None,
            "StudyInstanceUID": "1.2.3.4.6",
            "PatientID": "P001",
            "Modality": "CT",
            "StudyDate": "20200101",
        }
        result = validate_dicom_for_album(metadata)
        assert result["status"] == "invalid"
        assert "SeriesInstanceUID" in result["missing_critical"]

    def test_missing_study_uid_returns_invalid(self):
        metadata = {
            "SeriesInstanceUID": "1.2.3.4.5",
            "StudyInstanceUID": None,
            "PatientID": "P001",
            "Modality": "CT",
            "StudyDate": "20200101",
        }
        result = validate_dicom_for_album(metadata)
        assert result["status"] == "invalid"
        assert "StudyInstanceUID" in result["missing_critical"]

    def test_missing_optional_field_returns_warning(self):
        # Missing Modality degrades filtering but album can still be created
        metadata = {
            "SeriesInstanceUID": "1.2.3.4.5",
            "StudyInstanceUID": "1.2.3.4.6",
            "PatientID": "P001",
            "Modality": None,
            "StudyDate": "20200101",
        }
        result = validate_dicom_for_album(metadata)
        assert result["status"] == "warning"
        assert "Modality" in result["missing_optional"]

    def test_corrupted_file_extract_returns_all_none(self, tmp_path):
        # A corrupted file should produce all-None metadata —
        # validate_dicom_for_album must reject it cleanly
        fake_file = tmp_path / "corrupt.dcm"
        fake_file.write_bytes(b"this is not a dicom file")
        metadata = extract_basic_metadata(str(fake_file))
        result = validate_dicom_for_album(metadata)
        assert result["status"] == "invalid"
        assert "SeriesInstanceUID" in result["missing_critical"]
        assert "StudyInstanceUID" in result["missing_critical"]