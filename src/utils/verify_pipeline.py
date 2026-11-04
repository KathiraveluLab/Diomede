#!/usr/bin/env python3
"""
DICOM End-to-End Pipeline Verifier.
Compares a local original DICOM file against a remote processed version fetched
from an Orthanc server to identify metadata drops, alterations, or pixel shifts.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

import numpy as np
import pydicom
import requests
from dotenv import load_dotenv

from src.utils.env import require_env
from src.utils.logging_config import get_logger

log = get_logger(__name__, "VERIFY_PIPELINE")
load_dotenv()


def create_secure_session(ca_path, auth):
    """
    Initializes a pre-configured requests Session.
    Enforces SSL validation using the local CA for all requests.
    """
    log.info(f"[*] Creating secure session with CA bundle: {ca_path}")

    if os.getenv("REQUESTS_CA_BUNDLE") == "/certs/ca.pem":
        local_relative_path = "./certs/ca.pem"
        if os.path.exists(local_relative_path):
            os.environ["REQUESTS_CA_BUNDLE"] = local_relative_path
            os.environ["CURL_CA_BUNDLE"] = (
                local_relative_path  # Bypasses underlying curl-level fallbacks
            )
        else:
            print(f"[X] Critical Error: Cannot locate certificate file at {local_relative_path}")
            sys.exit(1)
    else:
        local_relative_path = os.getenv("REQUESTS_CA_BUNDLE")

    if not os.path.exists(local_relative_path):
        log.info(f"[FAIL] Local CA certificate file not found at: {local_relative_path}")
        exit(1)

    session = requests.Session()
    session.auth = auth
    session.verify = local_relative_path  # Automatically applies to all requests in this session
    return session


def upload_dicom_to_orthanc(
    session: requests.Session, file_path: str, auth: tuple[str, str]
) -> str | None:
    """
    Uploads a local DICOM file to the Orthanc server.
    """
    url = require_env("EDGE_AGENT1_HTTPS_URL") + "/instances"
    headers = {"Content-Type": "application/dicom"}

    log.info(f"[STEP 1] Uploading {file_path} to Orthanc...")

    # Ensure file exists before trying to open it
    if not os.path.exists(file_path):
        log.error(f"[FAIL] Local file not found: {file_path}")
        return None

    try:
        # Open and read raw binary data
        with open(file_path, "rb") as dicom_file:
            file_data = dicom_file.read()

        # Verification is inherited from the session's CA bundle
        response = session.post(url, auth=auth, headers=headers, data=file_data)

        # Verify Orthanc success code (200 OK)
        if response.status_code == 200:
            log.info("[SUCCESS] DICOM uploaded successfully.")
            instance_id = response.json().get("ID")
            log.info(f"Orthanc Response ID: {instance_id}")
            return instance_id
        else:
            log.error(f"[FAIL] Server returned status code: {response.status_code}")
            log.error(f"Response: {response.text}")
            return None

    except requests.exceptions.SSLError as ssl_err:
        log.error(f"[FAIL] SSL Verification failed: {ssl_err}")
        return None
    except requests.exceptions.RequestException as e:
        log.error(f"[FAIL] Pipeline error: Connection failed. {e}")
        return None


def download_remote_dicom(session: requests.Session, instance_id: str) -> pydicom.Dataset:
    """Download the raw Part-10 file stream from remote Orthanc into a memory-resident Dataset."""
    found = False
    attempts = 10
    remote_urls = [
        require_env("NODE_US_HTTPS_URL"),
        require_env("NODE_EU_HTTPS_URL"),
        require_env("NODE_ASIA_HTTPS_URL"),
        require_env("NODE_AF_HTTPS_URL"),
    ]
    auth = (require_env("ORTHANC_USER"), require_env("ORTHANC_PASSWORD"))

    while (not found) and attempts > 0:
        for remote_url in remote_urls:
            url = f"{remote_url}/instances/{instance_id}/file"
            try:
                with session.get(url, auth=auth, stream=True) as response:
                    if response.status_code == 200:
                        log.info(
                            f"[*] Successfully fetched instance '{instance_id}' from {remote_url}."
                        )
                        bytes_io = io.BytesIO()
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:  # Filter out keep-alive new chunks
                                bytes_io.write(chunk)

                        bytes_io.seek(0)
                        log.info(
                            f"[*] Download complete. Bytes received: {bytes_io.getbuffer().nbytes}"
                        )
                        found = True
                        return pydicom.dcmread(bytes_io)
                    elif response.status_code == 404:
                        log.info(
                            f"[i] Instance ID '{instance_id}' not found on {remote_url}. "
                            "Trying next node..."
                        )
                        continue
            except requests.exceptions.HTTPError as http_err:
                log.error(f"[X] HTTP error occurred while fetching from {remote_url}: {http_err}")
                raise
            except Exception as exc:
                log.error(f"[X] Unexpected error while fetching from {remote_url}: {exc}")
                raise
        attempts -= 1
        log.info(
            f"[i] Instance ID '{instance_id}' not found on any node. Attempts remaining: {attempts}"
        )
        time.sleep(6)  # Wait before retrying


def compare_datasets(local_path: str, dcm_proc: pydicom.Dataset) -> bool:
    """Compare metadata structures and exact pixel payloads between local and remote files."""
    dcm_orig = pydicom.dcmread(local_path)
    pipeline_intact = True

    log.info("\n" + "=" * 60)
    log.info(" PHASE 1: METADATA CHECK ")
    log.info("=" * 60)

    # Define common tags to check side-by-side
    common_tags = [
        ("Instance UID", "SOPInstanceUID"),
        ("Patient Name", "PatientName"),
        ("Patient ID", "PatientID"),
        ("Modality", "Modality"),
        ("Study Date", "StudyDate"),
        ("Series Description", "SeriesDescription"),
    ]

    log.info(f"{'Field':<20} | {'Original (Local)':<25} | {'Processed (Remote)':<25}")
    log.info("-" * 76)
    for label, attribute in common_tags:
        val_orig = str(dcm_orig.get(attribute, "N/A"))
        val_proc = str(dcm_proc.get(attribute, "N/A"))
        log.info(f"{label:<20} | {val_orig:<25} | {val_proc:<25}")

    log.info("\n" + "=" * 60)
    log.info(" PHASE 2: DETAILED METADATA TAG AUDIT ")
    log.info("=" * 60)

    # Union set of all standard and vendor-private tags found across both headers
    all_tags = set(dcm_orig.keys()) | set(dcm_proc.keys())
    metadata_matches = True

    for tag in sorted(all_tags):
        # Explicitly skip the raw pixel payload data tags during text reporting loops
        if tag == 0x7FE00010:
            continue

        in_orig = tag in dcm_orig
        in_proc = tag in dcm_proc

        if in_orig and not in_proc:
            log.info(f"[-] Tag {tag} ({dcm_orig[tag].name}): Missing in remote instance")
            metadata_matches = False
        elif not in_orig and in_proc:
            log.info(
                f"[+] Tag {tag} ({dcm_proc[tag].name}): Added remotely ({dcm_proc[tag].value})"
            )
            metadata_matches = False
        else:
            val_orig = dcm_orig[tag].value
            val_proc = dcm_proc[tag].value
            if val_orig != val_proc:
                log.info(f"[Δ] Tag {tag} ({dcm_orig[tag].name}) altered:")
                log.info(f"    - Original:  {val_orig}")
                log.info(f"    - Remote:    {val_proc}")
                metadata_matches = False

    if metadata_matches:
        log.info("[✓] Perfect Metadata Sync: No header tag drift detected.")
    else:
        pipeline_intact = False

    log.info("\n" + "=" * 60)
    log.info(" PHASE 3: PIXEL DATA FRACTION TESTING ")
    log.info("=" * 60)

    # Detect if the dataset is a Presentation State object
    is_presentation_state = dcm_orig.get("SOPClassUID") == "1.2.840.10008.5.1.4.1.1.11.1"

    if is_presentation_state:
        log.info(
            "[i] Detected Grayscale Softcopy Presentation State (GSPS). Bypassing pixel checks."
        )
        log.info("[*] Running Structural Annotation Audit...")

        gsps_intact = True

        # 1. Audit Referencing Image Links (Ensures the GSPS still points to the same target images)
        orig_ref_series = dcm_orig.get("ReferencedSeriesSequence")
        proc_ref_series = dcm_proc.get("ReferencedSeriesSequence")

        if (orig_ref_series is None) != (proc_ref_series is None):
            log.error("[X] Integrity Error: ReferencedSeriesSequence availability mismatch.")
            gsps_intact = False
        elif orig_ref_series and proc_ref_series:
            # Safely stringify internal nested structures to quickly verify if links match
            if str(orig_ref_series) != str(proc_ref_series):
                log.warning("[Δ] Warning: Target image reference linkages have changed.")
                gsps_intact = False
            else:
                log.info("[✓] Verification Passed: Image target links are perfectly intact.")

        # 2. Audit Annotation Layer Sequences (Graphic Layers / Text / Overlays)
        annotation_tags = [
            ("GraphicLayerSequence", "Graphic Layers"),
            ("GraphicAnnotationSequence", "Vector Drawings/Annotations"),
            ("TextObjectSequence", "Text Annotations"),
        ]

        for tag_name, label in annotation_tags:
            if tag_name in dcm_orig or tag_name in dcm_proc:
                val_orig = str(dcm_orig.get(tag_name, "Missing"))
                val_proc = str(dcm_proc.get(tag_name, "Missing"))
                if val_orig != val_proc:
                    log.warning(
                        f"[Δ] Warning: {label} ({tag_name}) data differs between local and remote."
                    )
                    gsps_intact = False
                else:
                    log.info(f"[✓] Verification Passed: {label} match perfectly.")

        return gsps_intact

    if "PixelData" not in dcm_orig and "PixelData" not in dcm_proc:
        log.info("[i] No pixel data present in either file. Skipping pixel comparison.")
        return True

    if "PixelData" not in dcm_orig or "PixelData" not in dcm_proc:
        log.info("[!] Aborting image validation: One or both files do not contain pixel matrices.")
        return False

    pixels_orig = dcm_orig.pixel_array
    pixels_proc = dcm_proc.pixel_array

    # 1. Structural Dimensional Layout Matching
    if pixels_orig.shape != pixels_proc.shape:
        log.info("[X] Spatial Error: Structural matrix framing dimensions changed.")
        log.info(f"    - Original Matrix Shape: {pixels_orig.shape}")
        log.info(f"    - Remote Matrix Shape:   {pixels_proc.shape}")
        return False

    # 2. Perfect Mathematical Equality Test
    if np.array_equal(pixels_orig, pixels_proc):
        log.info("[✓] Perfect Content Match: Image matrices are exactly identical.")
    else:
        pipeline_intact = False
        # Prevent integer underflow wrapping bugs by explicitly casting unsigned elements
        # to signed 32-bit blocks
        diff = np.abs(pixels_orig.astype(np.int32) - pixels_proc.astype(np.int32))
        max_diff = np.max(diff)
        mean_diff = np.mean(diff)
        changed_count = np.count_nonzero(diff)
        total_pixels = pixels_orig.size
        pct_changed = (changed_count / total_pixels) * 100

        log.info("[Δ] Image matrix variances identified (Pipeline is NOT lossless):")
        log.info(f"    - Maximum pixel deviation:   {max_diff}")
        log.info(f"    - Mean absolute error delta: {mean_diff:.4f}")
        log.info(f"    - Mutated pixels: {changed_count} / {total_pixels} ({pct_changed:.2f}%)")

    return pipeline_intact


def parse_args() -> argparse.Namespace:
    """Parse runtime command line variables."""
    parser = argparse.ArgumentParser(
        description="Verify end-to-end data integrity between a local file and a remote instance."
    )
    parser.add_argument(
        "--local-path", required=True, help="Absolute or relative path to local source DICOM file."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    log.info("Upload file")

    auth = (require_env("EDGE_USER"), require_env("EDGE_PASS"))
    session = create_secure_session(ca_path=require_env("REQUESTS_CA_BUNDLE"), auth=auth)

    instance_id = upload_dicom_to_orthanc(session, args.local_path, auth)

    # Instance ID stays the same for the unmodified file
    remote_id = instance_id
    auth = (require_env("ORTHANC_USER"), require_env("ORTHANC_PASSWORD"))
    session = create_secure_session(ca_path=require_env("REQUESTS_CA_BUNDLE"), auth=auth)

    try:
        remote_dataset = download_remote_dicom(session, remote_id)
        success = compare_datasets(args.local_path, remote_dataset)

        log.info("\n" + "=" * 60)
        if success:
            log.info("[SUCCESS] End-to-end verification passed cleanly.")
            sys.exit(0)
        else:
            log.info(
                "[FAILURE] Pipeline data variance detected between original and processed file."
            )
            sys.exit(1)

    except FileNotFoundError:
        log.info(f"[X] Execution Failed: Local target path '{args.local_path}' does not exist.")
        sys.exit(1)
    except Exception as exc:
        log.info(f"[X] Runtime System Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
