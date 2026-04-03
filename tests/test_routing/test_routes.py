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

