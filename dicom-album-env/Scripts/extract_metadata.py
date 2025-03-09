import pandas as pd

def extract_metadata(dicom_files):
    """Extract metadata from DICOM files and return a DataFrame."""
    metadata = []
    for dicom_file in dicom_files:
        metadata.append({
            "PatientID": dicom_file.get("PatientID", ""),
            "StudyDate": dicom_file.get("StudyDate", ""),
            "Modality": dicom_file.get("Modality", ""),
            "SeriesDescription": dicom_file.get("SeriesDescription", ""),
            "FilePath": dicom_file.filename,
        })
    return pd.DataFrame(metadata)

# Example usage
metadata_df = extract_metadata(dicom_files)
print(metadata_df.head())  # Display the first few rows of the DataFrame