"""Run the verifier CLI through real Edge, forwarder, and cloud Orthanc services."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.uid import GrayscaleSoftcopyPresentationStateStorage, generate_uid

from src.simulator.generate_dicom import make_ct_8x8
from tests.integration.conftest import _delete_instance
from tests.integration.settings import CA_CERT, CLOUD_URLS, EDGE_URL, ORTHANC_AUTH

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("presentation_state", [False, True], ids=["no-pixels", "gsps"])
def test_verifier_without_pixels_through_stack(tmp_path, presentation_state):
    """Orthanc storage and forwarding must preserve the audited non-pixel metadata."""
    ds = make_ct_8x8()
    del ds.PixelData
    # Unique identities prevent deduplication from hiding an upload or reusing stale data.
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    if presentation_state:
        ds.SOPClassUID = GrayscaleSoftcopyPresentationStateStorage
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        ds.Modality = "PR"
        reference = Dataset()
        reference.SeriesInstanceUID = generate_uid()
        image = Dataset()
        image.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
        image.ReferencedSOPInstanceUID = generate_uid()
        reference.ReferencedImageSequence = Sequence([image])
        ds.ReferencedSeriesSequence = Sequence([reference])

    local_path = tmp_path / "original.dcm"
    ds.save_as(local_path, enforce_file_format=True)
    env = os.environ.copy()
    env.update(
        EDGE_AGENT1_HTTPS_URL=EDGE_URL,
        EDGE_USER=ORTHANC_AUTH[0],
        EDGE_PASS=ORTHANC_AUTH[1],
        ORTHANC_USER=ORTHANC_AUTH[0],
        ORTHANC_PASSWORD=ORTHANC_AUTH[1],
        REQUESTS_CA_BUNDLE=str(Path(CA_CERT).resolve()),
        LOG_LEVEL="INFO",
        PYTHONIOENCODING="utf-8",
    )
    for region, url in zip(("US", "EU", "ASIA", "AF"), CLOUD_URLS.values(), strict=True):
        env[f"NODE_{region}_HTTPS_URL"] = url

    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.utils.verify_pipeline", "--local-path", str(local_path)],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "Perfect Metadata Sync" in output, output
        assert "End-to-end verification passed cleanly" in output, output
    finally:
        # The forwarder can leave the edge copy briefly after the cloud download succeeds.
        for url in (EDGE_URL, *CLOUD_URLS.values()):
            _delete_instance(
                url, str(ds.SOPInstanceUID), ORTHANC_AUTH, str(Path(CA_CERT).resolve())
            )
