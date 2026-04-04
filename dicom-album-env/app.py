from flask import Flask, request, render_template, redirect, url_for, session, flash
from werkzeug.utils import secure_filename
import pandas as pd
import os
import shutil
import sys
import requests
import pydicom
from Diomedex import create_app

# Add Scripts to path
sys.path.append(os.path.join(os.path.dirname(__file__), "Scripts"))

# Import your existing functions
from Scripts.load_dicom import load_dicom_files
from Scripts.extract_metadata import extract_metadata
from Scripts.query_metadata import query_metadata

app = create_app()
app.template_folder = os.path.abspath('templates')

def get_target_directory():
    """Returns absolute path to the DICOM storage directory."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "dicom_files"))

def get_albums_directory():
    """Returns absolute path to the Albums storage directory."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "albums"))

@app.route("/", methods=["GET", "POST"])
def select_directory():
    target_directory = get_target_directory()
    
    # Ensure directory exists
    if not os.path.exists(target_directory):
        os.makedirs(target_directory)
        
    files_exist = len(os.listdir(target_directory)) > 0

    if request.method == "POST":
        if 'action' in request.form:
            if request.form['action'] == 'remove':
                shutil.rmtree(target_directory)
                os.makedirs(target_directory)
                flash("DICOM directory cleared successfully.", "success")
                return redirect(url_for('select_directory'))
            elif request.form['action'] == 'proceed':
                return redirect(url_for('create_album'))
        else:
            files = request.files.getlist("dicom_directory")
            if not files or files[0].filename == '':
                flash("Please select at least one DICOM file to upload.", "warning")
                return redirect(url_for('select_directory'))

            for file in files:
                filename = secure_filename(file.filename)
                if filename:
                    file_path = os.path.join(target_directory, filename)
                    file.save(file_path)

            flash(f"Successfully uploaded {len(files)} files.", "success")
            return redirect(url_for('create_album'))

    return render_template("select_directory.html", files_exist=files_exist)

@app.route("/create_album", methods=["GET", "POST"])
def create_album():
    if request.method == "POST":
        query = request.form.get("query", "").strip()
        album_name_raw = request.form.get("album_name", "").strip()

        if not query or not album_name_raw:
            flash("Both Query and Album Name are required.", "warning")
            return render_template("create_album.html")

        album_name = secure_filename(album_name_raw)
        if not album_name:
            flash("Invalid Album Name. Please use a different name.", "warning")
            return render_template("create_album.html")

        try:
            target_directory = get_target_directory()
            dicom_files = load_dicom_files(target_directory)
            
            if not dicom_files:
                flash("No DICOM files found in target directory.", "error")
                return redirect(url_for('select_directory'))
                
            metadata_df = extract_metadata(dicom_files)

            # Issue #55: Catch grammar/value errors from the query engine
            subset_df = query_metadata(metadata_df, query)
            
            if subset_df.empty:
                flash("Query returned zero results. Please refine your filter.", "warning")
                return render_template("create_album.html")

            session['query'] = query
            session['album_name'] = album_name
            return redirect(url_for('view_query_results'))

        except ValueError as e:
            flash(f"Query Error: {str(e)}", "error")
            return render_template("create_album.html")
        except Exception as e:
            flash(f"An unexpected error occurred: {str(e)}", "error")
            return render_template("create_album.html")

    return render_template("create_album.html")

@app.route("/view_query_results", methods=["GET", "POST"])
def view_query_results():
    query = session.get('query')
    album_name = session.get('album_name')
    
    if not query or not album_name:
        flash("No active query session found. Please start over.", "warning")
        return redirect(url_for('select_directory'))
        
    target_directory = get_target_directory()
    dicom_files = load_dicom_files(target_directory)
    metadata_df = extract_metadata(dicom_files)
    subset_df = query_metadata(metadata_df, query)

    if subset_df.empty:
        flash("Query returned zero results. Please refine your filter.", "warning")
        return redirect(url_for('create_album'))

    if request.method == "POST":
        try:
            save_album_to_disk(subset_df, album_name)
            flash(f"Album '{album_name}' created successfully!", "success")
            # Clear session after successful creation
            session.pop('query', None)
            session.pop('album_name', None)
            return redirect(url_for('select_directory'))
        except Exception as e:
            flash(f"Failed to save album: {str(e)}", "error")

    return render_template("view_query_results.html", 
                           tables=[subset_df.to_html(classes='table table-hover table-striped')], 
                           titles=subset_df.columns.values,
                           album_name=album_name)

def save_album_to_disk(subset_df, album_name):
    album_directory = os.path.join(get_albums_directory(), album_name)
    if os.path.exists(album_directory):
        raise FileExistsError(f"Album '{album_name}' already exists. Please use a different name.")
    os.makedirs(album_directory)

    for index, row in subset_df.iterrows():
        dicom_file_path = row['FilePath']
        if pd.notna(dicom_file_path) and os.path.exists(dicom_file_path):
            shutil.copy(dicom_file_path, album_directory)

if __name__ == "__main__":
    app.run(debug=True)
