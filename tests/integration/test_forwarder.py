"""
Integration tests for the Edge Agent forwarding pipeline.

Verifies the full path: DICOM file POST to Edge Orthanc → Forwarder detects
via /changes → Orchestrator picks best node → file lands in a cloud Orthanc →
Edge Orthanc copy is deleted.

Requires the full Docker Compose stack to be running:
    docker compose up -d
"""

import time

import httpx
import pytest

from src.simulator.generate_dicom import _RTT_SOP_UID, make_ct_8x8
from tests.integration.conftest import (
    EDGE_URL,
    _delete_instance,
)

pytestmark = pytest.mark.integration

_EDGE_AUTH = ("orthanc", "orthanc")
_FORWARD_TIMEOUT_S = 30  # forwarder polls every 5 s; allow several cycles
_POLL_INTERVAL_S = 2

_CLOUD_URLS = {
    "us-east1": "http://localhost:8042",
    "eu-west1": "http://localhost:8043",
    "asia-northeast1": "http://localhost:8044",
    "af-south1": "http://localhost:8045",
}


def _post_to_edge(dcm_bytes: bytes) -> None:
    resp = httpx.post(
        f"{EDGE_URL}/instances",
        content=dcm_bytes,
        headers={"Content-Type": "application/dicom"},
        auth=_EDGE_AUTH,
        timeout=30,
    )
    resp.raise_for_status()


def _find_instance(base_url: str, sop_uid: str, auth: tuple, verify=True) -> list[dict]:
    resp = httpx.post(
        f"{base_url}/tools/find",
        json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_uid}, "Expand": True},
        auth=auth,
        verify=verify,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _wait_for_instance_in_cloud(sop_uid: str, timeout: float = _FORWARD_TIMEOUT_S) -> str | None:
    """Poll all cloud nodes until the instance appears in one. Returns the node URL or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for node_id, base_url in _CLOUD_URLS.items():
            try:
                results = _find_instance(base_url, sop_uid, _EDGE_AUTH, verify=False)
                if results:
                    return node_id
            except Exception:
                pass
        time.sleep(_POLL_INTERVAL_S)
    return None


def _instance_on_edge(sop_uid: str) -> bool:
    """Return True if the instance is still present on Edge Orthanc."""
    try:
        return bool(_find_instance(EDGE_URL, sop_uid, _EDGE_AUTH, verify=False))
    except Exception:
        return False


@pytest.fixture(autouse=True)
def clean_edge_and_cloud():
    """Remove the test instance from Edge and all cloud nodes before and after each test."""
    _delete_instance(EDGE_URL, _RTT_SOP_UID, _EDGE_AUTH, False)
    for base_url in _CLOUD_URLS.values():
        _delete_instance(base_url, _RTT_SOP_UID, _EDGE_AUTH, False)
    yield
    _delete_instance(EDGE_URL, _RTT_SOP_UID, _EDGE_AUTH, False)
    for base_url in _CLOUD_URLS.values():
        _delete_instance(base_url, _RTT_SOP_UID, _EDGE_AUTH, False)


def test_file_forwarded_to_a_cloud_node():
    """End-to-end: file posted to Edge appears in a cloud node within the timeout."""
    dcm_bytes = make_ct_8x8()
    _post_to_edge(dcm_bytes)

    node_id = _wait_for_instance_in_cloud(_RTT_SOP_UID)
    assert node_id is not None, (
        f"Instance {_RTT_SOP_UID} not forwarded to any cloud node within {_FORWARD_TIMEOUT_S}s"
    )


def test_edge_copy_deleted_after_forward():
    """After the Forwarder routes the file, the local Edge copy must be gone."""
    dcm_bytes = make_ct_8x8()
    _post_to_edge(dcm_bytes)

    # Wait for the file to land in the cloud first.
    node_id = _wait_for_instance_in_cloud(_RTT_SOP_UID)
    assert node_id is not None, "File never forwarded — cannot test edge cleanup"

    # Give the Forwarder one extra poll cycle to delete the local copy.
    time.sleep(_POLL_INTERVAL_S * 2)
    assert not _instance_on_edge(_RTT_SOP_UID), (
        "Edge Orthanc still holds the instance after confirmed forward — cleanup failed"
    )


def test_forwarded_file_is_intact():
    """The DICOM bytes forwarded to the cloud node match what was sent to the edge."""
    dcm_bytes = make_ct_8x8()
    _post_to_edge(dcm_bytes)

    node_id = _wait_for_instance_in_cloud(_RTT_SOP_UID)
    assert node_id is not None, "File never forwarded"

    base_url = _CLOUD_URLS[node_id]
    instances = _find_instance(base_url, _RTT_SOP_UID, _EDGE_AUTH, verify=False)
    assert len(instances) == 1

    # Download the raw file from the cloud node and compare.
    instance_id = instances[0]["ID"]
    resp = httpx.get(
        f"{base_url}/instances/{instance_id}/file",
        auth=_EDGE_AUTH,
        timeout=30,
    )
    resp.raise_for_status()
    assert resp.content == dcm_bytes


def test_second_post_also_forwarded():
    """Two sequential files (different SOP UIDs) both land in the cloud."""
    import io

    import pydicom

    from src.simulator.generate_dicom import make_ct_8x8

    dcm1 = make_ct_8x8()
    dcm2 = make_ct_8x8()  # make_ct_8x8 generates a fresh UID each call

    _post_to_edge(dcm1)
    _post_to_edge(dcm2)

    # Parse SOP UIDs from the two files.
    ds1 = pydicom.dcmread(io.BytesIO(dcm1))
    ds2 = pydicom.dcmread(io.BytesIO(dcm2))

    uid1 = str(ds1.SOPInstanceUID)
    uid2 = str(ds2.SOPInstanceUID)

    assert _wait_for_instance_in_cloud(uid1) is not None, f"File 1 ({uid1}) not forwarded"
    assert _wait_for_instance_in_cloud(uid2) is not None, f"File 2 ({uid2}) not forwarded"
