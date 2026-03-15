import logging
from pathlib import Path
from typing import List, Dict

from ..models import db, DICOMFile
from ..utils.dicom_helpers import safe_load_dicom_file

LOG = logging.getLogger(__name__)


def _safe_load_dicom_file(file_path):
    """Wrapper for safe DICOM file loading.

    Keeps the same API for scan_directory but delegates to a shared helper.
    """
    try:
        return safe_load_dicom_file(file_path)
    except Exception as ex:
        # Should not normally happen; preserve scanning behavior even on edge cases
        LOG.warning("Unexpected error while loading DICOM file %s: %s", file_path, ex)
        return None


class DICOMAlbumCreator:
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        
    def scan_directory(self, path: str) -> List[Dict]:
        """Scan a directory for DICOM files and return metadata"""
        dicom_files = []
        for dcm_path in Path(path).rglob('*'):
            if dcm_path.is_file():
                ds = _safe_load_dicom_file(dcm_path)
                if ds is None:
                    continue

                # Use safe .get field access and defaults for partially broken datasets
                # .get is explicit and avoids reflection-style behaviors.
                dicom_files.append({
                    'path': str(dcm_path),
                    'patient_id': ds.get('PatientID', ''),
                    'study_uid': ds.get('StudyInstanceUID', ''),
                    'modality': ds.get('Modality', ''),
                })
        return dicom_files

    def create_album_index(self, files: List[Dict]) -> bool:
        """Index DICOM files in database"""
        try:
            for file_info in files:
                if not DICOMFile.query.filter_by(file_path=file_info['path']).first():
                    dicom_file = DICOMFile(
                        file_path=file_info['path'],
                        patient_id=file_info['patient_id'],
                        study_uid=file_info['study_uid'],
                        modality=file_info['modality']
                    )
                    db.session.add(dicom_file)
            db.session.commit()
            return True
        except Exception as e:
            db.session.rollback()
            print(f"Error indexing files: {str(e)}")
            return False