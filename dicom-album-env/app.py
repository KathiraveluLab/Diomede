from flask import Flask, request, render_template, redirect, url_for, session
import pandas as pd
import os
import shutil
import sys
import tempfile
from Diomedex import create_app
sys.path.append("Scripts")

# Import your existing functions
from Scripts.load_dicom import load_dicom_files
from Scripts.extract_metadata import extract_metadata
from Scripts.query_metadata import query_metadata

# Load app secret key from environment variable
app = create_app()

@app.route("/", methods=["GET", "POST"])
def select_directory():
    target_directory = os.path.join(os.getcwd(), "data", "dicom_files")
    files_exist = os.path.exists(target_directory) and len(os.listdir(target_directory)) > 0

    if request.method == "POST":
        if 'action' in request.form:
            if request.form['action'] == 'remove':
                # Remove all files in the target directory
                shutil.rmtree(target_directory)
                os.makedirs(target_directory)
                files_exist = False
            elif request.form['action'] == 'proceed':
                return redirect(url_for('create_album'))
        else:
            files = request.files.getlist("dicom_directory")

            # Create target directory if it doesn't exist
            if not os.path.exists(target_directory):
                os.makedirs(target_directory)

            # Save uploaded files to target directory
            for file in files:
                # Save each file directly to the target directory
                file_path = os.path.join(target_directory, os.path.basename(file.filename))
                file.save(file_path)

            return redirect(url_for('create_album'))

    return render_template("select_directory.html", files_exist=files_exist)

@app.route("/create_album", methods=["GET", "POST"])
def create_album_route():
    if request.method == "POST":
<<<<<<< Updated upstream
        query = request.form.get("query", "")
        album_name = request.form.get("album_name", "default_album")

=======
        query = request.form.get("query", "").strip()
        if not query:
            return "Query cannot be empty", 400
        album_name = request.form.get("album_name", "").strip() or "default_album"
        
>>>>>>> Stashed changes
        # Load DICOM files and extract metadata
        target_directory = os.path.join(os.getcwd(), "data", "dicom_files")
        dicom_files = load_dicom_files(target_directory)
        metadata_df = extract_metadata(dicom_files)

        # Query metadata
        subset_df = query_metadata(metadata_df, query)
        # Cleanup previous temp file (if exists)
        old_path = session.get('subset_path')
        if old_path and os.path.exists(old_path):
            os.remove(old_path)
        # Create new temp file
        fd, temp_path = tempfile.mkstemp(suffix='.csv', prefix='subset_')
        os.close(fd)
        subset_df.to_csv(temp_path, index=False)
        # Store only reference in session
        session['subset_path'] = temp_path
        session['album_name'] = album_name

        return redirect(url_for('view_query_results'))
    return render_template("create_album.html")

@app.route("/view_query_results", methods=["GET", "POST"])
def view_query_results():
<<<<<<< Updated upstream
    subset_path = session.get('subset_path')
=======
    data = session.get('subset_df')

    if not data:
        subset_df = pd.DataFrame()
    else:
        subset_df = pd.DataFrame(data)
        
>>>>>>> Stashed changes
    album_name = session.get('album_name')

    if subset_path and os.path.exists(subset_path):
        subset_df = pd.read_csv(subset_path)
    else:
        subset_df = pd.DataFrame()

    if request.method == "POST":
        # Create album
        create_album(subset_df, album_name)

        # Cleanup temp file after use
        if subset_path and os.path.exists(subset_path):
            os.remove(subset_path)
            session.pop('subset_path', None)
        
        return f"Album '{album_name}' created successfully!"

    return render_template("view_query_results.html", tables=[subset_df.to_html(classes='data')], titles=subset_df.columns.values)

def create_album(subset_df, album_name):
    # album_directory = os.path.join(os.getcwd(), "albums", album_name)
    # if not os.path.exists(album_directory):
    #     os.makedirs(album_directory)

    # for index, row in subset_df.iterrows():
    #     dicom_file_path = row['FilePath']
    #     shutil.copy(dicom_file_path, album_directory)
    pass

if __name__ == "__main__":
    app.run(debug=True)
