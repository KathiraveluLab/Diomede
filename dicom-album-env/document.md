### **DICOM Album Creator: User Guide**

This document provides a step-by-step guide on how to set up, use, and understand the **DICOM Album Creator** project. The project allows researchers to organize and query locally stored DICOM files, create albums (subsets of DICOM images), and share them via a web interface.

---

## **Table of Contents**
1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Setup Instructions](#setup-instructions)
4. [How It Works](#how-it-works)
5. [Using the Web Interface](#using-the-web-interface)
6. [Troubleshooting](#troubleshooting)
7. [Contributing](#contributing)

---

## **1. Overview**
The **DICOM Album Creator** is a Python-based utility that:
- Loads DICOM files from a local directory.
- Extracts metadata from the DICOM files.
- Allows querying of metadata to create subsets (albums) of DICOM images.
- Provides a web interface for easy interaction.
- Creates albums by copying matching DICOM files into a new folder.

---

## **2. Prerequisites**
Before using the project, ensure you have the following installed:
- **Python 3.7 or higher**
- **pip** (Python package manager)
- **Virtual Environment** (optional but recommended)

---

## **3. Setup Instructions**

### **Step 1: Clone the Repository**
If you have the project in a repository, clone it to your local machine:
```bash
git clone <repository_url>
cd <repository_folder>
```

### **Step 2: Set Up a Virtual Environment**
1. Create a virtual environment:
   ```bash
   python -m venv dicom-album-env
   ```
2. Activate the virtual environment:
   - On Linux/macOS:
     ```bash
     source dicom-album-env/bin/activate
     ```
   - On Windows:
     ```cmd
     dicom-album-env\Scripts\activate
     ```

### **Step 3: Install Dependencies**
Install the required Python packages:
```bash
pip install pydicom flask pandas
```

### **Step 4: Add DICOM Files**
1. Create a `data/dicom_files` folder in the root of your project:
   ```bash
   mkdir -p data/dicom_files
   ```
2. Place your DICOM files (`.dcm`) in the `data/dicom_files` folder.

   If you don’t have DICOM files, you can generate dummy files using the `create_dummy_dicom.py` script:
   ```bash
   python Scripts/create_dummy_dicom.py
   ```

---

## **4. How It Works**

### **Key Components**
1. **`load_dicom_files.py`**:
   - Loads DICOM files from a specified directory.
   - Skips non-DICOM files.

2. **`extract_metadata.py`**:
   - Extracts metadata (e.g., PatientID, StudyDate, Modality) from DICOM files.
   - Stores metadata in a Pandas DataFrame.

3. **`query_metadata.py`**:
   - Queries the metadata DataFrame to create subsets of DICOM files.

4. **`app.py`**:
   - Provides a Flask web interface for querying and creating albums.
   - Uses the `query_metadata` function to filter DICOM files.
   - Creates albums by copying matching DICOM files into a new folder.

5. **`create_dummy_dicom.py`**:
   - Generates dummy DICOM files for testing.

---

## **5. Using the Web Interface**

### **Step 1: Run the Flask App**
1. Activate your virtual environment (if not already activated):
   ```bash
   source dicom-album-env/bin/activate  # Linux/macOS
   ```
   or
   ```cmd
   dicom-album-env\Scripts\activate  # Windows
   ```
2. Run the Flask app:
   ```bash
   python app.py
   ```

### **Step 2: Access the Web Interface**
1. Open your browser and go to `http://127.0.0.1:5000/`.
2. You’ll see a form with two fields:
   - **Query**: Enter a query to filter DICOM files (e.g., `Modality == 'CT'`).
   - **Album Name**: Enter a name for the album (e.g., `CT_Studies`).

### **Step 3: Create an Album**
1. Enter a query and album name.
2. Click **Create Album**.
3. If the query matches any DICOM files:
   - A new folder will be created in the `albums` directory with the specified album name.
   - The matching DICOM files will be copied into this folder.
   - A success message will be displayed in your browser.

---

## **6. Troubleshooting**

### **Issue: No DICOM Files Loaded**
- **Cause**: The `data/dicom_files` folder is empty or contains non-DICOM files.
- **Solution**:
  1. Ensure the `data/dicom_files` folder contains valid DICOM files (`.dcm`).
  2. Use the `create_dummy_dicom.py` script to generate dummy files for testing.

### **Issue: `NameError: name 'dicom_files' is not defined`**
- **Cause**: The `dicom_files` variable is not defined before being used.
- **Solution**:
  1. Ensure `dicom_files` is created by calling `load_dicom_files` before using it in `extract_metadata`.
  2. Combine the scripts or use a main script to ensure proper execution order.

### **Issue: Flask App Fails to Start**
- **Cause**: Missing dependencies or incorrect paths.
- **Solution**:
  1. Ensure all dependencies are installed:
     ```bash
     pip install -r requirements.txt
     ```
  2. Verify the `dicom_directory` path in `app.py`.

---

## **7. Contributing**
Contributions to the project are welcome! Here’s how you can contribute:
1. **Report Issues**: Open an issue on GitHub if you encounter any bugs or have suggestions.
2. **Submit Pull Requests**: Fork the repository, make your changes, and submit a pull request.
3. **Improve Documentation**: Help improve this guide or add comments to the code.

---

## **Conclusion**
The **DICOM Album Creator** is a powerful tool for organizing and querying DICOM files. By following this guide, you can set up the project, use the web interface, and create albums of DICOM images. If you encounter any issues or have questions, feel free to reach out for assistance.

