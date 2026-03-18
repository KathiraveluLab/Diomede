import logging
from os import PathLike
from typing import Union

import pydicom

LOG = logging.getLogger(__name__)


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
            pydicom.errors.PydicomError,
            EOFError,
            ValueError,
            OSError) as ex:
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None

    return dataset
