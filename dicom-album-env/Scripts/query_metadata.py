import pandas as pd
from Scripts import load_dicom_files, extract_metadata

def query_metadata(metadata_df, query):
    """Query the metadata DataFrame and return matching rows."""
    return metadata_df.query(query)

# The rest of the code will be handled by the Flask application in app.py. This function will be called within the create_album route to filter the metadata DataFrame based on the user's query. The filtered DataFrame will then be used to create the album.