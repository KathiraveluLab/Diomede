# Dynamic DICOM Routing

Experimental module for routing DICOM images to multiple PACS/Orthanc destinations based on system load.

## Concept

Instead of static endpoints, the router acts as a middleman:

```
Scanner → Diomede Router → Best Available PACS/Orthanc
```

The router selects destinations based on:
- Priority level
- Current queue load
- System health/availability
- Response time

## Basic Usage

```python
from Diomedex.routing import DICOMRouter

router = DICOMRouter()

# Add destinations
router.add_destination("primary", "ORTHANC1", "localhost", 4242, priority=10)
router.add_destination("backup", "ORTHANC2", "localhost", 4243, priority=5)

# Health monitoring
router.start_health_monitoring(interval=30)

# Route a dataset
router.route_dataset(dicom_dataset)
```

## REST API

- `GET /routing/stats` - View routing statistics
- `GET /routing/destinations` - List all destinations
- `GET /routing/destinations/<name>` - Get specific destination info

### `POST /routing/destinations`

Registers a new DICOM destination endpoint dynamically without requiring a service restart.

**Request Body (`application/json`):**

```json
{
  "name": "destination_name",    // Required: string (letters, digits, hyphens, underscores, dots only)
  "ae_title": "AE_TITLE",        // Required: string
  "host": "192.168.1.10",        // Required: string
  "port": 104,                   // Required: integer (1-65535)
  "priority": 1,                 // Optional: positive integer
  "max_queue_size": 100,         // Optional: positive integer
  "http_port": 8042              // Optional: positive integer
}
```

**Response (`201 Created`):**

```json
{
  "message": "Destination 'destination_name' added successfully",
  "destination": {
    "name": "destination_name",
    "ae_title": "AE_TITLE",
    "host": "192.168.1.10",
    "port": 104,
    "priority": 1,
    "status": "healthy",
    "load": 0.0,
    "score": 17.0,
    "current_queue": 0,
    "max_queue_size": 100,
    "http_port": 8042
  }
}
```

### `DELETE /routing/destinations/<name>`

Unregisters and removes a specified DICOM destination endpoint.

**Response (`200 OK`):**

```json
{
  "message": "Destination '<name>' removed successfully"
}
```

### `PATCH /routing/destinations/<name>`

Modifies the configuration of an existing DICOM destination endpoint dynamically.

**Request Body (`application/json`):**

Include any subset of the following modifiable parameters:

```json
{
  "ae_title": "NEW_AE_TITLE",   // Optional: string
  "host": "192.168.1.11",       // Optional: string
  "port": 105,                  // Optional: integer (1-65535)
  "priority": 5,                // Optional: positive integer
  "max_queue_size": 250,        // Optional: positive integer
  "http_port": 8043             // Optional: positive integer
}
```

**Response (`200 OK`):**

```json
{
  "message": "Destination '<name>' updated successfully",
  "destination": {
    "name": "<name>",
    "ae_title": "NEW_AE_TITLE",
    "host": "192.168.1.11",
    "port": 105,
    "priority": 5,
    "status": "healthy",
    "load": 0.0,
    "score": 57.0,
    "current_queue": 0,
    "max_queue_size": 250,
    "http_port": 8043
  }
}
```

## Current Status

This is a proof-of-concept implementation. Full DICOM C-STORE functionality requires `pynetdicom` integration.

## Trade-offs

**Research value:** Explores dynamic endpoint selection  
**Practical challenge:** DICOM expects static endpoints (host, port, AE_Title)

This creates an interesting research question about maintaining compatibility while adding dynamic behavior.
