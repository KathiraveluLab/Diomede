from flask import Flask, request, render_template
import pandas as pd
import os
import shutil
import sys
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

def create_album(subset_df, album_name):
    """Create an album by copying the queried DICOM files to a new directory."""
    album_dir = os.path.join("albums", album_name)
    os.makedirs(album_dir, exist_ok=True)
    
    for _, row in subset_df.iterrows():
        shutil.copy(row["FilePath"], album_dir)
    
    print(f"Album '{album_name}' created with {len(subset_df)} files.")

if __name__ == "__main__":
    app.run(debug=True)