import os
import pydicom

def load_dicom_files(directory):
    """Load all DICOM files from a directory."""
    dicom_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                dicom_file = pydicom.dcmread(file_path)
                dicom_files.append(dicom_file)
            except pydicom.errors.InvalidDicomError:
                print(f"Skipping non-DICOM file: {file}")
    return dicom_files