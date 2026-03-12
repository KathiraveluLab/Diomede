from enum import Enum
import time
import logging

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
    def __init__(self, name, ae_title, host, port, priority=1, max_queue=100):
        self.name = name
        self.ae_title = ae_title
        self.host = host
        self.port = port
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
    
    def add_destination(self, destination):
        self.destinations[destination.name] = destination
    
    def remove_destination(self, name):
        if name in self.destinations:
            del self.destinations[name]
    
    def get_destination(self, name):
        return self.destinations.get(name)
    
    def get_all_destinations(self):
        return list(self.destinations.values())
    
    def get_available(self):
        return [d for d in self.destinations.values() if d.is_available]
    
    def select_best(self):
        available = self.get_available()
        if not available:
            return None
        
        available.sort(key=lambda d: d.calculate_score(), reverse=True)
        return available[0]
    
    def update_status(self, name, status, queue=None, response_time=None):
        dest = self.get_destination(name)
        if not dest:
            return
        
        dest.status = status
        dest.last_check = time.time()
        
        if queue is not None:
            dest.current_queue = queue
        if response_time is not None:
            dest.response_time = response_time
    
    def record_send(self, name, success=True):
        dest = self.get_destination(name)
        if not dest:
            return
        
        dest.sent_count += 1
        if not success:
            dest.failed_count += 1
    
    def record_complete(self, name):
        dest = self.get_destination(name)
        if dest and dest.current_queue > 0:
            dest.current_queue -= 1
