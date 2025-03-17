import pandas as pd
from Scripts import load_dicom_files, extract_metadata

def query_metadata(metadata_df, query):
    """Query the metadata DataFrame and return matching rows."""
    return metadata_df.query(query)

# Load DICOM files and extract metadata
dicom_directory = "data/dicom_files"  # Update this path
dicom_files = load_dicom_files(dicom_directory)
metadata_df = extract_metadata(dicom_files)

# Example query
query = "Modality == 'CT'"
subset_df = query_metadata(metadata_df, query)
print(subset_df)