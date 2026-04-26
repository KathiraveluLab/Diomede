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
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400

    if not isinstance(data, dict):
        return jsonify({"error": "JSON payload must be an object"}), 400

    if "src" not in data or "dest" not in data:
        return jsonify({"error": "'src' and 'dest' parameters are required"}), 400

    if not isinstance(data.get("src"), str) or not isinstance(data.get("dest"), str):
        return jsonify({"error": "'src' and 'dest' must be strings"}), 400

    try:
        src_dir = _validate_path(data["src"])
        # src is intentionally not restricted to STORAGE_PATH. It is treated as
        # read-only by this API and may legitimately reside outside the storage
        # sandbox (e.g. a scanner's network mount or an incoming data drop). Only
        # DICOM files discovered by Niffler are ever read, which significantly
        # limits the accessible scope. The sandbox constraint applies only to dest,
        # where anonymized output is written.
        dest_dir = _validate_path(data["dest"])
        _check_within_storage(dest_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        LOG.error("Unexpected error during path validation: %s", e, exc_info=True)
        return jsonify({"error": "An internal processing error occurred."}), 500

    anonymizer = DICOMAnonymizer()
    stats = anonymizer.anonymize_directory(src_dir, dest_dir)
    return jsonify({"status": "success", "stats": stats}), 200
