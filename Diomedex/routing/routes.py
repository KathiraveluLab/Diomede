import re
from .destinations import Destination
from flask import Blueprint, jsonify, current_app, request

routing_bp = Blueprint('routing', __name__, url_prefix='/routing')

# Required fields and their expected Python types for POST
_REQUIRED_POST_FIELDS = {
    'name': str,
    'ae_title': str,
    'host': str,
    'port': int,
}

# Optional fields that may appear in POST or PATCH with their types/constraints
_OPTIONAL_POST_FIELDS = {
    'priority': int,
    'max_queue_size': int,
    'http_port': int,
}

# Fields that PATCH is allowed to modify
_PATCHABLE_FIELDS = {
    'ae_title': str,
    'host': str,
    'port': int,
    'priority': int,
    'max_queue_size': int,
    'http_port': int,
}

_DEST_ALLOWED_FIELDS = set(_PATCHABLE_FIELDS) | {'name'}


def get_router():
    return getattr(current_app, 'dicom_router', None)


def _dest_to_dict(dest, *, full=False):
    # Serialize a Destination object to a simple dictionary
    d = {
        'name': dest.name,
        'ae_title': dest.ae_title,
        'host': dest.host,
        'port': dest.port,
        'priority': dest.priority,
        'status': dest.status.value,
        'load': dest.load_factor,
        'score': dest.calculate_score(),
    }
    if full:
        d['current_queue'] = dest.current_queue
        d['max_queue_size'] = dest.max_queue_size
        d['http_port'] = dest.http_port
    return d


def _validate_int_positive(value, field):
    if not isinstance(value, int) or isinstance(value, bool):
        return None, f"'{field}' must be an integer, got {value!r}"
    if value < 0:
        return None, f"'{field}' must be a positive integer, got {value}"
    return value, None


def _validate_port(value):
    v, err = _validate_int_positive(value, 'port')
    if err:
        return None, err
    if v > 65535:
        return None, f"'port' must be between 1 and 65535, got {v}"
    return v, None


def _validate_optional_port(value, field):
    v, err = _validate_int_positive(value, field)
    if err:
        return None, err
    if v > 65535:
        return None, f"'{field}' must be between 1 and 65535, got {v}"
    return v, None


def _serialize_destination_stats(destinations):
    """Normalize stats destinations to JSON-safe dictionaries."""
    serialized = []
    for d in destinations:
        if isinstance(d, dict):
            serialized.append(d)
            continue
        try:
            serialized.append({
                'name': d.name,
                'ae_title': d.ae_title,
                'host': d.host,
                'port': d.port,
                'priority': d.priority,
                'status': d.status.value,
                'load': getattr(d, 'load_factor', None),
                'score': d.calculate_score() if hasattr(d, 'calculate_score') else None,
            })
        except Exception as e:
            current_app.logger.exception(f"Failed to serialize destination object: {e}")
    return serialized


def validate_destination_config(config: dict):
    if not isinstance(config, dict):
        raise ValueError('Request JSON must be an object')

    unknown = set(config) - _DEST_ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(sorted(unknown))}")

    for field in ('name', 'ae_title', 'host', 'port'):
        if field not in config:
            raise ValueError(f"Missing required field: '{field}'")

    for field in ('name', 'ae_title', 'host'):
        if not isinstance(config[field], str) or not config[field].strip():
            raise ValueError(f"'{field}' must be a non-empty string")

    port, err = _validate_port(config['port'])
    if err:
        raise ValueError(err)

    return {
        **config,
        'name': config['name'].strip(),
        'ae_title': config['ae_title'].strip(),
        'host': config['host'].strip(),
        'port': port
    }


@routing_bp.route('/stats', methods=['GET'])
def get_stats():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    stats = router.get_stats()

    # Ensure destinations in stats are JSON-serializable
    if isinstance(stats, dict) and 'destinations' in stats and stats['destinations'] is not None:
        try:
            stats['destinations'] = _serialize_destination_stats(stats['destinations'])
        except Exception as e:
            current_app.logger.exception(f"Failed to serialize destinations list: {e}")
            stats['destinations'] = []

    return jsonify(stats), 200


@routing_bp.route('/destinations', methods=['GET'])
def list_destinations():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    destinations = router.destination_manager.get_all_destinations()
    return jsonify({
        'destinations': [
            {
                'name': d.name,
                'ae_title': d.ae_title,
                'host': d.host,
                'port': d.port,
                'priority': d.priority,
                'status': d.status.value,
                'load': d.load_factor,
                'score': d.calculate_score()
            }
            for d in destinations
        ]
    }), 200


@routing_bp.route('/destinations/<name>', methods=['GET'])
def get_destination(name):
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    dest = router.destination_manager.get_destination(name)
    if not dest:
        return jsonify({'error': f'Destination {name} not found'}), 404

    return jsonify({
        'name': dest.name,
        'ae_title': dest.ae_title,
        'host': dest.host,
        'port': dest.port,
        'priority': dest.priority,
        'status': dest.status.value,
        'current_queue': dest.current_queue,
        'max_queue_size': dest.max_queue_size,
        'load': dest.load_factor,
        'score': dest.calculate_score()
    }), 200


@routing_bp.route('/destinations', methods=['POST'])
def add_destination():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({'error': 'Request body must be JSON'}), 400

    try:
        body = validate_destination_config(body)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    name = body['name']

    if not re.match(r'^[A-Za-z0-9_\-\.]+$', name):
        return jsonify({'error': "'name' must contain only letters, digits, hyphens, underscores, or dots"}), 400

    if router.destination_manager.get_destination(name):
        return jsonify({'error': f"Destination '{name}' already exists"}), 409

    kwargs = {}
    for field in _OPTIONAL_POST_FIELDS:
        if field in body:
            validator = _validate_optional_port if field == 'http_port' else _validate_int_positive
            val, err = validator(body[field], field)
            if err:
                return jsonify({'error': err}), 400
            kwargs[field] = val

    router.add_destination(
        name=name,
        ae_title=body['ae_title'],
        host=body['host'],
        port=body['port'],
        priority=kwargs.get('priority', 1),
        max_queue=kwargs.get('max_queue_size', 100),
        http_port=kwargs.get('http_port', 8042),
    )

    dest = router.destination_manager.get_destination(name)
    return jsonify({
        'message': f"Destination '{name}' added successfully",
        'destination': _dest_to_dict(dest, full=True),
    }), 201


@routing_bp.route('/destinations/<name>', methods=['DELETE'])
def remove_destination(name):
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    if not router.destination_manager.get_destination(name):
        return jsonify({'error': f"Destination '{name}' not found"}), 404

    router.destination_manager.remove_destination(name)
    return jsonify({'message': f"Destination '{name}' removed successfully"}), 200


@routing_bp.route('/destinations/<name>', methods=['PATCH'])
def update_destination(name):
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    dest = router.destination_manager.get_destination(name)
    if not dest:
        return jsonify({'error': f"Destination '{name}' not found"}), 404

    body = request.get_json(silent=True)
    if body is None:
        return jsonify({'error': 'Request body must be JSON'}), 400
    if not isinstance(body, dict):
        return jsonify({'error': 'Request JSON must be an object'}), 400
    if not body:
        return jsonify({'error': 'No fields provided to update'}), 400

    candidate = {
        'name': dest.name,
        'ae_title': dest.ae_title,
        'host': dest.host,
        'port': dest.port,
        **body
    }

    try:
        validated = validate_destination_config(candidate)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    updates = {}

    for field in body:
        if field not in _PATCHABLE_FIELDS:
            continue

        if field in ('ae_title', 'host', 'port'):
            updates[field] = validated[field]

        elif _PATCHABLE_FIELDS[field] is int:
            validator = _validate_optional_port if field == 'http_port' else _validate_int_positive
            val, err = validator(body[field], field)
            if err:
                return jsonify({'error': err}), 400
            updates[field] = val

        else:
            if not isinstance(body[field], str) or not body[field].strip():
                return jsonify({'error': f"'{field}' must be a non-empty string"}), 400
            updates[field] = body[field].strip()

    if not router.destination_manager.update_destination(name, updates):
        return jsonify({'error': f"Destination '{name}' was removed concurrently"}), 404

    return jsonify({
        'message': f"Destination '{name}' updated successfully",
        'destination': _dest_to_dict(dest, full=True),
    }), 200
