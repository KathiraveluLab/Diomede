import logging
import pickle
from os import PathLike
from pathlib import Path
from typing import Dict, Union

import pydicom
from pydicom.uid import generate_uid
from modules.dicom_anonymization.DicomAnonymizer2 import (
    dcm_anonymize as _niffler_dcm_anonymize,
    get_dcm_paths as _niffler_get_dcm_paths,
)

_REQUIRED_UID_TAGS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID")


def _ensure_required_tags(file_path: str) -> None:
    """Add any missing required UID tags to a DICOM file in-place.

    Niffler's ``dcm_anonymize`` unconditionally accesses ``StudyInstanceUID``,
    ``SeriesInstanceUID``, and ``SOPInstanceUID`` and will raise ``KeyError``
    on files that lack them (e.g. minimal or legacy files).
    This function silently patches missing tags with freshly generated UIDs so
    the file can pass through Niffler without error.
    """
    # Check headers first without loading large pixel data into memory
    ds = pydicom.dcmread(file_path, stop_before_pixels=True)
    patched = False
    for tag in _REQUIRED_UID_TAGS:
        if tag not in ds:
            patched = True
            break

    if patched:
        # Re-read fully only if we actually need to save changes
        ds = pydicom.dcmread(file_path)
        for tag in _REQUIRED_UID_TAGS:
            if tag not in ds:
                setattr(ds, tag, generate_uid())
        ds.save_as(file_path)


LOG = logging.getLogger(__name__)


class DICOMAnonymizer:
    """Batch DICOM anonymizer backed by Niffler.

    Delegates all PHI removal and ID generation to Niffler's ``dcm_anonymize``,
    which generates a cryptographically random 25-character alphanumeric
    PatientID per unique patient (via ``random.SystemRandom``) and remaps all
    study/series/instance UIDs consistently across a batch.
    """

    def anonymize_directory(
        self,
        src_dir: Union[str, PathLike],
        dest_dir: Union[str, PathLike],
    ) -> Dict[str, int]:
        """Recursively anonymize every DICOM file under *src_dir* using Niffler.

        Output is organised as
        ``dest/<PatientID>/<StudyUID>/<SeriesUID>/<SOPUID>.dcm``.

        Args:
            src_dir: Root directory containing source DICOM files.
            dest_dir: Root directory for anonymized output.

        Returns:
            A dict with integer counts for ``processed``, ``skipped``, and ``failed`` files.

        Raises:
            ValueError: If *src_dir* and *dest_dir* resolve to the same path.
        """
        src = Path(src_dir)
        dest = Path(dest_dir)

        if src.resolve() == dest.resolve():
            raise ValueError(
                "Source and destination directories cannot be the same to prevent data corruption."
            )

        dcm_files = _niffler_get_dcm_paths(str(src))
        if not dcm_files:
            return {"processed": 0, "skipped": 0, "failed": 0}

        for f in dcm_files:
            _ensure_required_tags(f)

        dest.mkdir(parents=True, exist_ok=True)
        _niffler_dcm_anonymize(dcm_files, str(dest))

        skipped_pkl = dest / "skipped.pkl"
        failed = (
            len(pickle.load(open(str(skipped_pkl), "rb")))
            if skipped_pkl.exists()
            else 0
        )
        processed = len(dcm_files) - failed

        return {"processed": processed, "skipped": 0, "failed": failed}
