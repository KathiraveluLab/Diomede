import os
import sys
import json
import shutil
import pytest
 
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dicom-album-env', 'Scripts'))
 
from pydicom.data import get_testdata_file
from dicom_indexer import index_folder
from album_api import app

@pytest.fixture
def client(tmp_path):
    """
    Flask test client with a real indexed SQLite DB.
    Sets DICOM_DB_PATH so all endpoints use the temp DB.
    """
    # Build a real indexed DB using CT_small.dcm
    sample_dcm = get_testdata_file("CT_small.dcm")
    dicom_dir  = tmp_path / "dicoms"
    dicom_dir.mkdir()
    shutil.copy(sample_dcm, dicom_dir / "CT_small.dcm")
 
    db_path = str(tmp_path / "test.db")
    index_folder(str(dicom_dir), db_path)
 
    app.config["TESTING"] = True
    os.environ["DICOM_DB_PATH"] = db_path
 
    with app.test_client() as client:
        yield client, db_path
 
    # Cleanup env var
    os.environ.pop("DICOM_DB_PATH", None)
 
 
@pytest.fixture
def empty_client(tmp_path):
    """Flask test client with an empty (un-indexed) DB."""
    db_path = str(tmp_path / "empty.db")
    app.config["TESTING"] = True
    os.environ["DICOM_DB_PATH"] = db_path
 
    with app.test_client() as client:
        yield client, db_path
 
    os.environ.pop("DICOM_DB_PATH", None)
 
class TestIndexEndpoint:
 
    def test_index_valid_folder(self, client, tmp_path):
        """Should index a valid folder and return success."""
        c, db_path = client
        sample_dcm = get_testdata_file("CT_small.dcm")
        folder = str(tmp_path / "dicoms2")
        os.makedirs(folder)
        shutil.copy(sample_dcm, os.path.join(folder, "CT.dcm"))
 
        resp = c.post("/index", json={"folder": folder, "db_path": db_path})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
 
    def test_index_missing_folder_param(self, client):
        """Should return 400 if folder is missing."""
        c, _ = client
        resp = c.post("/index", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()
 
    def test_index_invalid_folder_path(self, client):
        """Should return 400 if folder path does not exist."""
        c, db_path = client
        resp = c.post("/index", json={"folder": "/nonexistent/path", "db_path": db_path})
        assert resp.status_code == 400
 
    def test_index_no_body(self, client):
        """Should return 400 if no JSON body sent."""
        c, _ = client
        resp = c.post("/index")
        assert resp.status_code == 400
 
class TestCreateAlbumEndpoint:
 
    def test_create_album_success(self, client):
        """Should create album and return 201 with album details."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":    "My CT Album",
            "query":   "Modality == 'CT'",
            "db_path": db_path,
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["album"]["name"] == "My CT Album"
        assert data["album"]["file_count"] >= 1
 
    def test_create_album_stores_query(self, client):
        """query_used should be stored on the album."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":    "Query Test",
            "query":   "Modality == 'CT'",
            "db_path": db_path,
        })
        assert resp.status_code == 201
        assert resp.get_json()["album"]["query_used"] == "Modality == 'CT'"
 
    def test_create_album_with_description(self, client):
        """Optional description should be returned in response."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":        "Described Album",
            "query":       "Modality == 'CT'",
            "description": "Test description",
            "db_path":     db_path,
        })
        assert resp.status_code == 201
        assert resp.get_json()["album"]["description"] == "Test description"
 
    def test_create_album_no_match_returns_404(self, client):
        """Should return 404 if query matches no files."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":    "Empty",
            "query":   "Modality == 'MR'",
            "db_path": db_path,
        })
        assert resp.status_code == 404
 
    def test_create_album_invalid_query_returns_400(self, client):
        """Should return 400 for invalid query string."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":    "Bad",
            "query":   "InvalidField == 'CT'",
            "db_path": db_path,
        })
        assert resp.status_code == 400
 
    def test_create_album_missing_name_returns_400(self, client):
        """Should return 400 if name is missing."""
        c, db_path = client
        resp = c.post("/albums", json={"query": "Modality == 'CT'", "db_path": db_path})
        assert resp.status_code == 400
 
    def test_create_album_missing_query_returns_400(self, client):
        """Should return 400 if query is missing."""
        c, db_path = client
        resp = c.post("/albums", json={"name": "No Query", "db_path": db_path})
        assert resp.status_code == 400
 
    def test_create_album_returns_uuid(self, client):
        """Response must include a valid album_id UUID."""
        c, db_path = client
        resp = c.post("/albums", json={
            "name":    "UUID Test",
            "query":   "Modality == 'CT'",
            "db_path": db_path,
        })
        assert resp.status_code == 201
        album_id = resp.get_json()["album"]["album_id"]
        assert len(album_id) == 36   # UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
 
 
# ── GET /albums ───────────────────────────────────────────────────────────────
 
class TestListAlbumsEndpoint:
 
    def test_list_albums_returns_all(self, client):
        """Should return all created albums."""
        c, db_path = client
        c.post("/albums", json={"name": "A1", "query": "Modality == 'CT'", "db_path": db_path})
        c.post("/albums", json={"name": "A2", "query": "Modality == 'CT'", "db_path": db_path})
 
        resp = c.get(f"/albums?db_path={db_path}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] >= 2
 
    def test_list_albums_empty_returns_zero(self, empty_client):
        """Should return count 0 on empty DB."""
        c, db_path = empty_client
        resp = c.get(f"/albums?db_path={db_path}")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 0
 
    def test_list_albums_response_structure(self, client):
        """Each album in list should have required fields."""
        c, db_path = client
        c.post("/albums", json={"name": "Struct", "query": "Modality == 'CT'", "db_path": db_path})
 
        resp = c.get(f"/albums?db_path={db_path}")
        album = resp.get_json()["albums"][0]
        for key in ["album_id", "name", "query_used", "created_at", "file_count"]:
            assert key in album
 
 
# ── GET /albums/<album_id> ────────────────────────────────────────────────────
 
class TestGetAlbumEndpoint:
 
    def test_get_album_by_id(self, client):
        """Should return correct album by UUID."""
        c, db_path = client
        create_resp = c.post("/albums", json={
            "name":    "Fetchable",
            "query":   "Modality == 'CT'",
            "db_path": db_path,
        })
        album_id = create_resp.get_json()["album"]["album_id"]
 
        resp = c.get(f"/albums/{album_id}?db_path={db_path}")
        assert resp.status_code == 200
        assert resp.get_json()["album"]["name"] == "Fetchable"
 
    def test_get_album_includes_file_paths(self, client):
        """Response should include list of file paths."""
        c, db_path = client
        create_resp = c.post("/albums", json={
            "name":    "Files",
            "query":   "Modality == 'CT'",
            "db_path": db_path,
        })
        album_id = create_resp.get_json()["album"]["album_id"]
 
        resp = c.get(f"/albums/{album_id}?db_path={db_path}")
        data = resp.get_json()
        assert "files" in data["album"]
        assert len(data["album"]["files"]) >= 1
 
    def test_get_album_wrong_id_returns_404(self, client):
        """Should return 404 for non-existent album ID."""
        c, db_path = client
        resp = c.get(f"/albums/00000000-0000-0000-0000-000000000000?db_path={db_path}")
        assert resp.status_code == 404
 