from enum import Enum
import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)

# Scoring weights
PRIORITY_WEIGHT = 10
LOAD_WEIGHT = 5
RESPONSE_TIME_WEIGHT = 2

class DestinationStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

class Destination:
    def __init__(self, name, ae_title, host, port, priority=1, max_queue=100, http_port=8042):
        self.name = name
        self.ae_title = ae_title
        self.host = host
        self.port = port
        self.http_port = http_port
        self.priority = priority
        self.max_queue_size = max_queue
        self.status = DestinationStatus.HEALTHY
        self.current_queue = 0
        self.last_check = time.time()
        self.response_time = 0.0
        self.sent_count = 0
        self.failed_count = 0
    
    @property
    def load_factor(self):
        if self.max_queue_size == 0:
            return 0.0
        return self.current_queue / self.max_queue_size
    
    @property
    def is_available(self):
        return self.status == DestinationStatus.HEALTHY and self.current_queue < self.max_queue_size
    
    def calculate_score(self):
        if not self.is_available:
            return 0.0
        
        load_score = 1.0 - self.load_factor
        response_score = 1.0 / (1.0 + self.response_time) if self.response_time > 0 else 1.0
        
        score = (self.priority * PRIORITY_WEIGHT + 
                 load_score * LOAD_WEIGHT + 
                 response_score * RESPONSE_TIME_WEIGHT)
        return score

class DestinationManager:
    def __init__(self):
        self.destinations = {}
        self._lock = Lock()
    
    def add_destination(self, destination):
        with self._lock:
            self.destinations[destination.name] = destination
    
    def remove_destination(self, name):
        with self._lock:
            if name in self.destinations:
                del self.destinations[name]
    
    def get_destination(self, name):
        with self._lock:
            return self.destinations.get(name)
    
    def get_all_destinations(self):
        with self._lock:
            return list(self.destinations.values())
    
    def get_available(self):
        with self._lock:
            return [d for d in self.destinations.values() if d.is_available]
    
    def select_best(self):
        with self._lock:
            available = [d for d in self.destinations.values() if d.is_available]
            if not available:
                return None
            
            return max(available, key=lambda d: d.calculate_score())
    
    def update_status(self, name, status, queue=None, response_time=None):
        with self._lock:
            dest = self.destinations.get(name)
            if not dest:
                return
            
            dest.status = status
            dest.last_check = time.time()
            
            if queue is not None:
                dest.current_queue = queue
            if response_time is not None:
                dest.response_time = response_time
    
    def record_send(self, name):
        with self._lock:
            dest = self.destinations.get(name)
            if not dest:
                return
            
            dest.current_queue += 1
            dest.sent_count += 1
    
    def record_failure(self, name):
        with self._lock:
            dest = self.destinations.get(name)
            if dest:
                dest.failed_count += 1
    
    def record_complete(self, name):
        with self._lock:
            dest = self.destinations.get(name)
            if dest and dest.current_queue > 0:
                dest.current_queue -= 1

    def update_destination(self, name, updates: dict) -> bool:
        _ALLOWED = {'ae_title', 'host', 'port', 'priority', 'max_queue_size', 'http_port'}

        def _is_valid_update(field, value):
            if field in {'ae_title', 'host'}:
                return isinstance(value, str) and bool(value.strip())
            
            # Ensure value is an int and not a bool
            if not isinstance(value, int) or isinstance(value, bool):
                return False

            if field in {'port', 'http_port'}:
                return 1 <= value <= 65535
            if field == 'priority':
                return value >= 0
            if field == 'max_queue_size':
                return value > 0
            return False
                return value > 0
            return False

        with self._lock:
            dest = self.destinations.get(name)
            if not dest:
                return False
            to_apply = {}
            for field, value in updates.items():
                if field in _ALLOWED:
                    if not _is_valid_update(field, value):
                        logger.warning(
                            "Rejected invalid destination update for %s: %s=%r",
                            name,
                            field,
                            value,
                        )
                        return False
                    to_apply[field] = value
            for field, value in to_apply.items():
                setattr(dest, field, value)
            return True
