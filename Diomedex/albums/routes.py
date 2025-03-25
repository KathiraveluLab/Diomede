from flask import Blueprint, request, jsonify
from .core import DICOMAlbumCreator
from .kheops import KheopsAdapter
from .models import Album, DICOMFile, db
from .. import csrf

albums_bp = Blueprint('albums', __name__)
creator = DICOMAlbumCreator(current_app.config['STORAGE_PATH'])
kheops = KheopsAdapter()

@albums_bp.route('/scan', methods=['POST'])
@csrf.exempt
def scan_directory():
    """Scan directory for DICOM files"""
    try:
        data = request.get_json()
        if not data or 'path' not in data:
            return jsonify({'error': 'Path parameter is required'}), 400
            
        files = creator.scan_directory(data['path'])
        if creator.create_album_index(files):
            return jsonify({
                'status': 'success',
                'file_count': len(files),
                'files': files[:10]  # Return first 10 for preview
            })
        return jsonify({'error': 'Failed to index files'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@albums_bp.route('/create', methods=['POST'])
def create_album():
    """Create a new DICOM album"""
    try:
        data = request.get_json()
        if not data or 'name' not in data:
            return jsonify({'error': 'Name is required'}), 400
            
        # Create in Kheops
        album = kheops.create_album(data['name'], data.get('description', ''))
        if not album:
            return jsonify({'error': 'Failed to create Kheops album'}), 500
            
        # Save to local database
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
        return jsonify({'error': str(e)}), 500