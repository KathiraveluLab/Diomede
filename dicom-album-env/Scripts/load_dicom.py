import os
from Diomedex.utils.dicom_helpers import safe_load_dicom_file


def load_dicom_files(directory):
    """Load all DICOM files from a directory."""
    dicom_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            dicom_file = safe_load_dicom_file(file_path)
            if dicom_file is not None:
                dicom_files.append(dicom_file)
    return dicom_files