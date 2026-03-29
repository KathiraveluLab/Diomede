from flask import Blueprint, request, jsonify, send_file
import pydicom
import io
import os
from ..utils.anonymize import anonymize_dicom_file_in_memory, anonymize_dicom_file_on_disk

anonymization_bp = Blueprint('anonymization', __name__, url_prefix='/anonymization')

@anonymization_bp.route('/file', methods=['POST'])
def anonymize_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    try:
        # Anonymize in memory
        anon_ds = anonymize_dicom_file_in_memory(file)
        
        # Save to byte stream to send back
        output_stream = io.BytesIO()
        anon_ds.save_as(output_stream)
        output_stream.seek(0)
        
        return send_file(
            output_stream,
            as_attachment=True,
            download_name=f"anon_{file.filename}",
            mimetype='application/dicom'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@anonymization_bp.route('/directory', methods=['POST'])
def anonymize_directory():
    data = request.json
    if not data or 'directory_path' not in data:
        return jsonify({'error': 'No directory_path provided'}), 400
        
    directory_path = data['directory_path']
    if not os.path.exists(directory_path):
        return jsonify({'error': 'Directory does not exist'}), 404
        
    # Anonymize in place or to a new directory
    output_dir = data.get('output_dir', os.path.join(directory_path, 'anonymized'))
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    success_count = 0
    errors = []
    
    for root, _, files in os.walk(directory_path):
        # Prevent recursing into output_dir if it's a subfolder
        if output_dir in root:
            continue
            
        for file in files:
            input_path = os.path.join(root, file)
            output_path = os.path.join(output_dir, file)
            
            # Simple soft check if it looks like DICOM
            if input_path.endswith('.dcm') or '.' not in file:
                success, msg = anonymize_dicom_file_on_disk(input_path, output_path)
                if success:
                    success_count += 1
                else:
                    # Could error natively if it isn't actually a DICOM file
                    pass
                    
    return jsonify({
        'anonymized_count': success_count,
        'errors': errors,
        'output_directory': output_dir
    })
