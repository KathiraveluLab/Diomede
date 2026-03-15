import logging
import os
import pydicom

LOG = logging.getLogger(__name__)


def safe_load_dicom_file(file_path: str):
    """Safely load a DICOM file.

    This helper encapsulates pydicom.dcmread and catches typical file errors from
    corrupted/malformed/non-DICOM inputs.

    Returns:
        pydicom.dataset.Dataset if valid DICOM,
        None if invalid or unreadable.

    Handled exceptions:
        - pydicom.errors.InvalidDicomError
        - pydicom.errors.PydicomError
        - EOFError
        - ValueError
        - OSError

    The function returns None for invalid files so pipeline consumers can continue
    processing the remaining files safely.
    """
    try:
        dataset = pydicom.dcmread(file_path)
        # store path for downstream metadata extraction if dataset lacks filename attr
        try:
            dataset.filename = file_path
        except Exception:
            pass
        return dataset
    except (pydicom.errors.InvalidDicomError, pydicom.errors.PydicomError, EOFError, ValueError, OSError) as ex:
        LOG.warning(
            "Skipping invalid or corrupted DICOM file: %s (%s)",
            file_path,
            ex,
        )
        return None


def load_dicom_files(directory):
    """Load all DICOM files from a directory."""
    dicom_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            dicom_file = safe_load_dicom_file(file_path)
            if dicom_file is not None:
                dicom_files.append(dicom_file)
    return dicom_files