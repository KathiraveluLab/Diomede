import uuid
import logging
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Table, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, joinedload

from dicom_indexer import DICOMIndex, Base
from query_metadata import query_metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

album_files = Table(
    "album_files",
    Base.metadata,
    Column("album_id", String(36), ForeignKey("albums.album_id")),
    Column("dicom_id", Integer, ForeignKey("DICOMIndex.id")),
)

class Album(Base):
    __tablename__ = "albums"

    album_id    = Column(String(36), primary_key=True,
                         default=lambda: str(uuid.uuid4()))
    name        = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    query_used  = Column(Text, nullable=True)
    created_at  = Column(DateTime,
                         default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    files = relationship("DICOMIndex", secondary=album_files, backref="albums")

    def __repr__(self):
        return f"<Album '{self.name}' | {len(self.files)} file(s) | {self.album_id}>"


def load_index_as_dataframe(session):
    """
    Load all rows from the DICOMIndex table into a pandas DataFrame.
    Column names are mapped to match the query_metadata.py field whitelist.
    """
    rows = session.query(DICOMIndex).all()

    if not rows:
        return pd.DataFrame()

    data = []
    for row in rows:
        data.append({
            "id":                row.id,
            "PatientID":         row.patient_id or "",
            "Modality":          row.modality or "",
            "StudyDate":         row.study_date.strftime("%Y%m%d")
                                 if row.study_date else "",
            "SeriesDescription": row.series_description or "",
            "FilePath":          row.file_path,
            "study_uid":         row.study_uid or "",
            "series_uid":        row.series_uid or "",
        })

    return pd.DataFrame(data)


def create_album(name, db_path, query, description=None):
    """
    Filter the DICOM index using a query string and save the result
    as a named album in the same SQLite database.
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        df = load_index_as_dataframe(session)

        if df.empty:
            logger.warning("Index is empty — run dicom_indexer first.")
            return None

        filtered_df = query_metadata(df, query)

        if filtered_df.empty:
            logger.warning("No files matched query: %s", query)
            return None

        logger.info("Query matched %d file(s).", len(filtered_df))

        matched_ids   = filtered_df["id"].tolist()
        matched_files = session.query(DICOMIndex)\
                               .filter(DICOMIndex.id.in_(matched_ids))\
                               .all()

        album = Album(
            name=name,
            description=description,
            query_used=query,
            files=matched_files,
        )

        session.add(album)
        session.commit()
        session.refresh(album)

        # Access files while session is still open, then detach
        _ = len(album.files)
        session.expunge_all()

        logger.info(
            "Album '%s' created — %d file(s) — ID: %s",
            album.name, len(album.files), album.album_id,
        )
        return album

    except ValueError:
        raise

    except Exception as exc:
        session.rollback()
        logger.error("Failed to create album: %s", exc)
        raise

    finally:
        session.close()


def list_albums(db_path):
    """
    Return all albums stored in the database with files eagerly loaded.
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        albums = session.query(Album)\
                        .options(joinedload(Album.files))\
                        .all()
        for a in albums:
            _ = len(a.files)
        session.expunge_all()
        return albums
    finally:
        session.close()


def get_album(album_id, db_path):
    """
    Return a single album by its UUID with files eagerly loaded, or None.
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        album = session.query(Album)\
                       .options(joinedload(Album.files))\
                       .filter(Album.album_id == album_id)\
                       .first()
        if album:
            _ = len(album.files)
            session.expunge_all()
        return album
    finally:
        session.close()