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
    # Added index=True to prevent O(N) full table scans during the EXISTS check
    file_path = Column(String(500), unique=True, nullable=False, index=True)
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
        "series_description": getattr(ds, "SeriesDescription", None),
    }


def iter_dicom_files(folder):
    for root, _, files in os.walk(folder):
        for fname in files:
            if fname.lower().endswith((".dcm", ".dicom")):
                yield os.path.join(root, fname)


def index_folder(folder, db_path):
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    added = 0
    skipped = 0
    failed = 0
    BATCH_SIZE = 100

    # Context manager ensures connection is closed even if an exception occurs
    with Session() as session:

        # CHANGED: extracted duplicated batch processing logic into a helper function
        # to avoid code duplication between the main loop and the final cleanup block
        def process_batch(current_batch):
            nonlocal added, skipped, failed
            unique_batch = []
            seen = set()

            for abs_path in current_batch:
                if abs_path in seen:
                    skipped += 1
                    continue
                seen.add(abs_path)
                unique_batch.append(abs_path)

            existing = {
                p for (p,) in session.query(DICOMIndex.file_path).filter(
                    DICOMIndex.file_path.in_(unique_batch)
                ).all()
            }
            for abs_path in unique_batch:
                if abs_path in existing:
                    skipped += 1
                    continue
                meta = extract_metadata(abs_path)
                if meta is None:
                    failed += 1
                    continue
                session.add(DICOMIndex(**meta))
                added += 1
                
            session.commit()

        # Collect paths in BATCH_SIZE chunks and query the DB using IN operator
        # — memory stays flat regardless of index size
        batch = []
        for filepath in iter_dicom_files(folder):
            batch.append(os.path.abspath(filepath))

            if len(batch) >= BATCH_SIZE:
                process_batch(batch)
                logger.info("Indexed %d files so far ...", added)
                batch = []  # reset batch

        # Process any remaining files that didn't fill a complete batch
        if batch:
            process_batch(batch)

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
