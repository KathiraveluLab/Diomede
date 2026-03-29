import pydicom
import uuid
import os

# These are standard DICOM tags that typically contain Protected Health Information
PHI_TAGS = [
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "InstitutionName",
    "ReferringPhysicianName",
    "AccessionNumber",
    "StudyID",
    "PatientSex",
    "PatientAge"
]

def anonymize_dataset(dataset):
    """
    Remove PHI tags from a single pydicom Dataset iteratively.
    Adds PatientIdentityRemoved='YES' compliance tag.
    """
    for tag in PHI_TAGS:
        if tag in dataset:
            # Type VR=PN (Person Name) and others can often be set to empty string safely
            dataset.data_element(tag).value = ""
            
    # Add DICOM Standard Compliance flag
    dataset.PatientIdentityRemoved = "YES"

    return dataset

def anonymize_dicom_file_in_memory(file_stream):
    """ 
    Reads a DICOM file stream (like a Flask request.file), 
    scrubs PHI, and returns the scrubbed dataset. 
    """
    ds = pydicom.dcmread(file_stream)
    ds = anonymize_dataset(ds)
    return ds

def anonymize_dicom_file_on_disk(input_path, output_path):
    """ Reads a DICOM file, scrubs PHI, saves it to output_path. """
    try:
        ds = pydicom.dcmread(input_path)
        ds = anonymize_dataset(ds)
        ds.save_as(output_path)
        return True, "Anonymized successfully"
    except Exception as e:
        return False, str(e)
