import pandas as pd
from Scripts import load_dicom_files, extract_metadata

def query_metadata(metadata_df, query):
    """Query the metadata DataFrame and return matching rows."""
    return metadata_df.query(query)
