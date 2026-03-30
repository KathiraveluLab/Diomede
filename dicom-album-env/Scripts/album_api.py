import os
import sys
 
from flask import Flask, request, jsonify
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
 
from dicom_indexer import index_folder
from album_manager import create_album, list_albums, get_album
 
app = Flask(__name__)
DEFAULT_DB = os.environ.get("DICOM_DB_PATH", "dicom_index.db")
 
@app.route("/index", methods=["POST"])
def index():
    """
    Scan a local folder and index all DICOM files into SQLite.
    Request body (JSON):
        {
            "folder": "/path/to/dicom/files",   # required
            "db_path": "dicom_index.db"          # optional, uses default if omitted
        }
 
    Response:
        200 — { "status": "success", "message": "..." }
        400 — { "error": "..." }
        500 — { "error": "..." }
    """
    data = request.get_json(silent=True)
 
    if not data or "folder" not in data:
        return jsonify({"error": "'folder' is required in request body"}), 400
 
    folder  = data["folder"]
    db_path = data.get("db_path", DEFAULT_DB)
 
    if not os.path.isdir(folder):
        return jsonify({"error": f"'{folder}' is not a valid directory"}), 400
 
    try:
        index_folder(folder, db_path)
        return jsonify({
            "status":  "success",
            "message": f"Indexed DICOM files from '{folder}' into '{db_path}'",
        }), 200
 
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
@app.route("/albums", methods=["POST"])
def create():
    """
    Create a named album by querying the DICOM index.
 
    Request body (JSON):
        {
            "name":        "My CT Album",               # required
            "query":       "Modality == 'CT'",           # required
            "description": "All CT scans from 2023",    # optional
            "db_path":     "dicom_index.db"             # optional
        }
 
    Query syntax (from query_metadata.py):
        "Modality == 'CT'"
        "Modality == 'CT' and StudyDate > '20200101'"
        "Modality in ['CT', 'MR']"
 
    Response:
        201 — { "status": "success", "album": { ... } }
        400 — { "error": "..." }
        404 — { "error": "No files matched" }
        500 — { "error": "..." }
    """
    data = request.get_json(silent=True)
 
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
 
    if "name" not in data:
        return jsonify({"error": "'name' is required"}), 400
 
    if "query" not in data:
        return jsonify({"error": "'query' is required"}), 400
 
    name        = data["name"]
    query       = data["query"]
    description = data.get("description")
    db_path     = data.get("db_path", DEFAULT_DB)
 
    try:
        album = create_album(
            name=name,
            db_path=db_path,
            query=query,
            description=description,
        )
 
        if album is None:
            return jsonify({
                "error": f"No DICOM files matched query: {query}"
            }), 404
 
        return jsonify({
            "status": "success",
            "album": {
                "album_id":    album.album_id,
                "name":        album.name,
                "description": album.description,
                "query_used":  album.query_used,
                "created_at":  album.created_at.isoformat(),
                "file_count":  len(album.files),
            },
        }), 201
 
    except ValueError as exc:
        # Invalid query string from query_metadata
        return jsonify({"error": f"Invalid query: {exc}"}), 400
 
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
 
 
# ── GET /albums ───────────────────────────────────────────────────────────────
@app.route("/albums", methods=["GET"])
def list_all():
    """
    List all albums stored in the database.
 
    Query params:
        db_path — optional, path to SQLite DB
 
    Response:
        200 — { "status": "success", "count": N, "albums": [ ... ] }
        500 — { "error": "..." }
    """
    db_path = request.args.get("db_path", DEFAULT_DB)
 
    try:
        albums = list_albums(db_path)
        return jsonify({
            "status": "success",
            "count":  len(albums),
            "albums": [
                {
                    "album_id":   a.album_id,
                    "name":       a.name,
                    "description": a.description,
                    "query_used": a.query_used,
                    "created_at": a.created_at.isoformat(),
                    "file_count": len(a.files),
                }
                for a in albums
            ],
        }), 200
 
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
 
 
# ── GET /albums/<album_id> ────────────────────────────────────────────────────
@app.route("/albums/<album_id>", methods=["GET"])
def get_one(album_id):
    """
    Get a single album by its UUID, including all file paths it contains.
 
    Response:
        200 — { "status": "success", "album": { ..., "files": [...] } }
        404 — { "error": "Album not found" }
        500 — { "error": "..." }
    """
    db_path = request.args.get("db_path", DEFAULT_DB)
 
    try:
        album = get_album(album_id, db_path)
 
        if album is None:
            return jsonify({"error": f"Album '{album_id}' not found"}), 404
 
        return jsonify({
            "status": "success",
            "album": {
                "album_id":    album.album_id,
                "name":        album.name,
                "description": album.description,
                "query_used":  album.query_used,
                "created_at":  album.created_at.isoformat(),
                "file_count":  len(album.files),
                "files":       [f.file_path for f in album.files],
            },
        }), 200
 
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
 
 
if __name__ == "__main__":
    app.run(debug=True, port=5000)
 