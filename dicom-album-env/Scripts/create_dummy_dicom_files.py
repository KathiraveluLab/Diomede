import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
import os

def create_dummy_dicom(file_path, patient_id, study_date, modality, series_description):
    """Create a dummy DICOM file with minimal metadata."""
    # Create a minimal DICOM dataset
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
    file_meta.MediaStorageSOPInstanceUID = "1.2.3"
    file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"  # Explicit VR Little Endian

    ds = FileDataset(file_path, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientID = patient_id
    ds.StudyDate = study_date
    ds.Modality = modality
    ds.SeriesDescription = series_description

    # Save the DICOM file
    ds.save_as(file_path)

# Create a directory for dummy DICOM files in the root of the project
dicom_directory = os.path.join("data", "dicom_files")
os.makedirs(dicom_directory, exist_ok=True)

# Create a few dummy DICOM files
create_dummy_dicom(os.path.join(dicom_directory, "file1.dcm"), "12345", "20220101", "CT", "Abdomen")
create_dummy_dicom(os.path.join(dicom_directory, "file2.dcm"), "12345", "20220102", "MR", "Brain")
create_dummy_dicom(os.path.join(dicom_directory, "file3.dcm"), "67890", "20220103", "CT", "Chest")

print(f"Created dummy DICOM files in {dicom_directory}")