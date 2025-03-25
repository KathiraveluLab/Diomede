# DICOM Albums Module

## Overview
This module handles the creation and management of DICOM image albums.

## API Endpoints

### Scan Directory
`POST /albums/scan`

**Request:**
```json
{
    "path": "/path/to/dicom/files"
}