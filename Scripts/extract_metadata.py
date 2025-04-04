import os
import pydicom
import pandas as pd

def extract_metadata(dicom_dir):
    """
    Extract metadata from all DICOM files in the given directory.
    """
    metadata_list = []

    for root, _, files in os.walk(dicom_dir):
        for file in files:
            if file.endswith(".dcm"):
                print(f"Found DICOM file: {file}")  # Debug line
                try:
                    dcm = pydicom.dcmread(os.path.join(root, file))
                    metadata = {
                        "PatientID": getattr(dcm, "PatientID", None),
                        "StudyDate": getattr(dcm, "StudyDate", None),
                        "Modality": getattr(dcm, "Modality", None),
                        "StudyDescription": getattr(dcm, "StudyDescription", None),
                        "SeriesDescription": getattr(dcm, "SeriesDescription", None),
                        "BodyPartExamined": getattr(dcm, "BodyPartExamined", None),
                        "Manufacturer": getattr(dcm, "Manufacturer", None),
                        "FilePath": os.path.join(root, file)
                    }
                    metadata_list.append(metadata)
                except Exception as e:
                    print(f"Error reading {file}: {e}")

    return pd.DataFrame(metadata_list)


if __name__ == "__main__":
    path = "path/to/dicom/files"  # Replace this with the actual folder path
    df = extract_metadata(path)
    print(df.head())
