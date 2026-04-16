import os
import argparse
import logging
from datetime import datetime, timezone

import pydicom
from pydicom.errors import InvalidDicomError
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

Base = declarative_base()


class DICOMIndex(Base):
    __tablename__ = "DICOMIndex"

    id = Column(Integer, primary_key=True)
    file_path = Column(String(500), unique=True, nullable=False)
    patient_id = Column(String(100), index=True)
    series_description = Column(String(300))
    study_uid = Column(String(200), index=True)
    study_date = Column(DateTime)
    series_uid = Column(String(200))
    indexed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    modality = Column(String(50))

    def __repr__(self):
        return f"<DICOMIndex {self.file_path}>"

def parse_study_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y%m%d")
    except ValueError:
        return None

def extract_metadata(filepath):
    try:
        ds = pydicom.dcmread(filepath, stop_before_pixels=True)
    except InvalidDicomError:
        logger.warning("Skipping non-DICOM file: %s", filepath)
        return None
    except Exception as exc:
        logger.warning("Could not read %s — %s", filepath, exc)
        return None

    return {
        "file_path": os.path.abspath(filepath),
        "patient_id": getattr(ds, "PatientID", None),
        "study_uid": getattr(ds, "StudyInstanceUID", None),
        "series_uid": getattr(ds, "SeriesInstanceUID", None),
        "modality": getattr(ds, "Modality", None),
        "study_date": parse_study_date(getattr(ds, "StudyDate", None)),
        "series_description": getattr(ds, "SeriesDescription", None),}

def iter_dicom_files(folder):
    for root, _, files in os.walk(folder):
        for fname in files:
            if fname.lower().endswith((".dcm", ".dicom")) or "." not in fname:
                yield os.path.join(root, fname)

def index_folder(folder, db_path):
    engine= create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    Session= sessionmaker(bind=engine)
    session= Session()
   
def file_already_indexed(session, file_path):
    return session.query(DICOMIndex).filter_by(file_path=file_path).first() is not None
    existing= {row[0] for row in session.query(DICOMIndex.file_path).all()}
    added= 0
    skipped= 0
    failed= 0

    for filepath in iter_dicom_files(folder):
        abs_path= os.path.abspath(filepath)
        if abs_path in existing:
            skipped += 1
            continue
        meta= extract_metadata(filepath)
        if meta is None:
            failed += 1
            continue
        session.add(DICOMIndex(**meta))
        existing.add(abs_path)
        added += 1
        if added % 100 == 0:
            session.commit()
            logger.info("Indexed %d files so far ...", added)

    session.commit()
    session.close()
    logger.info("Done — added: %d  skipped: %d  failed: %d", added, skipped, failed)
    logger.info("Database written to: %s", os.path.abspath(db_path))

def main():
    parser = argparse.ArgumentParser(
        description="Scan a folder of DICOM files and index their metadata into SQLite."
    )
    parser.add_argument(
        "folder",
        help="Path to the folder containing DICOM files (scanned recursively).",
    )
    parser.add_argument(
        "--db",
        default="dicom_index.db",
        help="Output SQLite database file (default: dicom_index.db).",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        logger.error("'%s' is not a valid directory.", args.folder)
        return

    logger.info("Scanning folder: %s", os.path.abspath(args.folder))
    index_folder(args.folder, args.db)


if __name__ == "__main__":
    main()