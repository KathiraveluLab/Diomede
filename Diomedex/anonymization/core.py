import logging
import pickle
import tempfile
from os import PathLike
from pathlib import Path
from typing import Dict, List, Union

import pydicom
from modules.dicom_anonymization.DicomAnonymizer2 import (
    dcm_anonymize as _niffler_dcm_anonymize,
)
from modules.dicom_anonymization.DicomAnonymizer2 import (
    get_dcm_paths as _niffler_get_dcm_paths,
)
from pydicom.uid import generate_uid

_REQUIRED_UID_TAGS = ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID")


def _ensure_required_tags(file_path: str) -> str:
    """Ensure a DICOM file has all UID tags required by Niffler.

    Niffler's ``dcm_anonymize`` unconditionally accesses ``StudyInstanceUID``,
    ``SeriesInstanceUID``, and ``SOPInstanceUID`` and will raise ``KeyError``
    on files that lack them (e.g. minimal or legacy files).

    If all tags are already present the original *file_path* is returned
    unchanged. If any are missing, a temporary copy is created with the tags
    patched in and its path is returned instead — the source file is never
    modified. The caller is responsible for deleting any returned temp file.
    """
    # Check headers first without loading large pixel data into memory
    ds = pydicom.dcmread(file_path, stop_before_pixels=True)
    missing_tags = [tag for tag in _REQUIRED_UID_TAGS if tag not in ds]

    if not missing_tags:
        return file_path

    # Re-read fully to capture pixel data before saving the patched copy
    ds = pydicom.dcmread(file_path)
    for tag in missing_tags:
        setattr(ds, tag, generate_uid())

    tmp = tempfile.NamedTemporaryFile(suffix=".dcm", delete=False)
    tmp.close()
    ds.save_as(tmp.name)
    return tmp.name


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

        patched_files: List[str] = []
        temp_files: List[str] = []
        pre_failed = 0
        for f in dcm_files:
            try:
                patched = _ensure_required_tags(f)
                patched_files.append(patched)
                if patched != f:
                    temp_files.append(patched)
            except Exception as e:
                LOG.warning("Skipping %s: could not patch required UID tags: %s", f, str(e))
                pre_failed += 1

        if not patched_files:
            return {"processed": 0, "skipped": 0, "failed": pre_failed}

        try:
            dest.mkdir(parents=True, exist_ok=True)
            _niffler_dcm_anonymize(patched_files, str(dest))
        finally:
            for tmp in temp_files:
                Path(tmp).unlink(missing_ok=True)

        skipped_pkl = dest / "skipped.pkl"
        if skipped_pkl.exists():
            # skipped.pkl is written by Niffler's DicomAnonymizer2, a trusted library
            # maintained by the same organization KathiraveluLab. Loading its pickle
            # output is an acceptable risk.
            with open(str(skipped_pkl), "rb") as f:
                failed = len(pickle.load(f))
        else:
            failed = 0
        processed = len(patched_files) - failed

        return {"processed": processed, "skipped": 0, "failed": failed + pre_failed}
