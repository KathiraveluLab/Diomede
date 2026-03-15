import logging
import pydicom

LOG = logging.getLogger(__name__)


def safe_load_dicom_file(file_path: str):
    """Safely load a DICOM file from disk.

    Returns the pydicom Dataset for valid files or None when invalid/malformed.

    Reasoning:
      - Centralizes DICOM error handling once (single source of truth).
      - Avoids duplication in multiple modules.
      - Allows scanning loops to continue safely on bad files.

    Exceptions handled:
      - pydicom.errors.InvalidDicomError
      - pydicom.errors.PydicomError
      - EOFError
      - ValueError
      - OSError
    """
    try:
        dataset = pydicom.dcmread(file_path)
    except (pydicom.errors.InvalidDicomError,
            pydicom.errors.PydicomError,
            EOFError,
            ValueError,
            OSError) as ex:
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None

    # attempt to annotate dataset with the source path, but do not fail if not allowed
    try:
        dataset.filename = file_path
    except (AttributeError, TypeError) as ex:
        LOG.debug("Could not set dataset.filename for %s: %s", file_path, ex)

    return dataset
