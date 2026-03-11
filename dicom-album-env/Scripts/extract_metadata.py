import pandas as pd


def extract_metadata(dicom_files):
    """
    Extract metadata from DICOM files and return a structured DataFrame.
    
    This function safely extracts commonly used DICOM fields from a list of
    DICOM dataset objects. Missing fields are handled gracefully with default
    empty strings to ensure robustness across diverse DICOM files.
    
    Args:
        dicom_files: List of pydicom Dataset objects to extract metadata from.
    
    Returns:
        pd.DataFrame: A DataFrame containing extracted DICOM metadata with one
                     row per DICOM file.
    """
    metadata = []
    for dicom_file in dicom_files:
        metadata.append({
            # Patient Information
            "PatientID": dicom_file.get("PatientID", ""),
            "PatientAge": dicom_file.get("PatientAge", ""),
            "PatientSex": dicom_file.get("PatientSex", ""),
            
            # Study Information
            "StudyDate": dicom_file.get("StudyDate", ""),
            "StudyDescription": dicom_file.get("StudyDescription", ""),
            "AccessionNumber": dicom_file.get("AccessionNumber", ""),
            
            # Series Information
            "Modality": dicom_file.get("Modality", ""),
            "SeriesDescription": dicom_file.get("SeriesDescription", ""),
            "SeriesNumber": dicom_file.get("SeriesNumber", ""),
            
            # File Reference
            "FilePath": dicom_file.filename,
        })
    return pd.DataFrame(metadata)

