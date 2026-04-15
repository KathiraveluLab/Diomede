import time
import requests
import logging
from threading import Thread, Event, Lock  

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
        self._lifecycle_lock = Lock()  
    
    def start(self):
        with self._lifecycle_lock:
            # Prevent duplicate or zombie threads
            if self._thread and self._thread.is_alive():
                return
            
            self._stop_event = Event()

            self._thread = Thread(target=self._check_loop, daemon=True)
            self._thread.start()
            self.running = True
    
    def stop(self):
        with self._lifecycle_lock:
            if not self.running and not (self._thread and self._thread.is_alive()):
                return
            
            self.running = False

            # signal current thread to stop
            self._stop_event.set()

            thread = self._thread
        
        # join outside lock
        if thread:
            thread.join(timeout=5)
    
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
        
        start = time.time()
        
        try:
            # Try HTTP health check for Orthanc
            # TODO: use pynetdicom C-ECHO for real DICOM verification
            # Note: Orthanc HTTP API typically on different port (e.g., 8042) than DICOM port
            url = f"http://{dest.host}:{dest.http_port}/system"
            response = requests.get(url, timeout=self.request_timeout)
            response_time = time.time() - start
            
            if response.status_code == 200:
                self.destination_manager.update_status(
                    name,
                    DestinationStatus.HEALTHY,
                    response_time=response_time
                )
                return True
            else:
                self.destination_manager.update_status(name, DestinationStatus.DEGRADED)
                return False
                
        except requests.Timeout:
            self.destination_manager.update_status(name, DestinationStatus.DEGRADED)
            return False
            
        except Exception:
            self.destination_manager.update_status(name, DestinationStatus.UNAVAILABLE)
            return False
