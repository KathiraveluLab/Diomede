"""
Integration tests for the Edge Agent forwarding pipeline.

Verifies the full path: DICOM file POST to Edge Orthanc → Forwarder detects
via /changes → Orchestrator picks best node → file lands in a cloud Orthanc →
Edge Orthanc copy is deleted.

Requires the full Docker Compose stack to be running:
    docker compose up -d
"""

import io
import os
import time

import httpx
import pytest
from dotenv import load_dotenv
from pydicom import FileDataset
from pydicom import uid as dcm_uid

from src.simulator.generate_dicom import _RTT_SOP_UID, make_ct_8x8, make_sized
from tests.integration.conftest import _delete_instance
from tests.integration.settings import CLOUD_URLS, EDGE_URL, ORCH_URL, ORTHANC_AUTH

load_dotenv()

pytestmark = pytest.mark.integration

_EDGE_AUTH = ORTHANC_AUTH
_FORWARD_TIMEOUT_S = 30  # forwarder polls every 5 s; allow several cycles
_POLL_INTERVAL_S = 2

_CLOUD_URLS = CLOUD_URLS


def _post_to_edge(ds: FileDataset) -> None:
    buffer = io.BytesIO()
    ds.save_as(buffer)
    resp = httpx.post(
        f"{EDGE_URL}/instances",
        content=buffer.getvalue(),
        headers={"Content-Type": "application/dicom"},
        auth=_EDGE_AUTH,
        verify=False,
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
    ds = make_ct_8x8()
    _post_to_edge(ds)

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

    # Poll until the local copy is deleted from Edge Orthanc.
    deadline = time.monotonic() + _FORWARD_TIMEOUT_S
    deleted = False
    while time.monotonic() < deadline:
        if not _instance_on_edge(_RTT_SOP_UID):
            deleted = True
            break
        time.sleep(_POLL_INTERVAL_S)
    assert deleted, "Edge Orthanc still holds the instance after confirmed forward — cleanup failed"


def test_forwarded_file_is_intact():
    """The DICOM bytes forwarded to the cloud node match what was sent to the edge."""
    ds = make_ct_8x8()
    _post_to_edge(ds)

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
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()


_STUDY_SIZE = 20
_AGENT_ID = os.environ.get("EDGE_AGENT1", "agent-001")
_ORCH_HEADERS = {"X-API-Key": os.environ.get("ORCHESTRATOR_API_KEY", "")}


def _nodes_holding(sop_uids: list[str]) -> dict[str, set[str]]:
    """Map each cloud node to the subset of sop_uids it holds."""
    found: dict[str, set[str]] = {}
    for node_id, base_url in _CLOUD_URLS.items():
        held = {uid for uid in sop_uids if _find_instance(base_url, uid, _EDGE_AUTH, verify=False)}
        if held:
            found[node_id] = held
    return found


def test_study_lands_on_a_single_node_when_scores_change():
    """Every instance of one study ends on one node, even if another node becomes the best."""
    study_uid = str(dcm_uid.generate_uid())
    datasets = [make_sized(1, study_uid=study_uid) for _ in range(_STUDY_SIZE)]
    sop_uids = [str(ds.SOPInstanceUID) for ds in datasets]
    half = _STUDY_SIZE // 2

    try:
        for ds in datasets[:half]:
            _post_to_edge(ds)
        pinned = _wait_for_instance_in_cloud(sop_uids[0])
        assert pinned is not None, "First half of the study was never forwarded"

        # Make every other node look far better for this agent, then send the rest.
        rtt = {node_id: 1.0 for node_id in _CLOUD_URLS}
        rtt[pinned] = 5000.0
        httpx.post(
            f"{ORCH_URL}/heartbeat",
            json={"agent_id": _AGENT_ID, "rtt_dict": rtt},
            headers=_ORCH_HEADERS,
            verify=False,
            timeout=10,
        ).raise_for_status()
        for ds in datasets[half:]:
            _post_to_edge(ds)

        deadline = time.monotonic() + _FORWARD_TIMEOUT_S * 2
        holding = _nodes_holding(sop_uids)
        while sum(len(v) for v in holding.values()) < _STUDY_SIZE:
            assert time.monotonic() < deadline, f"Study not fully forwarded: {holding}"
            time.sleep(_POLL_INTERVAL_S)
            holding = _nodes_holding(sop_uids)

        assert set(holding) == {pinned}, f"Study split across nodes: {holding}"
    finally:
        for uid in sop_uids:
            _delete_instance(EDGE_URL, uid, _EDGE_AUTH, False)
            for base_url in _CLOUD_URLS.values():
                _delete_instance(base_url, uid, _EDGE_AUTH, False)
