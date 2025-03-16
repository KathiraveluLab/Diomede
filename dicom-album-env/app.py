from flask import Flask, request, render_template
import pandas as pd
import os
import shutil
import sys
import os
import requests
import pydicom
sys.path.append("Scripts")


# Import your existing functions
from Scripts.load_dicom import load_dicom_files
from Scripts.extract_metadata import extract_metadata
from Scripts.query_metadata import query_metadata


app = Flask(__name__)

# Load DICOM files and extract metadata (run once when the app starts)
current_path = os.path.join(os.getcwd(), os.path.dirname(__file__))
print(current_path)
dicom_directory = current_path + "/data/dicom_files"  
dicom_files = load_dicom_files(dicom_directory)
metadata_df = extract_metadata(dicom_files)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        query = request.form["query"]
        album_name = request.form["album_name"]
        subset_df = query_metadata(metadata_df, query)
        create_album(subset_df, album_name)
        return f"Album '{album_name}' created successfully!"
    return render_template("index.html")



def create_album(subset_df, album_name, kheops_api_url, write_token):
    # """
    # Creates a KHEOPS album by uploading DICOM files via STOW-RS protocol.

    # Parameters:
    # - subset_df: DataFrame containing 'FilePath' to local DICOM files.
    # - album_name: Name for the new album (must pre-exist in KHEOPS).
    # - kheops_api_url: Base API URL (e.g., https://kheops.instance/api).
    # - write_token: Token with WRITE permission on the target album.
    # """
    # headers = {
    #     "Authorization": f"Bearer {write_token}",
    #     "Content-Type": "application/dicom"
    # }

    # uploaded_count = 0
    # for _, row in subset_df.iterrows():
    #     dicom_file = row["FilePath"]
        
    #     try:
    #         # Validate DICOM file
    #         ds = pydicom.dcmread(dicom_file, stop_before_pixels=True)
            
    #         # STOW-RS upload to /studies endpoint
    #         with open(dicom_file, 'rb') as f:
    #             response = requests.post(
    #                 f"{kheops_api_url}/studies",
    #                 headers=headers,
    #                 data=f.read()
    #             )
    #             response.raise_for_status()  # Raise an exception for HTTP errors
                
    #         uploaded_count += 1
            
    #     except (IOError, pydicom.errors.InvalidDicomError) as e:
    #         print(f"Invalid DICOM file {dicom_file}: {e}")
    #     except requests.HTTPError as e:
    #         print(f"Upload failed for {dicom_file}: {e.response.text}")
    #     except Exception as e:
    #         print(f"Unexpected error for {dicom_file}: {e}")

    print(f"Album '{album_name}' created with {uploaded_count}/{len(subset_df)} successful uploads.")


if __name__ == "__main__":
    app.run(debug=True)