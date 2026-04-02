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


def test_dicom_metadata_helpers_smoke(tmp_path):
    """One small test covering valid, missing fields, and invalid file safety."""
    def _write(path, **fields):
        meta = pydicom.Dataset()
        meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
        ds = pydicom.dataset.FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
        for key, value in fields.items():
            setattr(ds, key, value)
        pydicom.dcmwrite(path, ds)

    full_file = tmp_path / 'full.dcm'
    _write(full_file, PatientID='PID-1', StudyDate='20260101', Modality='CT', SeriesInstanceUID='1.2.3.4')

    missing_file = tmp_path / 'missing.dcm'
    _write(missing_file)
    invalid_file = tmp_path / 'invalid.dcm'
    invalid_file.write_bytes(b'not a dicom')

    assert extract_basic_metadata(full_file) == {
        'PatientID': 'PID-1',
        'StudyDate': '20260101',
        'Modality': 'CT',
        'SeriesInstanceUID': '1.2.3.4',
    }
    assert extract_basic_metadata(missing_file) == {key: None for key in _METADATA_KEYS}
    assert extract_basic_metadata(invalid_file) == {key: None for key in _METADATA_KEYS}
