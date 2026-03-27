import logging
from os import PathLike
from pathlib import Path
from typing import Dict, Optional, Union

import pydicom

from ..utils.dicom_helpers import safe_load_dicom_file

LOG = logging.getLogger(__name__)

# PHI attribute keywords defined by DICOM PS 3.15 Annex E — Confidentiality Profiles. Each tag's value is
# to be replaced with an empty string so that downstream viewers receive a structurally valid dataset.
_PHI_TAGS = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "PatientAge",
    "PatientSize",
    "PatientWeight",
    "PatientComments",
    "AdditionalPatientHistory",
    "AccessionNumber",
    "InstitutionName",
    "InstitutionAddress",
    "InstitutionalDepartmentName",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    "NameOfPhysiciansReadingStudy",
)

# UID attributes that must be regenerated so studies cannot be re-identified by cross-referencing UIDs.
# The anonymizer keeps a session-scoped map so that all files belonging to the same original study/series
# receive the same replacement UID.
_UID_TAGS = (
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPInstanceUID",
)


class DICOMAnonymizer:
    """Strips PHI from DICOM datasets and regenerates linkable UIDs.

    A single instance maintains a UID mapping across calls, so files from the same original study or series
    receive consistent replacement UIDs.
    Create a new instance whenever you need an independent UID namespace (e.g. for a fresh API request).
    """

    def __init__(self) -> None:
        # Maps original UID string -> replacement UID within this session.
        self._uid_map: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _blank_phi(self, ds: pydicom.Dataset) -> None:
        """Set every PHI tag that is present to an empty string."""
        for keyword in _PHI_TAGS:
            if hasattr(ds, keyword):
                try:
                    ds[keyword].value = ""
                except Exception as exc:
                    LOG.debug("Could not blank tag %s, attempting to delete. Error: %s", keyword, exc)
                    try:
                        # If blanking fails, delete the tag to ensure PHI is removed
                        del ds[keyword]
                    except Exception as exc_del:
                        LOG.warning("Could not blank or delete tag %s; skipping. Error: %s", keyword, exc_del)

    def _remap_uids(self, ds: pydicom.Dataset) -> None:
        """Replace each UID tag with a newly generated substitute."""
        for keyword in _UID_TAGS:
            if hasattr(ds, keyword):
                old_uid = str(getattr(ds, keyword))
                if old_uid:
                    if old_uid not in self._uid_map:
                        self._uid_map[old_uid] = pydicom.uid.generate_uid()
                    setattr(ds, keyword, self._uid_map[old_uid])

    @staticmethod
    def _stamp_deidentified(ds: pydicom.Dataset) -> None:
        """Add DICOM de-identification markers to the dataset."""
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = "Diomede Basic Profile"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def anonymize_dataset(
        self,
        ds: pydicom.Dataset,
        patient_id: Optional[str] = None,
    ) -> pydicom.Dataset:
        """Anonymize a pydicom Dataset in-place and return it.

        All PHI tags listed in *_PHI_TAGS* are blanked to empty string. UIDs in *_UID_TAGS* are replaced with
        freshly generated values (consistent within this anonymizer instance). The dataset is stamped with
        ``PatientIdentityRemoved = YES`` per the DICOM standard.

        Args:
            ds: The dataset to modify.
            patient_id: Optional replacement identifier written to both ``PatientName`` and ``PatientID``.
            When omitted those tags are left as empty strings.

        Returns:
            The same Dataset object after modification.
        """
        self._blank_phi(ds)

        if patient_id is not None:
            ds.PatientName = patient_id
            ds.PatientID = patient_id

        self._remap_uids(ds)
        self._stamp_deidentified(ds)
        return ds

    def anonymize_file(
        self,
        src_path: Union[str, PathLike],
        dest_path: Union[str, PathLike],
        patient_id: Optional[str] = None,
    ) -> bool:
        """Anonymize a single DICOM file and write the result to *dest_path*.

        Parent directories of *dest_path* are created automatically.

        Args:
            src_path: Path to the source DICOM file.
            dest_path: Destination path for the anonymized output.
            patient_id: Optional replacement patient identifier.

        Returns:
            True on success, False if the source file is unreadable or the write operation fails.
        """
        ds = safe_load_dicom_file(src_path)
        if ds is None:
            LOG.warning("Skipping non-DICOM or unreadable file: %s", src_path)
            return False

        self.anonymize_dataset(ds, patient_id=patient_id)

        try:
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(str(dest))
            LOG.debug("Anonymized %s -> %s", src_path, dest_path)
            return True
        except Exception as exc:
            LOG.error("Failed to write anonymized file %s: %s", dest_path, exc)
            return False

    def anonymize_directory(
        self,
        src_dir: Union[str, PathLike],
        dest_dir: Union[str, PathLike],
        patient_id_prefix: str = "ANON",
    ) -> Dict[str, int]:
        """Recursively anonymize every DICOM file under *src_dir* for up to 9,999 unique patient IDs per anonymization session.

        Each unique original PatientID receives a sequentially numbered replacement identifier built from
        *patient_id_prefix*, e.g. ``ANON0001``, ``ANON0002``.  The relative directory structure from
        *src_dir* is mirrored under *dest_dir*.

        Non-DICOM files (those rejected by ``safe_load_dicom_file``) are counted as skipped and left untouched.

        Args:
            src_dir: Root directory containing source DICOM files.
            dest_dir: Root directory for anonymized output.
            patient_id_prefix: Prefix for generated patient identifiers.

        Returns:
            A dict with integer counts for ``processed``, ``skipped``, and ``failed`` files.
        """
        src = Path(src_dir)
        dest = Path(dest_dir)

        if src.resolve() == dest.resolve():
            raise ValueError("Source and destination directories cannot be the same to prevent data corruption.")

        patient_id_map: Dict[str, str] = {}
        counter = 0
        stats: Dict[str, int] = {"processed": 0, "skipped": 0, "failed": 0}

        for dcm_path in sorted(src.rglob("*")):
            if not dcm_path.is_file():
                continue

            ds = safe_load_dicom_file(dcm_path)
            if ds is None:
                stats["skipped"] += 1
                continue

            original_pid = str(ds.get("PatientID", "") or "UNKNOWN")
            if original_pid not in patient_id_map:
                counter += 1
                patient_id_map[original_pid] = f"{patient_id_prefix}{counter:04d}"

            anon_pid = patient_id_map[original_pid]
            rel = dcm_path.relative_to(src)
            out_path = dest / rel

            if self.anonymize_file(dcm_path, out_path, patient_id=anon_pid):
                stats["processed"] += 1
            else:
                stats["failed"] += 1

        return stats
