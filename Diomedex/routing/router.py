from .destinations import DestinationManager, Destination
from .health import HealthChecker
import logging

logger = logging.getLogger(__name__)

class DICOMRouter:
    def __init__(self, ae_title="DIOMEDE_ROUTER", port=11112):
        self.ae_title = ae_title
        self.port = port
        self.destination_manager = DestinationManager()
        self.health_checker = HealthChecker(self.destination_manager)
        self.running = False
        
        self.total_received = 0
        self.total_routed = 0
        self.total_failed = 0
    
    def add_destination(self, name, ae_title, host, port, priority=1, max_queue=100):
        destination = Destination(name, ae_title, host, port, priority, max_queue)
        self.destination_manager.add_destination(destination)
    
    def start_health_monitoring(self, interval=30):
        self.health_checker.check_interval = interval
        self.health_checker.start()
    
    def stop_health_monitoring(self):
        self.health_checker.stop()
    
    def route_dataset(self, dataset, metadata=None):
        """Route DICOM dataset to best available destination"""
        self.total_received += 1
        
        try:
            destination = self.destination_manager.select_best()
            if not destination:
                self.total_failed += 1
                return False
            
            # TODO: actual DICOM C-STORE with pynetdicom  
            # For now just track the routing decision
            success = True  # simulated
            
            if success:
                self.destination_manager.record_send(destination.name, success=True)
                self.destination_manager.record_complete(destination.name)
                self.total_routed += 1
                return True
            else:
                self.destination_manager.record_send(destination.name, success=False)
                self.total_failed += 1
                return False
                
        except Exception as e:
            logger.exception('Failed to route dataset')
            self.total_failed += 1
            return False
    
    def get_stats(self):
        destinations = self.destination_manager.get_all_destinations()
        return {
            'total_received': self.total_received,
            'total_routed': self.total_routed,
            'total_failed': self.total_failed,
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
        }
