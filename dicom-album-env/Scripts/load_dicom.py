import os

def load_dicom_files(dicom_directory):
    dicom_files = []
    for file in os.listdir(dicom_directory):
        if file.endswith(".dcm"):
            dicom_files.append(f"{dicom_directory}/{file}")
    print(f"Loaded {len(dicom_files)} DICOM files.")
    return dicom_files