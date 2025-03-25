from .. import db
from datetime import datetime

class DICOMFile(db.Model):
    __tablename__ = 'dicom_files'
    
    id = db.Column(db.Integer, primary_key=True)
    file_path = db.Column(db.String(500), unique=True, nullable=False)
    patient_id = db.Column(db.String(100), index=True)
    study_uid = db.Column(db.String(100), index=True)
    series_uid = db.Column(db.String(100))
    modality = db.Column(db.String(50))
    study_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<DICOMFile {self.file_path}>'

class Album(db.Model):
    __tablename__ = 'albums'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    kheops_id = db.Column(db.String(100))
    share_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Album {self.name}>'