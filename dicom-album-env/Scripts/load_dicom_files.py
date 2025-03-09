import os

def load_dicom_files(dicom_directory):
    dicom_files = []
    for root, dirs, files in os.walk(dicom_directory):
        for file in files:
            if file.endswith(".dcm"):
                dicom_files.append(os.path.join(root, file))
    print(f"Loaded {len(dicom_files)} DICOM files.")
    return dicom_files