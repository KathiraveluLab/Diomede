import logging
from os import PathLike
from typing import Union, Iterable

import pydicom

LOG = logging.getLogger(__name__)

# System-level defaults — these are DICOM hierarchy identifiers required
# for series-level album grouping regardless of research context.
# Researchers may extend or override these via function parameters.
_DEFAULT_CRITICAL_TAGS = ("SeriesInstanceUID", "StudyInstanceUID", "PatientID")

# Tags that improve query and filtering quality but do not block album creation.
# Researchers may override these based on their workflow requirements.
_DEFAULT_OPTIONAL_TAGS = ("Modality", "StudyDate", "SeriesDescription")


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
            pydicom.errors.BytesLengthException,
            EOFError,
            ValueError,
            OSError) as ex:
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None
    return dataset


def extract_basic_metadata(
    file_path: Union[str, PathLike],
    tags: Iterable[str] = _DEFAULT_CRITICAL_TAGS + _DEFAULT_OPTIONAL_TAGS,
):
    """Extract specified DICOM fields from a file.
    Args:
        file_path: Path to the DICOM file.
        tags: DICOM tag names to extract. Defaults to combined critical
              and optional tags. Pass custom tags to match your workflow.
    Returns:
        A dict with extracted tag values, or None for missing tags.
    """
    dataset = safe_load_dicom_file(file_path)
    if dataset is None:
        return dict.fromkeys(tags)
    return {key: dataset.get(key) for key in tags}


def validate_dicom_for_album(
    metadata: dict,
    critical_tags: Iterable[str] = _DEFAULT_CRITICAL_TAGS,
    optional_tags: Iterable[str] = _DEFAULT_OPTIONAL_TAGS,
) -> dict:
    """Validate extracted DICOM metadata for album-workflow completeness.

    Tag criticality is user-configurable — different research workflows have
    different requirements. For example, PatientID is critical for cohort
    studies but may be anonymized or excluded in de-identified datasets.
    Researchers pass their own critical_tags and optional_tags to match
    their specific workflow needs.

    SeriesInstanceUID and StudyInstanceUID are included in the default
    critical set as system-level requirements — they are the grouping keys
    for DICOM series-level album organisation and cannot be substituted.

    Args:
        metadata:      dict returned by extract_basic_metadata, or any dict
                       containing DICOM tag name -> value pairs.
        critical_tags: tags whose absence marks the file as invalid for album
                       creation. Defaults to _DEFAULT_CRITICAL_TAGS but should
                       be supplied by the caller based on research context.
        optional_tags: tags whose absence marks the file as a warning — album
                       can still be created but queries may be degraded.
                       Defaults to _DEFAULT_OPTIONAL_TAGS.

    Returns:
        A structured result dict:
          status           - "valid" | "invalid" | "warning"
          missing_critical - list of critical tag names that are absent/None
          missing_optional - list of optional tag names that are absent/None

    Usage:
        # Default validation
        result = validate_dicom_for_album(metadata)

        # Research-specific validation (e.g. anonymized dataset)
        result = validate_dicom_for_album(
            metadata,
            critical_tags=("SeriesInstanceUID", "StudyInstanceUID"),
            optional_tags=("Modality", "StudyDate"),
        )

        if result["status"] == "invalid":
            # skip — file cannot be grouped into an album
    """
    missing_critical = [
        tag for tag in critical_tags
        if not metadata.get(tag)
    ]
    missing_optional = [
        tag for tag in optional_tags
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