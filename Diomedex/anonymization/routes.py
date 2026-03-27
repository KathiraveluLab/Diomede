import logging
from pathlib import Path

from flask import Blueprint, jsonify, request, current_app

from .core import DICOMAnonymizer

LOG = logging.getLogger(__name__)

anonymization_bp = Blueprint("anonymization", __name__, url_prefix="/anonymization")


def _validate_path(raw: str) -> Path:
    """Resolve a path string and require it to be absolute.

    Raises:
        ValueError: if the supplied path is not absolute.
    """
    p = Path(raw)
    if not p.is_absolute():
        raise ValueError(f"Path must be absolute, got: {raw!r}")
    return p.resolve()


def _check_within_storage(path: Path) -> None:
    """Ensure *path* is contained within the configured STORAGE_PATH.

    Raises:
        ValueError: If *path* is not a descendant of STORAGE_PATH.
    """
    storage_base = Path(current_app.config["STORAGE_PATH"]).resolve()
    try:
        path.relative_to(storage_base)
    except ValueError:
        raise ValueError("Path must be within configured storage area")


@anonymization_bp.route("/directory", methods=["POST"])
def anonymize_directory():
    """Recursively anonymize all DICOM files under a directory.

    Request body (JSON):
        src (str): Absolute path to the root source directory.
        dest (str): Absolute path to the root output directory.
    Returns:
        200: ``{"status": "success", "stats": {"processed": N, "skipped": N, "failed": N}}``
        400: ``{"error": "..."}`` when ``src`` or ``dest`` is missing.
    """
    data = request.get_json()
    if not data or "src" not in data or "dest" not in data:
        return jsonify({"error": "'src' and 'dest' parameters are required"}), 400

    try:
        src_dir = _validate_path(data["src"])
        dest_dir = _validate_path(data["dest"])
        _check_within_storage(dest_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Internal processing error: {str(e)}"}), 500

    anonymizer = DICOMAnonymizer()
    stats = anonymizer.anonymize_directory(src_dir, dest_dir)
    return jsonify({"status": "success", "stats": stats}), 200
