import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dicom-album-env', 'Scripts'))
import shutil
import pytest
 
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pydicom.data import get_testdata_file
 
from dicom_indexer import index_folder, DICOMIndex, Base
from album_manager import (
    Album, create_album, list_albums, get_album, load_index_as_dataframe
)

 
@pytest.fixture
def sample_dcm():
    """Real CT DICOM file bundled with pydicom — no external files needed."""
    return get_testdata_file("CT_small.dcm")
 
 
@pytest.fixture
def indexed_db(tmp_path, sample_dcm):
    """
    Builds a fully indexed SQLite DB with one CT DICOM file.
    This is the same setup as test_dicomIndexer.py — consistent style.
    """
    dicom_dir = tmp_path / "dicoms"
    dicom_dir.mkdir()
    shutil.copy(sample_dcm, dicom_dir / "CT_small.dcm")
    db_path = str(tmp_path / "test.db")
    index_folder(str(dicom_dir), db_path)
    return db_path

class TestLoadIndexAsDataframe:
 
    def test_returns_dataframe(self, indexed_db):
        """Should return a pandas DataFrame, not a list or None."""
        engine  = create_engine(f"sqlite:///{indexed_db}")
        session = sessionmaker(bind=engine)()
        df = load_index_as_dataframe(session)
        session.close()
        import pandas as pd
        assert isinstance(df, pd.DataFrame)
 
    def test_dataframe_has_required_columns(self, indexed_db):
        """
        Columns must match query_metadata.py's ALLOWED_FIELDS whitelist.
        If these are missing, the query engine will reject every query.
        """
        engine  = create_engine(f"sqlite:///{indexed_db}")
        session = sessionmaker(bind=engine)()
        df = load_index_as_dataframe(session)
        session.close()
        for col in ["PatientID", "Modality", "StudyDate", "SeriesDescription", "FilePath"]:
            assert col in df.columns, f"Missing column: {col}"
 
    def test_studydate_is_yyyymmdd_string(self, indexed_db):
        """
        query_metadata.py expects StudyDate as YYYYMMDD string.
        dicom_indexer stores it as datetime — the bridge must convert it.
        """
        engine  = create_engine(f"sqlite:///{indexed_db}")
        session = sessionmaker(bind=engine)()
        df = load_index_as_dataframe(session)
        session.close()
        date_val = df["StudyDate"].iloc[0]
        assert isinstance(date_val, str)
        assert len(date_val) == 8
        assert date_val.isdigit()
 
    def test_empty_db_returns_empty_dataframe(self, tmp_path):
        """Empty index should return empty DataFrame, not crash."""
        db_path = str(tmp_path / "empty.db")
        engine  = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        df = load_index_as_dataframe(session)
        session.close()
        assert df.empty

class TestCreateAlbum:
 
    def test_creates_album_for_matching_query(self, indexed_db):
        """CT_small.dcm is a CT file — querying for CT should find it."""
        album = create_album("My CT Album", indexed_db, query="Modality == 'CT'")
        assert album is not None
        assert album.name == "My CT Album"
 
    def test_album_contains_matched_files(self, indexed_db):
        """Album must actually contain the matched DICOM files."""
        album = create_album("Files Test", indexed_db, query="Modality == 'CT'")
        assert album is not None
        assert len(album.files) >= 1
 
    def test_album_stores_query_used(self, indexed_db):
        """
        The query string is stored on the album so you can see
        how an album was built later — important for reproducibility.
        """
        query = "Modality == 'CT'"
        album = create_album("Query Test", indexed_db, query=query)
        assert album is not None
        assert album.query_used == query
 
    def test_album_has_unique_uuid(self, indexed_db):
        """Every album must get a different UUID — no collisions."""
        album1 = create_album("Album A", indexed_db, query="Modality == 'CT'")
        album2 = create_album("Album B", indexed_db, query="Modality == 'CT'")
        assert album1.album_id != album2.album_id
 
    def test_album_has_created_at_timestamp(self, indexed_db):
        """created_at must be set automatically on creation."""
        album = create_album("Timestamped", indexed_db, query="Modality == 'CT'")
        assert album is not None
        assert isinstance(album.created_at, datetime)
 
    def test_description_is_stored(self, indexed_db):
        """Optional description should be saved correctly."""
        album = create_album(
            "Described", indexed_db,
            query="Modality == 'CT'",
            description="My test album"
        )
        assert album is not None
        assert album.description == "My test album"
 
    def test_no_match_returns_none(self, indexed_db):
        """
        If query matches nothing, return None instead of creating
        an empty album — empty albums are useless.
        """
        album = create_album("Empty", indexed_db, query="Modality == 'MR'")
        assert album is None
 
    def test_empty_index_returns_none(self, tmp_path):
        """If the index has no files at all, return None gracefully."""
        db_path = str(tmp_path / "empty.db")
        engine  = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        album = create_album("Nothing", db_path, query="Modality == 'CT'")
        assert album is None
 
    def test_invalid_query_raises_value_error(self, indexed_db):
        """
        query_metadata raises ValueError for bad queries.
        create_album must not swallow it — let it bubble up
        so the caller knows the query was wrong.
        """
        with pytest.raises(ValueError):
            create_album("Bad Query", indexed_db, query="InvalidField == 'CT'")
 
    def test_date_range_query_works(self, indexed_db):
        """
        Test that the StudyDate YYYYMMDD conversion in load_index_as_dataframe
        actually allows date range queries to work end-to-end.
        """
        album = create_album(
            "Date Range",
            indexed_db,
            query="Modality == 'CT' and StudyDate > '20000101'"
        )
        assert album is not None
 
    def test_two_albums_share_same_files(self, indexed_db):
        """
        The same DICOM file can belong to multiple albums.
        This tests the many-to-many relationship works correctly.
        """
        album1 = create_album("First",  indexed_db, query="Modality == 'CT'")
        album2 = create_album("Second", indexed_db, query="Modality == 'CT'")
        assert album1 is not None
        assert album2 is not None
        # Both albums reference the same underlying file
        assert album1.files[0].file_path == album2.files[0].file_path
 
 
# ── Tests: list_albums and get_album ─────────────────────────────────────────
 
class TestListAndGetAlbum:
 
    def test_list_albums_returns_all(self, indexed_db):
        """list_albums must return every album created."""
        create_album("Album 1", indexed_db, query="Modality == 'CT'")
        create_album("Album 2", indexed_db, query="Modality == 'CT'")
        albums = list_albums(indexed_db)
        assert len(albums) >= 2
 
    def test_list_albums_empty_db(self, tmp_path):
        """list_albums on empty DB should return empty list, not crash."""
        db_path = str(tmp_path / "empty.db")
        engine  = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        assert list_albums(db_path) == []
 
    def test_get_album_by_id(self, indexed_db):
        """get_album must return the correct album by UUID."""
        album   = create_album("Fetchable", indexed_db, query="Modality == 'CT'")
        fetched = get_album(album.album_id, indexed_db)
        assert fetched is not None
        assert fetched.name == "Fetchable"
 
    def test_get_album_wrong_id_returns_none(self, indexed_db):
        """get_album with a non-existent ID must return None, not crash."""
        result = get_album("00000000-0000-0000-0000-000000000000", indexed_db)
        assert result is None
 