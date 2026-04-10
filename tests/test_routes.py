import json
import pytest
from unittest.mock import MagicMock

from flask import Flask
from Diomedex.routing.routes import routing_bp
from Diomedex.routing.destinations import DestinationStatus, Destination


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(routing_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dest(name='orthanc-a', ae_title='ORTHANC_A', host='127.0.0.1',
               port=4242, priority=1, current_queue=0, max_queue_size=100, http_port=8042):
    dest = MagicMock(spec=Destination)
    dest.name = name
    dest.ae_title = ae_title
    dest.host = host
    dest.port = port
    dest.priority = priority
    dest.status = DestinationStatus.HEALTHY
    dest.current_queue = current_queue
    dest.max_queue_size = max_queue_size
    dest.http_port = http_port
    dest.load_factor = current_queue / max_queue_size if max_queue_size else 0.0
    dest.calculate_score.return_value = 12.0
    return dest


def _make_router(dest=None, all_dests=None):
    """Return (router_mock, manager_mock) with sensible defaults."""
    router = MagicMock()
    manager = MagicMock()
    manager.get_destination.return_value = dest
    manager.get_all_destinations.return_value = all_dests or []
    manager.update_destination.return_value = True
    router.destination_manager = manager
    router.get_stats.return_value = {
        'total_received': 0, 'total_routed': 0, 'total_failed': 0,
        'destinations': [],
    }
    return router, manager


# ===========================================================================
# GET /routing/destinations/<name>
# ===========================================================================

class TestGetDestinationRoute:

    def test_get_destination_with_existing_destination(self, client, app):
        dest = MagicMock(spec=Destination)
        dest.name = 'test-dest'
        dest.ae_title = 'TEST_AE'
        dest.host = '192.168.1.1'
        dest.port = 104
        dest.priority = 1
        dest.status = DestinationStatus.HEALTHY
        dest.current_queue = 5
        dest.max_queue_size = 100
        dest.load_factor = 0.25
        dest.calculate_score.return_value = 95
        
        router = MagicMock()
        dest_manager = MagicMock()
        dest_manager.get_destination.return_value = dest
        router.destination_manager = dest_manager
        
        app.dicom_router = router
        response = client.get('/routing/destinations/test-dest')
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['name'] == 'test-dest'
        assert data['ae_title'] == 'TEST_AE'
        assert data['host'] == '192.168.1.1'
        assert data['port'] == 104
        assert data['priority'] == 1
        assert data['status'] == 'healthy'
        assert data['current_queue'] == 5
        assert data['max_queue_size'] == 100
        assert data['load'] == 0.25
        assert data['score'] == 95
        
        dest_manager.get_destination.assert_called_once_with('test-dest')

    def test_get_destination_not_found(self, client, app):
        router = MagicMock()
        dest_manager = MagicMock()
        dest_manager.get_destination.return_value = None
        router.destination_manager = dest_manager
        
        app.dicom_router = router
        response = client.get('/routing/destinations/nonexistent')
        
        assert response.status_code == 404
        data = response.get_json()
        assert 'error' in data
        assert 'not found' in data['error'].lower()
        
        dest_manager.get_destination.assert_called_once_with('nonexistent')

    def test_get_destination_router_unavailable(self, client, app):
        response = client.get('/routing/destinations/test-dest')
        
        assert response.status_code == 503
        data = response.get_json()
        assert data['error'] == 'Router not running'


# ===========================================================================
# GET /routing/destinations  (list)
# ===========================================================================

class TestListDestinationsRoute:

    def test_list_empty(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = client.get('/routing/destinations')
        assert rv.status_code == 200
        assert rv.get_json()['destinations'] == []

    def test_list_returns_destination(self, client, app):
        dest = _make_dest()
        router, _ = _make_router(all_dests=[dest])
        app.dicom_router = router
        data = client.get('/routing/destinations').get_json()
        assert len(data['destinations']) == 1
        assert data['destinations'][0]['name'] == 'orthanc-a'

    def test_list_response_shape(self, client, app):
        dest = _make_dest()
        router, _ = _make_router(all_dests=[dest])
        app.dicom_router = router
        item = client.get('/routing/destinations').get_json()['destinations'][0]
        for key in ('name', 'ae_title', 'host', 'port', 'priority', 'status', 'load', 'score'):
            assert key in item, f"missing key: {key}"

    def test_list_router_unavailable(self, client, app):
        rv = client.get('/routing/destinations')
        assert rv.status_code == 503


# ===========================================================================
# GET /routing/stats
# ===========================================================================

class TestStatsRoute:

    def test_stats_returns_200(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        assert client.get('/routing/stats').status_code == 200

    def test_stats_response_shape(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        data = client.get('/routing/stats').get_json()
        for key in ('total_received', 'total_routed', 'total_failed', 'destinations'):
            assert key in data, f"missing key: {key}"

    def test_stats_preserves_pre_serialized_destination_dicts(self, client, app):
        router, _ = _make_router()
        router.get_stats.return_value = {
            'total_received': 1,
            'total_routed': 1,
            'total_failed': 0,
            'destinations': [{
                'name': 'orthanc-a',
                'ae_title': 'ORTHANC_A',
                'host': '127.0.0.1',
                'port': 4242,
                'priority': 1,
                'status': 'healthy',
                'load': 0.0,
                'score': 12.0,
            }],
        }
        app.dicom_router = router

        data = client.get('/routing/stats').get_json()
        assert len(data['destinations']) == 1
        assert data['destinations'][0]['name'] == 'orthanc-a'

    def test_stats_router_unavailable(self, client, app):
        assert client.get('/routing/stats').status_code == 503


# ===========================================================================
# POST /routing/destinations
# ===========================================================================

class TestPostDestinationRoute:

    def _post(self, client, payload):
        return client.post(
            '/routing/destinations',
            data=json.dumps(payload),
            content_type='application/json',
        )

    _VALID = {'name': 'orthanc-a', 'ae_title': 'ORTHANC_A', 'host': '127.0.0.1', 'port': 4242}

    def test_add_destination_returns_201(self, client, app):
        dest = _make_dest()
        router, manager = _make_router()
        manager.get_destination.side_effect = [None, dest]  # no duplicate, then fetch after add
        app.dicom_router = router
        rv = self._post(client, self._VALID)
        assert rv.status_code == 201
        assert rv.get_json()['destination']['name'] == 'orthanc-a'
        router.add_destination.assert_called_once_with(
            name='orthanc-a',
            ae_title='ORTHANC_A',
            host='127.0.0.1',
            port=4242,
            priority=1,
            max_queue=100,
            http_port=8042,
        )

    def test_add_duplicate_returns_409(self, client, app):
        router, manager = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._post(client, self._VALID)
        assert rv.status_code == 409

    def test_missing_port_returns_400(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = self._post(client, {'name': 'x', 'ae_title': 'AE', 'host': 'h'})
        assert rv.status_code == 400

    def test_port_as_boolean_returns_400(self, client, app):
        router, manager = _make_router()
        manager.get_destination.return_value = None
        app.dicom_router = router
        rv = self._post(client, {**self._VALID, 'port': True})
        assert rv.status_code == 400

    def test_port_above_65535_returns_400(self, client, app):
        router, manager = _make_router()
        manager.get_destination.return_value = None
        app.dicom_router = router
        rv = self._post(client, {**self._VALID, 'port': 99999})
        assert rv.status_code == 400

    def test_http_port_above_65535_returns_400(self, client, app):
        router, manager = _make_router()
        manager.get_destination.return_value = None
        app.dicom_router = router
        rv = self._post(client, {**self._VALID, 'http_port': 99999})
        assert rv.status_code == 400

    def test_unsafe_name_returns_400(self, client, app):
        router, manager = _make_router()
        manager.get_destination.return_value = None
        app.dicom_router = router
        rv = self._post(client, {**self._VALID, 'name': 'a/b'})
        assert rv.status_code == 400

    def test_json_array_body_returns_400(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = client.post('/routing/destinations', data='[]', content_type='application/json')
        assert rv.status_code == 400

    def test_no_json_body_returns_400(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = client.post('/routing/destinations', data='bad', content_type='text/plain')
        assert rv.status_code == 400

    def test_router_unavailable_returns_503(self, client, app):
        rv = self._post(client, self._VALID)
        assert rv.status_code == 503


# ===========================================================================
# DELETE /routing/destinations/<name>
# ===========================================================================

class TestDeleteDestinationRoute:

    def test_delete_existing_returns_200(self, client, app):
        router, manager = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = client.delete('/routing/destinations/orthanc-a')
        assert rv.status_code == 200
        manager.remove_destination.assert_called_once_with('orthanc-a')

    def test_delete_nonexistent_returns_404(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = client.delete('/routing/destinations/ghost')
        assert rv.status_code == 404

    def test_router_unavailable_returns_503(self, client, app):
        rv = client.delete('/routing/destinations/anything')
        assert rv.status_code == 503


# ===========================================================================
# PATCH /routing/destinations/<name>
# ===========================================================================

class TestPatchDestinationRoute:

    def _patch(self, client, name, payload):
        return client.patch(
            f'/routing/destinations/{name}',
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_patch_destination_success(self, client, app):
        router, manager = _make_router(dest=_make_dest())
        app.dicom_router = router
        payload = {'priority': 5}
        rv = self._patch(client, 'orthanc-a', payload)
        assert rv.status_code == 200
        data = rv.get_json()
        assert 'updated successfully' in data['message']
        assert data['destination']['name'] == 'orthanc-a'
        manager.update_destination.assert_called_once_with('orthanc-a', payload)

    def test_patch_nonexistent_returns_404(self, client, app):
        router, _ = _make_router()
        app.dicom_router = router
        rv = self._patch(client, 'ghost', {'priority': 5})
        assert rv.status_code == 404

    def test_patch_unknown_field_returns_400(self, client, app):
        router, _ = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._patch(client, 'orthanc-a', {'bad_field': 'x'})
        assert rv.status_code == 400

    def test_patch_empty_body_returns_400(self, client, app):
        router, _ = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._patch(client, 'orthanc-a', {})
        assert rv.status_code == 400

    def test_patch_boolean_priority_returns_400(self, client, app):
        router, _ = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._patch(client, 'orthanc-a', {'priority': True})
        assert rv.status_code == 400

    def test_patch_port_above_65535_returns_400(self, client, app):
        router, _ = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._patch(client, 'orthanc-a', {'port': 99999})
        assert rv.status_code == 400

    def test_patch_http_port_above_65535_returns_400(self, client, app):
        router, _ = _make_router(dest=_make_dest())
        app.dicom_router = router
        rv = self._patch(client, 'orthanc-a', {'http_port': 99999})
        assert rv.status_code == 400

    def test_router_unavailable_returns_503(self, client, app):
        rv = self._patch(client, 'anything', {'priority': 1})
        assert rv.status_code == 503
