from flask import Flask, request, render_template, redirect, url_for, session
import pandas as pd
import os
import shutil
import sys
import requests
import pydicom
sys.path.append("Scripts")

# Import your existing functions
from Scripts.load_dicom import load_dicom_files
from Scripts.extract_metadata import extract_metadata
from Scripts.query_metadata import query_metadata

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # Needed for session management

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
def create_album():
    if request.method == "POST":
        query = request.form["query"]
        album_name = request.form["album_name"]
        
        # Load DICOM files and extract metadata
        target_directory = os.path.join(os.getcwd(), "data", "dicom_files")
        dicom_files = load_dicom_files(target_directory)
        metadata_df = extract_metadata(dicom_files)
        
        # Query metadata
        subset_df = query_metadata(metadata_df, query)
        session['subset_df'] = subset_df.to_dict()  # Store the DataFrame in session
        session['album_name'] = album_name
        
        return redirect(url_for('view_query_results'))
    return render_template("create_album.html")

@app.route("/view_query_results", methods=["GET", "POST"])
def view_query_results():
    subset_df = pd.DataFrame(session.get('subset_df'))
    album_name = session.get('album_name')
    
    if request.method == "POST":
        # Create album
        create_album(subset_df, album_name)
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