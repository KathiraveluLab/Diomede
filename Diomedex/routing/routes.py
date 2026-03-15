from flask import Blueprint, jsonify, current_app

routing_bp = Blueprint('routing', __name__, url_prefix='/routing')

def get_router():
    return getattr(current_app, 'dicom_router', None)

@routing_bp.route('/stats', methods=['GET'])
def get_stats():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503
    
    return jsonify(router.get_stats()), 200

@routing_bp.route('/destinations', methods=['GET'])
def list_destinations():
    router = get_router()
    if not router:
        return jsonify({'error': 'Router not running'}), 503
    
    stats = router.get_stats()
    return jsonify({'destinations': stats.get('destinations', [])}), 200

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
