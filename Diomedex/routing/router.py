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
    
    def add_destination(self, name, ae_title, host, port, priority=1, max_queue=100, http_port=8042):
        destination = Destination(name, ae_title, host, port, priority, max_queue, http_port)
        self.destination_manager.add_destination(destination)
    
    def start_health_monitoring(self, interval=30):
        self.health_checker.check_interval = interval
        self.health_checker.start()
    
    def stop_health_monitoring(self):
        self.health_checker.stop()
    
    def route_dataset(self, dataset, metadata=None):
        """Route DICOM dataset to best available destination"""
        self.total_received += 1
        
        destination = self.destination_manager.select_and_reserve()
        if not destination:
            self.total_failed += 1
            return False
        
        try:
            # Queue slot and sent_count are already updated by select_and_reserve()
            
            # TODO: actual DICOM C-STORE with pynetdicom  
            # For now just track the routing decision
            success = True  # simulated
            
            if success:
                self.total_routed += 1
            else:
                # The attempt failed, so we need to correct the failure count.
                self.destination_manager.record_failure(destination.name)
                self.total_failed += 1
            
            return success
        except Exception as e:
            logger.exception('Failed to route dataset')
            # The attempt failed due to an exception. Correct the failure count.
            self.destination_manager.record_failure(destination.name)
            self.total_failed += 1
            return False
        finally:
            # Always decrement queue regardless of success/failure
            self.destination_manager.record_complete(destination.name)
    
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
