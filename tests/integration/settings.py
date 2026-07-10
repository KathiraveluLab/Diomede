"""
Source of truth for endpoints for integration testing
"""

import os

ORTHANC_USER = os.environ.get("ORTHANC_USER", "orthanc")
ORTHANC_PASSWORD = os.environ.get("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION")
ORTHANC_AUTH = (ORTHANC_USER, ORTHANC_PASSWORD)

CA_CERT = os.environ.get("TEST_CA_CERT", "certs/ca.pem")
CLIENT_CERT = os.environ.get("TEST_CLIENT_CERT", "certs/diomede-client/client.crt")
CLIENT_KEY = os.environ.get("TEST_CLIENT_KEY", "certs/diomede-client/client.key")

ORTHANC_URL = os.environ.get("TEST_ORTHANC_US_URL", "https://localhost:8042")
EDGE_URL = os.environ.get("TEST_EDGE_URL", "https://localhost:8046")
ORCH_URL = os.environ.get("TEST_ORCH_URL", "https://localhost:8000")

CLOUD_URLS = {
    "us-east1": ORTHANC_URL,
    "eu-west1": os.environ.get("TEST_ORTHANC_EU_URL", "https://localhost:8043"),
    "asia-northeast1": os.environ.get("TEST_ORTHANC_ASIA_URL", "https://localhost:8044"),
    "af-south1": os.environ.get("TEST_ORTHANC_AF_URL", "https://localhost:8045"),
}

DICOM_HOST = os.environ.get("TEST_DICOM_HOST", "localhost")
DICOM_PORT = int(os.environ.get("TEST_DICOM_PORT", "4242"))
DICOM_CALLED_AET = os.environ.get("TEST_DICOM_CALLED_AET", "Orthanc_US")
DICOM_CALLING_AET = os.environ.get("TEST_DICOM_CALLING_AET", "Simulator")
