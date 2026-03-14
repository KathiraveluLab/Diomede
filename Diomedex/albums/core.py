import pydicom
from pathlib import Path
from typing import List, Dict
from ..models import db

class DICOMAlbumCreator:
    def __init__(self, storage_path: str):
        self.storage_path = Path(storage_path)
        
    def scan_directory(self, path: str) -> List[Dict]:
        """Scan a directory for DICOM files and return metadata"""
        dicom_files = []
        for dcm_path in Path(path).rglob('*'):
            if dcm_path.is_file():
                try:
                    ds = pydicom.dcmread(dcm_path)
                    dicom_files.append({
                        'path': str(dcm_path),
                        'patient_id': getattr(ds, 'PatientID', ''),
                        'study_uid': getattr(ds, 'StudyInstanceUID', ''),
                        'modality': getattr(ds, 'Modality', '')
                    })
                except Exception as e:
                    print(f"Error reading {dcm_path}: {str(e)}")
                    continue
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