import logging
from os import PathLike
from typing import Union

import pydicom

LOG = logging.getLogger(__name__)

_METADATA_KEYS = ('PatientID', 'StudyDate', 'Modality', 'SeriesInstanceUID')


def safe_load_dicom_file(file_path: Union[str, PathLike]):
    """Safely load a DICOM file from disk.

    Returns the pydicom Dataset for valid files or None when invalid/malformed.
    """
    try:
        return pydicom.dcmread(file_path, stop_before_pixels=True)
    except (pydicom.errors.InvalidDicomError, EOFError, ValueError, OSError) as ex:
        LOG.warning("Skipping invalid or corrupted DICOM file: %s (%s)", file_path, ex)
        return None


def normalize_metadata(dataset_or_dict):
    """Normalize DICOM metadata for Dataset/dict inputs."""
    if dataset_or_dict is None:
        return {key: None for key in _METADATA_KEYS}

    getter = dataset_or_dict.get if hasattr(dataset_or_dict, 'get') else None
    if getter is None:
        return {key: None for key in _METADATA_KEYS}

    return {key: getter(key, None) for key in _METADATA_KEYS}


def extract_basic_metadata(file_path: Union[str, PathLike]):
    return normalize_metadata(safe_load_dicom_file(file_path))

