from flask import Blueprint, jsonify, current_app, request

from .destinations import Destination

routing_bp = Blueprint('routing', __name__, url_prefix='/routing')

# Required fields and their expected Python types for POST
_REQUIRED_POST_FIELDS = {
    'name':     str,
    'ae_title': str,
    'host':     str,
    'port':     int,
}

# Optional fields that may appear in POST or PATCH with their types/constraints
_OPTIONAL_POST_FIELDS = {
    'priority':      int,
    'max_queue_size': int,
    'http_port':     int,
}

# Fields that PATCH is allowed to modify
_PATCHABLE_FIELDS = {
    'ae_title':      str,
    'host':          str,
    'port':          int,
    'priority':      int,
    'max_queue_size': int,
    'http_port':     int,
}


def get_router():
    return getattr(current_app, 'dicom_router', None)


def _dest_to_dict(dest, *, full=False):
    #Serialize a Destination object to a simple dictionary
    d = {
        'name':      dest.name,
        'ae_title':  dest.ae_title,
        'host':      dest.host,
        'port':      dest.port,
        'priority':  dest.priority,
        'status':    dest.status.value,
        'load':      dest.load_factor,
        'score':     dest.calculate_score(),
    }
    if full:
        d['current_queue']   = dest.current_queue
        d['max_queue_size']  = dest.max_queue_size
        d['http_port']       = dest.http_port
    return d


def _validate_int_positive(value, field):
    #Return (int_value, None) or (None, error_str)
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None, f"'{field}' must be an integer, got {value!r}"
    if v <= 0:
        return None, f"'{field}' must be a positive integer, got {v}"
    return v, None

@routing_bp.route('/stats', methods=['GET'])
def get_stats():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    stats = router.get_stats()

    # Ensure destinations in stats are JSON-serializable
    if isinstance(stats, dict) and 'destinations' in stats and stats['destinations'] is not None:
        try:
            destinations = stats['destinations']
            stats['destinations'] = [
                {
                    'name': d.name,
                    'ae_title': d.ae_title,
                    'host': d.host,
                    'port': d.port,
                    'priority': d.priority,
                    'status': d.status.value,
                    'load': getattr(d, 'load_factor', None),
                    'score': d.calculate_score() if hasattr(d, 'calculate_score') else None,
                }
                for d in destinations
            ]
        except Exception:
            # If serialization fails for any reason, fall back to omitting destinations
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


# POST /routing/destinations — add a new DICOM endpoint at runtime
@routing_bp.route('/destinations', methods=['POST'])
def add_destination():
    #Register a new DICOM destination endpoint without restarting the router.
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'error': 'Request body must be JSON'}), 400

    # validate required fields
    for field, expected_type in _REQUIRED_POST_FIELDS.items():
        if field not in body:
            return jsonify({'error': f"Missing required field: '{field}'"}), 400
        if expected_type is int:
            val, err = _validate_int_positive(body[field], field)
            if err:
                return jsonify({'error': err}), 400
        elif not isinstance(body[field], str) or not body[field].strip():
            return jsonify({'error': f"'{field}' must be a non-empty string"}), 400

    name = body['name'].strip()

    # reject duplicates
    if router.destination_manager.get_destination(name):
        return jsonify({'error': f"Destination '{name}' already exists"}), 409

    # validate optional fields
    kwargs = {}
    for field, _ in _OPTIONAL_POST_FIELDS.items():
        if field in body:
            val, err = _validate_int_positive(body[field], field)
            if err:
                return jsonify({'error': err}), 400
            kwargs[field] = val

    # Normalise the port (already validated above)
    port, _ = _validate_int_positive(body['port'], 'port')

    router.add_destination(
        name=name,
        ae_title=body['ae_title'].strip(),
        host=body['host'].strip(),
        port=port,
        priority=kwargs.get('priority', 1),
        max_queue=kwargs.get('max_queue_size', 100),
        http_port=kwargs.get('http_port', 8042),
    )

    dest = router.destination_manager.get_destination(name)
    return jsonify({
        'message': f"Destination '{name}' added successfully",
        'destination': _dest_to_dict(dest, full=True),
    }), 201


# DELETE /routing/destinations/<name> — remove a destination at runtime
@routing_bp.route('/destinations/<name>', methods=['DELETE'])
def remove_destination(name):
    """Unregister a DICOM destination endpoint at runtime."""
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    if not router.destination_manager.get_destination(name):
        return jsonify({'error': f"Destination '{name}' not found"}), 404

    router.destination_manager.remove_destination(name)
    return jsonify({'message': f"Destination '{name}' removed successfully"}), 200


# PATCH /routing/destinations/<name> — update priority / queue size live
@routing_bp.route('/destinations/<name>', methods=['PATCH'])
def update_destination(name):
    """Hot-update one or more fields of an existing destination without downtime."""
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503

    dest = router.destination_manager.get_destination(name)
    if not dest:
        return jsonify({'error': f"Destination '{name}' not found"}), 404

    body = request.get_json(silent=True)
    if not body:
        return jsonify({'error': 'Request body must be JSON'}), 400

    # Reject any unknown keys to surface typos early
    unknown = set(body.keys()) - set(_PATCHABLE_FIELDS.keys())
    if unknown:
        return jsonify({'error': f"Unknown field(s): {', '.join(sorted(unknown))}"}), 400

    if not body:
        return jsonify({'error': 'No fields provided to update'}), 400

    # Validate and collect updates
    updates = {}
    int_fields = {'port', 'priority', 'max_queue_size', 'http_port'}
    for field in body:
        if field in int_fields:
            val, err = _validate_int_positive(body[field], field)
            if err:
                return jsonify({'error': err}), 400
            updates[field] = val
        else:  # str fields: ae_title, host
            if not isinstance(body[field], str) or not body[field].strip():
                return jsonify({'error': f"'{field}' must be a non-empty string"}), 400
            updates[field] = body[field].strip()

    #apply updates atomically (the manager's Lock wraps attribute writes too)
    with router.destination_manager._lock:
        for field, value in updates.items():
            setattr(dest, field, value)

    return jsonify({
        'message': f"Destination '{name}' updated successfully",
        'destination': _dest_to_dict(dest, full=True),
    }), 200
