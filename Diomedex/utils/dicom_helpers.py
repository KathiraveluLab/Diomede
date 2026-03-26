import logging
from os import PathLike
from typing import Union

import pydicom

LOG = logging.getLogger(__name__)

# Tags required for album creation workflow
# SeriesInstanceUID and StudyInstanceUID are critical — without them
# a file cannot be grouped into any album
_CRITICAL_TAGS = ("SeriesInstanceUID", "StudyInstanceUID")

# Important but not blocking — albums can be created without these,
# but queries and filtering will be degraded
_IMPORTANT_TAGS = ("PatientID", "Modality", "StudyDate")


def safe_load_dicom_file(file_path: Union[str, PathLike]):
    """Safely load a DICOM file from disk.
    Returns the pydicom Dataset for valid files or None when invalid/malformed.
    Reasoning:
      - Centralizes DICOM error handling once (single source of truth).
      - Avoids duplication in multiple modules.
      - Allows scanning loops to continue safely on bad files.
    """
    try:
        dataset = pydicom.dcmread(file_path)
    except (pydicom.errors.InvalidDicomError,
            EOFError,
            ValueError,
            OSError) as ex:
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None
    return dataset


def extract_basic_metadata(file_path: Union[str, PathLike]):
    """Extract key DICOM fields needed for album creation.
    Returns a dict with None defaults for any missing field.
    """
    keys = ("PatientID", "StudyDate", "Modality",
            "SeriesInstanceUID", "StudyInstanceUID")
    dataset = safe_load_dicom_file(file_path)
    if dataset is None:
        return dict.fromkeys(keys)
    return {key: dataset.get(key) for key in keys}


def validate_dicom_for_album(metadata: dict) -> dict:
    """Validate extracted DICOM metadata for album-workflow completeness.

    Albums are organised at the SeriesInstanceUID level — series are the
    atomic unit of a DICOM album.  A file that is missing a critical tag
    cannot be placed into any album and must be rejected early, before it
    silently corrupts query results or produces orphaned album entries.

    Args:
        metadata: dict returned by extract_basic_metadata (or any dict
                  containing DICOM tag name → value pairs).

    Returns:
        A structured result dict with three keys:
          status           - "valid" | "invalid" | "warning"
          missing_critical - list of critical tag names that are absent/None
          missing_optional - list of important tag names that are absent/None

    Usage:
        metadata = extract_basic_metadata(file_path)
        result = validate_dicom_for_album(metadata)
        if result["status"] == "invalid":
            # skip this file — cannot be grouped into an album
    """
    missing_critical = [
        tag for tag in _CRITICAL_TAGS
        if not metadata.get(tag)
    ]
    missing_optional = [
        tag for tag in _IMPORTANT_TAGS
        if not metadata.get(tag)
    ]

    if missing_critical:
        status = "invalid"
    elif missing_optional:
        status = "warning"
    else:
        status = "valid"

    return {
        "status": status,
        "missing_critical": missing_critical,
        "missing_optional": missing_optional,
    }