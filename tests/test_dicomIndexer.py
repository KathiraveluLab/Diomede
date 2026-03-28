import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dicom-album-env", "Scripts"))

from dicom_indexer import extract_metadata, parse_study_date, index_folder, DICOMIndex
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pydicom.data import get_testdata_file

@pytest.fixture
def sample_dcm():
    return get_testdata_file("CT_small.dcm")

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_index.db")

@pytest.fixture
def tmp_dicom_dir(tmp_path, sample_dcm):
    import shutil
    shutil.copy(sample_dcm, tmp_path / "CT_small.dcm")
    (tmp_path / "not_dicom.txt").write_text("this is not a dicom file")
    return str(tmp_path)

class TestParseStudyDate:
    def test_valid_date_string(self):
        result = parse_study_date("20200115")
        assert result is not None
        assert result.year == 2020
    def test_none_input(self):
        assert parse_study_date(None) is None
    def test_empty_string(self):
        assert parse_study_date("") is None
    def test_invalid_format(self):
        assert parse_study_date("not-a-date") is None

class TestExtractMetadata:
    def test_returns_dict_for_valid_dicom(self, sample_dcm):
        meta = extract_metadata(sample_dcm)
        assert meta is not None
        assert isinstance(meta, dict)
    def test_returns_none_for_non_dicom(self, tmp_path):
        fake = tmp_path / "fake.dcm"
        fake.write_text("this is not dicom content")
        assert extract_metadata(str(fake)) is None
    def test_contains_required_keys(self, sample_dcm):
        meta = extract_metadata(sample_dcm)
        for key in ["file_path", "patient_id", "study_uid", "modality", "study_date"]:
            assert key in meta
    def test_file_path_is_absolute(self, sample_dcm):
        meta = extract_metadata(sample_dcm)
        assert os.path.isabs(meta["file_path"])
    def test_modality_is_ct(self, sample_dcm):
        meta = extract_metadata(sample_dcm)
        assert meta["modality"] == "CT"

class TestIndexFolder:
    def test_indexes_dicom_file(self, tmp_dicom_dir, tmp_db):
        index_folder(tmp_dicom_dir, tmp_db)
        engine = create_engine(f"sqlite:///{tmp_db}")
        session = sessionmaker(bind=engine)()
        count = session.query(DICOMIndex).count()
        session.close()
        assert count == 1
    def test_skips_non_dicom_files(self, tmp_dicom_dir, tmp_db):
        index_folder(tmp_dicom_dir, tmp_db)
        engine = create_engine(f"sqlite:///{tmp_db}")
        session = sessionmaker(bind=engine)()
        count = session.query(DICOMIndex).count()
        session.close()
        assert count == 1
    def test_no_duplicates_on_reindex(self, tmp_dicom_dir, tmp_db):
        index_folder(tmp_dicom_dir, tmp_db)
        index_folder(tmp_dicom_dir, tmp_db)
        engine = create_engine(f"sqlite:///{tmp_db}")
        session = sessionmaker(bind=engine)()
        count = session.query(DICOMIndex).count()
        session.close()
        assert count == 1
    def test_modality_stored_correctly(self, tmp_dicom_dir, tmp_db):
        index_folder(tmp_dicom_dir, tmp_db)
        engine = create_engine(f"sqlite:///{tmp_db}")
        session = sessionmaker(bind=engine)()
        record = session.query(DICOMIndex).first()
        session.close()
        assert record.modality == "CT"
