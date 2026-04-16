import os
import csv
from typing import List, Dict

EXPECTED_FIELDS = ["PatientID", "StudyInstanceUID", "Modality", "StudyDate", "filepath"]


def load_niffler_csv(csv_path: str) -> List[Dict]:
    """
    Load DICOM metadata records from a CSV file produced by Niffler.
    Niffler's cold-extraction and meta-extraction modules output metadata
    as CSV files. This function reads that file and returns the records
    as a list of dicts for downstream album creation.
    Args:
        csv_path: Absolute path to Niffler's output CSV file.
    Returns:
        List of dicts, one per DICOM file entry in the CSV.
    Raises:
        FileNotFoundError: If the CSV path does not exist.
        ValueError: If required Niffler columns are missing from the CSV.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Niffler CSV not found at: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return []

    missing = [field for field in EXPECTED_FIELDS if field not in rows[0]]
    if missing:
        raise ValueError(
            f"Niffler CSV is missing expected columns: {missing}. "
            f"Make sure this file was produced by Niffler's meta-extraction module."
        )

    return rows


def filter_metadata(records: List[Dict], filters: Dict) -> List[Dict]:
    """
    Filter Niffler metadata records by any field-value pairs.
    Args:
        records: List of metadata dicts from load_niffler_csv().
        filters: Dict of {field: value} pairs to filter on.
                 Example: {"Modality": "CT"} or {"PatientID": "P001", "Modality": "MR"}
    Returns:
        List of records that match all the given filters.
    """
    processed_filters = {f: str(v).strip() for f, v in filters.items()}
    return [
    r for r in records
    if all(str(r.get(f, "")).strip() == v for f, v in processed_filters.items())
]

def to_album_index_format(records: List[Dict]) -> List[Dict]:
    """Convert Niffler CSV records to the format expected by
    DICOMAlbumCreator.create_album_index().
    Niffler uses: filepath, PatientID, StudyInstanceUID, Modality
    create_album_index() expects: path, patient_id, study_uid, modality
    """
    return [
        {
            'path':       r.get('filepath', '').strip(),
            'patient_id': r.get('PatientID', '').strip(),
            'study_uid':  r.get('StudyInstanceUID', '').strip(),
            'modality':   r.get('Modality', '').strip(),
        }
        for r in records
        if r.get('filepath', '').strip()
    ]