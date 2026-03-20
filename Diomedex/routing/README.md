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

## Current Status

This is a proof-of-concept implementation. Full DICOM C-STORE functionality requires `pynetdicom` integration.

## Trade-offs

**Research value:** Explores dynamic endpoint selection  
**Practical challenge:** DICOM expects static endpoints (host, port, AE_Title)

This creates an interesting research question about maintaining compatibility while adding dynamic behavior.
