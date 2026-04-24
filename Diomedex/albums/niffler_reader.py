import os
import csv
from datetime import datetime
from typing import Iterator, Dict  # CHANGED: List removed, Iterator added since generators don't return Lists

EXPECTED_FIELDS = ["PatientID", "StudyInstanceUID", "Modality", "StudyDate", "filepath"]


def load_niffler_csv(csv_path: str) -> Iterator[Dict]:  # CHANGED: return type List[Dict] -> Iterator[Dict]
    """
    Load DICOM metadata records from a CSV file produced by Niffler.
    Niffler's cold-extraction and meta-extraction modules output metadata
    as CSV files. This function yields records one at a time to avoid
    loading the entire file into memory — Niffler CSVs can be very large.

    Args:
        csv_path: Absolute path to Niffler's output CSV file.
    Yields:                                                         #CHANGED: Returns -> Yields in docstring
        One dict per DICOM file entry in the CSV.
    Raises:
        FileNotFoundError: If the CSV path does not exist.
        ValueError: If required Niffler columns are missing from the CSV.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Niffler CSV not found at: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames:
            missing = [field for field in EXPECTED_FIELDS if field not in reader.fieldnames]
            if missing:
                raise ValueError(f"Niffler CSV is missing expected columns: {missing}. Make sure this file was produced by Niffler's meta-extraction module.")
        # CHANGED: removed return list(reader) - replaced with yield to stream rows one at a time
        # This prevents loading a 10,000x10,000 CSV entirely into memory
        for row in reader:
            yield row


def filter_metadata(records: Iterator[Dict], filters: Dict) -> Iterator[Dict]:  # CHANGED: List[Dict] -> Iterator[Dict] for both input and return type
    """
    Filter Niffler metadata records by any field-value pairs in a single pass.

    Args:
        records: Iterator of metadata dicts from load_niffler_csv().  # CHANGED: List -> Iterator in docstring
        filters: Dict of {field: value} pairs to filter on.
                 Example: {"Modality": "CT"} or {"PatientID": "P001", "Modality": "MR"}
    Yields:                                                            # CHANGED: Returns -> Yields in docstring
        Records that match all the given filters.
    """
    if not filters:
        yield from records  # CHANGED: was "return records" — now yields each record from the iterator
        return

    # Pre-process filters once (O(M) complexity)
    processed_filters = {f: str(v).strip() for f, v in filters.items()}

    # CHANGED: was a list comprehension — now a generator expression using yield
    # Single pass (O(N) complexity) with short-circuit evaluation
    for r in records:
        if all(str(r.get(field, "")).strip() == target_val for field, target_val in processed_filters.items()):
            yield r


def _parse_date(date_str: str):  # ADDED: new helper function to safely parse study dates
    """
    Safely parse a DICOM study date string (YYYYMMDD) into a datetime object.
    Returns None if the string is missing or malformed, so a single bad date
    in the CSV does not crash the entire indexing request.
    """
    try:
        return datetime.strptime(date_str, '%Y%m%d')
    except (ValueError, TypeError):
        return None


def to_album_index_format(records: Iterator[Dict]) -> Iterator[Dict]:  # CHANGED: List[Dict] -> Iterator[Dict] for both input and return type
    """
    Convert Niffler CSV records to the format expected by
    DICOMAlbumCreator.create_album_index().

    Niffler uses: filepath, PatientID, StudyInstanceUID, Modality
    create_album_index() expects: path, patient_id, study_uid, modality
    """
    # CHANGED: was a list comprehension — now a generator using yield
    # CHANGED: date parsing moved to _parse_date() helper to handle malformed dates gracefully
    for r in records:
        path = r.get('filepath', '').strip()
        if not path:  # skip records with no filepath, same logic as before
            continue
        yield {
            'path':       path,
            'patient_id': r.get('PatientID', '').strip(),
            'study_uid':  r.get('StudyInstanceUID', '').strip(),
            'modality':   r.get('Modality', '').strip(),
            'study_date': _parse_date(r.get('StudyDate', '').strip()),  # CHANGED: direct strptime -> _parse_date() for safe error handling
        }
