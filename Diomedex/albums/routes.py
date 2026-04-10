from pathlib import Path
from flask import Blueprint, request, jsonify, current_app
from .core import DICOMAlbumCreator
from .kheops import KheopsAdapter
from .models import Album, db

albums_bp = Blueprint('albums', __name__)


@albums_bp.route('/scan', methods=['POST'])
def scan_directory():
    """Scan directory for DICOM files"""
    try:
        data = request.get_json()
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
        current_app.logger.error(f"Scan directory failed: {e}")
        return jsonify({'error': str(e)}), 500


@albums_bp.route('/create', methods=['POST'])
def create_album():
    """Create a new DICOM album"""
    try:
        data = request.get_json()
        if not data or 'name' not in data:
            return jsonify({'error': 'Name is required'}), 400

        storage_path = current_app.config.get('STORAGE_PATH')
        if not storage_path:
            current_app.logger.error("'STORAGE_PATH' is not configured.")
            return jsonify({'error': 'Server configuration error.'}), 500

        # Initialize Kheops adapter safely
        try:
            kheops = KheopsAdapter()
        except KeyError as e:
            current_app.logger.error(f'Kheops configuration missing: {e}')
            return jsonify({'error': 'Integration service is not configured.'}), 500

        # Create album in Kheops
        album = kheops.create_album(data['name'], data.get('description', ''))
        if not album:
            return jsonify({'error': 'Failed to create Kheops album'}), 500

        # Save locally
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
        current_app.logger.error(f"Create album failed: {e}")
        return jsonify({'error': str(e)}), 500
