import os
from pathlib import Path
from flask import Blueprint, request, jsonify, current_app
from .core import DICOMAlbumCreator
from .kheops import KheopsAdapter
from .models import Album, db
from .niffler_reader import load_niffler_csv, filter_metadata, to_album_index_format

albums_bp = Blueprint('albums', __name__)
_SCAN_PREVIEW_LIMIT = 10


@albums_bp.route('/scan', methods=['POST'])
def scan_directory():
    """Scan directory for DICOM files"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON body'}), 400

        path = data.get('path')
        if not isinstance(path, str) or not path.strip():
            return jsonify({'error': 'Path parameter is required and must be a non-empty string'}), 400

        # Get and validate configuration
        storage_path = current_app.config.get('STORAGE_PATH')
        if not storage_path:
            current_app.logger.error("'STORAGE_PATH' is not configured.")
            return jsonify({'error': 'Server configuration error.'}), 500

        user_path = Path(path)
        storage_base = Path(storage_path).resolve()

        # Ensure the user path is absolute and resolve it
        if not user_path.is_absolute():
            user_path = storage_base / user_path
        user_path = user_path.resolve()

        # Verify the resolved path is within storage_path
        # Security: Enforce strict path boundaries to prevent CWE-22 Path Traversal
        try:
            user_path.relative_to(storage_base)
        except ValueError:
            return jsonify({'error': 'Path must be within configured storage area'}), 403

        # Initialize creator with config from current_app
        creator = DICOMAlbumCreator(storage_path)
        files = creator.scan_directory(str(user_path))

        if creator.create_album_index(files):
            current_app.logger.info(
                "Album scan indexed %d files from %s",
                len(files),
                user_path,
            )
            return jsonify({
                'status': 'success',
                'file_count': len(files),
                'files': files[:_SCAN_PREVIEW_LIMIT]  # Return first 10 for preview
            })

        return jsonify({'error': 'Failed to index files'}), 500

    except Exception:
        current_app.logger.exception('Directory scan failed')
        return jsonify({'error': 'Internal server error'}), 500


@albums_bp.route('/create', methods=['POST'])
def create_album():
    """Create a new DICOM album"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON body'}), 400

        name = data.get('name')
        if not isinstance(name, str) or not name.strip():
            return jsonify({'error': 'Name is required and must be a non-empty string'}), 400
        if len(name) > 100:
            return jsonify({'error': 'Name must be 100 characters or less'}), 400

        description = data.get('description')
        if description is None:
            description = ''
        if not isinstance(description, str):
            return jsonify({'error': 'Description must be a string'}), 400

        # Initialize kheops adapter with error handling for missing config
        try:
            kheops = KheopsAdapter()
        except KeyError as e:
            current_app.logger.error(f'Kheops configuration missing: {e}')
            return jsonify({'error': 'Integration service is not configured.'}), 500

        # Create in Kheops
        album = kheops.create_album(name, description)
        if not album:
            current_app.logger.error(
                "Kheops album creation returned empty response for album=%s", 
                name
    )
            return jsonify({'error': 'Failed to create Kheops album'}), 500

        if not isinstance(album, dict):
            current_app.logger.error(
        "Kheops album creation returned non-dict response type=%s",
        type(album).__name__,
    )
            return jsonify({'error': 'Failed to create Kheops album'}), 500

        # Save to local database
        new_album = Album(
            name=name,
            description=description,
            kheops_id=album.get('album_id'),
            share_url=album.get('viewer_url')
        )
        db.session.add(new_album)
        db.session.commit()
        current_app.logger.info(
            "Created album id=%s name=%s kheops_id=%s",
            new_album.id, 
            new_album.name, 
            new_album.kheops_id)


        return jsonify({
            'status': 'success',
            'album': {
                'id': new_album.id,
                'name': new_album.name,
                'share_url': new_album.share_url
            }
        })

    except Exception:
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
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or 'csv_path' not in data:
            return jsonify({'error': 'Invalid request body, csv_path is required'}), 400

        csv_path = data.get('csv_path')
        if not isinstance(csv_path, str) or not csv_path.strip():
            return jsonify({'error': 'csv_path must be a non-empty string'}), 400

        storage_path = current_app.config.get('STORAGE_PATH')
        if not storage_path:
            current_app.logger.error("'STORAGE_PATH' is not configured.")
            return jsonify({'error': 'Server configuration error.'}), 500

        user_path = Path(csv_path)
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
        filters = data.get('filters')
        if filters:
            if not isinstance(filters, dict):
                return jsonify({'error': 'filters must be a dictionary'}), 400
            records = filter_metadata(records, filters)

        # Security: Validate that all paths in the CSV are within the storage area.
        # CHANGED: replaced p.resolve() inside loop (heavy disk I/O per file) with
        # os.path.normpath + os.path.commonpath — a pure string-based check with no
        # filesystem calls, safe for 10,000+ row CSVs without timing out.
        storage_base_str = str(storage_base)
        valid_files = []
        for f in to_album_index_format(records):
            try:
                p = f['path']
                if not Path(p).is_absolute():
                    p = os.path.join(storage_base_str, p)
                p = os.path.normpath(p)
                if os.path.commonpath([p, storage_base_str]) == storage_base_str:
                    f['path'] = p
                    valid_files.append(f)
                else:
                    current_app.logger.warning(f"Skipping file outside storage area: {f['path']}")
            except (ValueError, TypeError):
                current_app.logger.warning(f"Skipping file with invalid path: {f['path']}")

        creator = DICOMAlbumCreator(storage_path)
        if creator.create_album_index(valid_files):
            current_app.logger.info(
                "Indexed %d files from Niffler CSV %s",
                len(valid_files),
                user_path,
)
            return jsonify({
                'status': 'success',
                'indexed': len(valid_files)  # len() works fine — valid_files is a list
            })
        return jsonify({'error': 'Failed to index files'}), 500

    except FileNotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unhandled error in /index-from-niffler")
        return jsonify({'error': 'An internal server error occurred'}), 500
