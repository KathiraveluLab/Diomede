import logging
from pathlib import Path
from typing import List, Dict
from .models import db, DICOMFile
from ..utils.dicom_helpers import safe_load_dicom_file

LOG = logging.getLogger(__name__)

class DICOMAlbumCreator:
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        
    def scan_directory(self, path: str) -> List[Dict]:
        """Scan a directory for DICOM files and return metadata"""
        dicom_files = []
        for dcm_path in Path(path).rglob('*'):
            if dcm_path.is_file():
                ds = safe_load_dicom_file(dcm_path)
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
            unique_paths = list({
                p for file_info in files
                if isinstance(file_info, dict)
                and isinstance(path := file_info.get('path'), str)
                and (p := path.strip())
            })
            existing_paths = set()
            for i in range(0, len(unique_paths), 500):
                chunk = unique_paths[i:i + 500]
                existing_paths.update(
                    f.file_path
                    for f in db.session.query(DICOMFile.file_path)
                    .filter(DICOMFile.file_path.in_(chunk))
                    .all()
                )
            count = 0
            for file_info in files:
                if not isinstance(file_info, dict):
                    LOG.warning("Skipping malformed file_info entry (not a dictionary)")
                    continue

                path = file_info.get('path')
                if not isinstance(path, str) or not (path := path.strip()):
                    LOG.warning("Skipping file_info entry with missing or invalid path")
                    continue
                if path not in existing_paths:
                    dicom_file = DICOMFile(
                        file_path=path,
                        patient_id=file_info.get('patient_id', ''),
                        study_uid=file_info.get('study_uid', ''),
                        modality=file_info.get('modality', '')
                    )
                    db.session.add(dicom_file)
                    existing_paths.add(path)
                    count += 1
                    if count % 500 == 0:
                        db.session.commit()
            db.session.commit()
            return True
        except Exception as e:
            db.session.rollback()
<<<<<<< replace-print-with-logging
            LOG.exception("Error indexing files")
=======
            print(f"Error indexing files: {str(e)}")
>>>>>>> main
            return False
