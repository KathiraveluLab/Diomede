import os
import pydicom
import pandas as pd
import argparse
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(message)s')


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
                    logging.error(f"Error reading {file}: {e}")
                    continue  # this makes the script skip the file and continue


    return pd.DataFrame(metadata_list)


if __name__ == "__main__":
    path = "C:\Users\asus\Diomede"  # Replace this with the actual folder path
    # Parse command-line arguments
parser = argparse.ArgumentParser(description="Extract metadata from DICOM files and save to CSV.")
parser.add_argument("input_folder", help="Path to the folder containing DICOM files")
parser.add_argument("output_file", nargs="?", default="dicom_metadata.csv", help="Output CSV file name (default: dicom_metadata.csv)")
args = parser.parse_args()

# Run metadata extraction
df = extract_metadata(args.input_folder)

# Save to CSV
df.to_csv(args.output_file, index=False)
print(f"Metadata saved to {args.output_file}")

