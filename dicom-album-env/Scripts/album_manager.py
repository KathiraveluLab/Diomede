import uuid
import logging
from datetime import datetime, timezone
 
import pandas as pd
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, Table, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
 
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
    query_used  = Column(Text, nullable=True)   # stores the query string for reference
    created_at  = Column(DateTime,
                         default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
 
    # SQLAlchemy relationship — gives us album.files as a Python list
    files = relationship("DICOMIndex", secondary=album_files, backref="albums")
 
    def __repr__(self):
        return f"<Album '{self.name}' | {len(self.files)} file(s) | {self.album_id}>"
 
def load_index_as_dataframe(session):
    """
    Load all rows from the DICOMIndex table into a pandas DataFrame.
    Column names are mapped to match the query_metadata.py field whitelist.
 
    Parameters
    ----------
    session : SQLAlchemy session (already open)
 
    Returns
    -------
    pd.DataFrame with columns:
        id, PatientID, Modality, StudyDate, SeriesDescription,
        AccessionNumber, FilePath, study_uid, series_uid
    """
    rows = session.query(DICOMIndex).all()
 
    if not rows:
        return pd.DataFrame()
 
    data = []
    for row in rows:
        data.append({
            "id":               row.id,
            # Map to query_metadata's expected field names
            "PatientID":        row.patient_id or "",
            "Modality":         row.modality or "",
            # StudyDate stored as datetime — convert back to YYYYMMDD string
            # so query_metadata.py can parse it correctly
            "StudyDate":        row.study_date.strftime("%Y%m%d")
                                if row.study_date else "",
            "SeriesDescription": row.series_description or "",
            # query_metadata expects FilePath, not file_path
            "FilePath":         row.file_path,
            # Keep UIDs for reference
            "study_uid":        row.study_uid or "",
            "series_uid":       row.series_uid or "",
        })
 
    return pd.DataFrame(data)
 
def create_album(name, db_path, query, description=None):
    """
    Filter the DICOM index using a query string and save the result
    as a named album in the same SQLite database.
 
    This is the core function of this module. It:
      1. Loads the SQLite index into a DataFrame
      2. Passes it through the existing query_metadata engine
      3. Fetches the matching DICOMIndex rows from SQLAlchemy
      4. Persists them as a named Album
 
    Parameters
    ----------
    name        : str   Album name (required)
    db_path     : str   Path to the SQLite DB built by dicom_indexer
    query       : str   Query string using query_metadata syntax e.g.
                        "Modality == 'CT' and StudyDate > '20200101'"
    description : str   Optional free-text description
 
    Returns
    -------
    Album  The persisted Album object, or None if no files matched.
 
    Raises
    ------
    ValueError  If the query string is invalid (from query_metadata)
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)   # creates albums + album_files if missing
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
    Return all albums stored in the database.
 
    Parameters
    ----------
    db_path : str  Path to the SQLite DB
 
    Returns
    -------
    list of Album objects
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        return session.query(Album).all()
    finally:
        session.close()

def get_album(album_id, db_path):
    """
    Return a single album by its UUID, or None if not found.
 
    Parameters
    ----------
    album_id : str  The UUID string of the album
    db_path  : str  Path to the SQLite DB
 
    Returns
    -------
    Album or None
    """
    engine  = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        return session.query(Album)\
                      .filter(Album.album_id == album_id)\
                      .first()
    finally:
        session.close()