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


@anonymization_bp.route("/file", methods=["POST"])
def anonymize_file():
    """Anonymize a single DICOM file.

    Request body (JSON):
        src (str): Absolute path to the source DICOM file.
        dest (str): Absolute path for the anonymized output file.
            Parent directories are created automatically.
        patient_id (str, optional): Replacement patient identifier written to
            ``PatientName`` and ``PatientID``.  Defaults to empty string when
            omitted.

    Returns:
        200: ``{"status": "success", "dest": "<dest_path>"}``
        400: ``{"error": "..."}`` when ``src`` or ``dest`` is missing.
        500: ``{"error": "..."}`` when the source file is not valid DICOM or
             the write fails.
    """
    data = request.get_json()
    if not data or "src" not in data or "dest" not in data:
        return jsonify({"error": "'src' and 'dest' parameters are required"}), 400

    try:
        src_path = _validate_path(data["src"])
        dest_path = _validate_path(data["dest"])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        LOG.error("Internal processing error in anonymize_file: %s", e, exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

    anonymizer = DICOMAnonymizer()
    success = anonymizer.anonymize_file(
        src_path,
        dest_path,
        patient_id=data.get("patient_id"),
    )

    if success:
        return jsonify({"status": "success", "dest": str(dest_path)}), 200
    return (
        jsonify({"error": "Failed to anonymize file — verify that src is a valid DICOM file"}),
        500,
    )


@anonymization_bp.route("/directory", methods=["POST"])
def anonymize_directory():
    """Recursively anonymize all DICOM files under a directory.

    Request body (JSON):
        src (str): Absolute path to the root source directory.
        dest (str): Absolute path to the root output directory.
        patient_id_prefix (str, optional): Prefix for generated patient
            identifiers (default: ``"ANON"``).

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
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        LOG.error("Internal processing error in anonymize_directory: %s", e, exc_info=True)
        return jsonify({"error": "An internal server error occurred"}), 500

    anonymizer = DICOMAnonymizer()
    stats = anonymizer.anonymize_directory(
        src_dir,
        dest_dir,
        patient_id_prefix=data.get("patient_id_prefix", "ANON"),
    )
    return jsonify({"status": "success", "stats": stats}), 200
