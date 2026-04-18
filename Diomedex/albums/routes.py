from pathlib import Path
from flask import Blueprint, request, jsonify, current_app
from .core import DICOMAlbumCreator
from .kheops import KheopsAdapter
from .models import Album, DICOMFile, db

albums_bp = Blueprint('albums', __name__)

@albums_bp.route('/scan', methods=['POST'])
def scan_directory():
    """Scan directory for DICOM files"""
    try:
        data = request.get_json(silent=True)
        if not data or 'path' not in data:
            return jsonify({'error': 'Path parameter is required'}), 400
        
        storage_path = current_app.config.get('STORAGE_PATH')
        if not storage_path:
            current_app.logger.error("'STORAGE_PATH' is not configured.")
            return jsonify({'error': 'Server configuration error.'}), 500
        
        user_path = Path(data['path'])
        storage_base = Path(storage_path).resolve()
        
        if not user_path.is_absolute():
            user_path = storage_base / user_path
        user_path = user_path.resolve()
        
        # Security: Enforce strict path boundaries to prevent CWE-22 Path Traversal
        try:
            user_path.relative_to(storage_base)
        except ValueError:
            return jsonify({'error': 'Path must be within configured storage area'}), 403
        
        creator = DICOMAlbumCreator(storage_path)
        files = creator.scan_directory(str(user_path))
        if creator.create_album_index(files):
            return jsonify({
                'status': 'success',
                'file_count': len(files),
                'files': files[:10]
            })
        return jsonify({'error': 'Failed to index files'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@albums_bp.route('/create', methods=['POST'])
def create_album():
    """Create a new DICOM album"""
    try:
        data = request.get_json(silent=True)
        if not data or 'name' not in data:
            return jsonify({'error': 'Name is required'}), 400
        
        try:
            kheops = KheopsAdapter()
        except KeyError as e:
            current_app.logger.error(f'Kheops configuration missing: {e}')
            return jsonify({'error': 'Integration service is not configured.'}), 500

        album = kheops.create_album(data['name'], data.get('description', ''))
        if not album:
            return jsonify({'error': 'Failed to create Kheops album'}), 500
            
        new_album = Album(
            name=data['name'],
            description=data.get('description', ''),
            kheops_id=album.get('album_id'),
            share_url=album.get('viewer_url')
        )
        db.session.add(new_album)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'album': {
                'id': new_album.id,
                'name': new_album.name,
                'share_url': new_album.share_url
            }
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Unhandled error in /create")
        return jsonify({'error': 'An internal server error occurred'}), 500
     

@albums_bp.route('/index-from-niffler', methods=['POST'])
def index_from_niffler():
    """
    Index DICOM files into the album database using a Niffler CSV output file.
    
    Instead of scanning raw DICOM files (which reimplements Niffler),
    this route accepts the path to a CSV already produced by Niffler's
    meta-extraction module and feeds it into the existing album index pipeline.
    
    Request body:
        csv_path  (str, required): path to Niffler's output CSV file
        filters   (dict, optional): e.g. {"Modality": "CT"} to index a subset
    Returns:
        JSON with status and count of files indexed.
    """
    try:
        from .niffler_reader import load_niffler_csv, filter_metadata, to_album_index_format

        data = request.get_json(silent=True)
        if not data or 'csv_path' not in data:
            return jsonify({'error': 'csv_path is required'}), 400

        storage_path = current_app.config.get('STORAGE_PATH')
        if not storage_path:
            return jsonify({'error': 'Server configuration error.'}), 500

        user_path = Path(data['csv_path'])
        storage_base = Path(storage_path).resolve()
        
        if not user_path.is_absolute():
            user_path = storage_base / user_path
        user_path = user_path.resolve()
        
        # Security: Enforce strict path boundaries to prevent CWE-22 Path Traversal
        try:
            user_path.relative_to(storage_base)
        except ValueError:
            return jsonify({'error': 'Path must be within configured storage area'}), 403
            
        records = load_niffler_csv(str(user_path))
        if data.get('filters'):
            records = filter_metadata(records, data['filters'])

        files = to_album_index_format(records)

        creator = DICOMAlbumCreator(storage_path)
        if creator.create_album_index(files):
            return jsonify({
                'status': 'success',
                'indexed': len(files)
            })
        return jsonify({'error': 'Failed to index files'}), 500

    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        current_app.logger.exception("Unhandled error in /index-from-niffler")
        return jsonify({'error': 'An internal server error occurred'}), 500
