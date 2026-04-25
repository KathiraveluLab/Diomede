import time
import requests
import logging
from threading import Thread, Event
from .destinations import DestinationManager, DestinationStatus

logger = logging.getLogger(__name__)

class HealthChecker:
    def __init__(self, destination_manager, check_interval=30, request_timeout=5):
        self.destination_manager = destination_manager
        self.check_interval = check_interval
        self.request_timeout = request_timeout
        self.running = False
        self._stop_event = Event()
        self._thread = None
    
    def start(self):
        if self.running:
            return
        
        self.running = True
        self._stop_event.clear()
        self._thread = Thread(target=self._check_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        if not self.running:
            return
        
        self.running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
    
    def _check_loop(self):
        while not self._stop_event.is_set():
            try:
                self.check_all()
            except Exception:
                logger.exception('Unhandled exception in health check loop')
            
            self._stop_event.wait(self.check_interval)
    
    def check_all(self):
        destinations = self.destination_manager.get_all_destinations()
        for dest in destinations:
            try:
                self.check_destination(dest.name)
            except Exception:
                logger.exception(f'Failed to check destination {dest.name}')
    
    def check_destination(self, name):
        dest = self.destination_manager.get_destination(name)
        if not dest:
            return False
        
        start = time.perf_counter()
        
        try:
            # Try HTTP health check for Orthanc
            # TODO: use pynetdicom C-ECHO for real DICOM verification
            # Note: Orthanc HTTP API typically on different port (e.g., 8042) than DICOM port
            url = f"http://{dest.host}:{dest.http_port}/system"
            response = requests.get(url, timeout=self.request_timeout)
            response_time = time.perf_counter() - start
            
            if response.status_code == 200:
                self.destination_manager.update_status(
                    name,
                    DestinationStatus.HEALTHY,
                    response_time=response_time
                )
                return True
            else:
                logger.warning(
                    "Health check degraded for %s: HTTP %s (took %.3fs)",
                    name,
                    response.status_code,
                    response_time,
                )
                self.destination_manager.update_status(name, DestinationStatus.DEGRADED, response_time=response_time)
                return False
                
        except requests.Timeout:
            logger.warning(
                "Health check timeout for %s after %ss",
                name,
                self.request_timeout,
            )
            self.destination_manager.update_status(name, DestinationStatus.DEGRADED)
            return False

        except requests.RequestException as e:
            logger.error(
                "Health check failed for %s: %s",
                name,
                e,
            )
            self.destination_manager.update_status(name, DestinationStatus.DEGRADED)
            return False
            
        except Exception:
            logger.exception("Health check unavailable for %s", name)
            self.destination_manager.update_status(name, DestinationStatus.UNAVAILABLE)
            return False
