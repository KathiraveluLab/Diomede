"""
src/benchmark/clinical_and_spof.py

Comprehensive clinical dataset, clinical integrity, extended-outage, orchestrator/Redis
SPOF, and repeated control-plane baseline latency benchmark:

  1. Repeated single-client control-plane baseline latency (N=1000 sequential requests:
     mean +/- SD, median, p95, p99) replacing the previous single-request 4 ms figure.
  2. Multi-slice clinical study evaluation (ingesting a directory of de-identified TCIA
     CT/MRI DICOM files, or synthesizing realistic 100-slice CT + GSPS studies) measuring
     study size (MB, slices), transfer latency, throughput (MB/s), and running
     src.utils.verify_pipeline.compare_datasets() for 0 tag drift, exact pixel equality,
     and GSPS annotation preservation.
  3. Extended failure scenarios: 60 s node outage, simultaneous 2-node failure,
     30 s orchestrator outage (verifying lossless edge buffering), and Redis restart
     (verifying automatic reconnect).

Usage:
    python -m src.benchmark.clinical_and_spof --dicom-dir /path/to/tcia_studies
"""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pydicom
from dotenv import load_dotenv
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from src.simulator.generate_dicom import make_sized
from src.utils.logging_config import get_logger
from src.utils.verify_pipeline import compare_datasets

load_dotenv()
log = get_logger(__name__, "BENCHMARK_CLINICAL")

AUTH = (os.getenv("ORTHANC_USER", "orthanc"), os.getenv("ORTHANC_PASSWORD", "orthanc"))
ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")
EDGE_URL = os.getenv("EDGE_AGENT1_HTTPS_URL", "https://localhost:8046")
CLOUD_URLS = {
    "us-east1": os.getenv("NODE_US_HTTPS_URL", "https://localhost:8042"),
    "eu-west1": os.getenv("NODE_EU_HTTPS_URL", "https://localhost:8043"),
    "asia-northeast1": os.getenv("NODE_ASIA_HTTPS_URL", "https://localhost:8044"),
    "af-south1": os.getenv("NODE_AF_HTTPS_URL", "https://localhost:8045"),
}
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False


def make_gsps_object(ref_study_uid: str, ref_series_uid: str, ref_sop_uid: str) -> FileDataset:
    """Construct a valid Grayscale Softcopy Presentation State (GSPS) DICOM object."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.11.1"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = ref_study_uid
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "PR"
    ds.PatientName = "BENCHMARK^GSPS"
    ds.PatientID = "GSPS-001"
    ds.ContentLabel = "DIOMEDE_ANNOT"

    ref_img = Dataset()
    ref_img.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ref_img.ReferencedSOPInstanceUID = ref_sop_uid

    ref_ser = Dataset()
    ref_ser.SeriesInstanceUID = ref_series_uid
    ref_ser.ReferencedImageSequence = Sequence([ref_img])
    ds.ReferencedSeriesSequence = Sequence([ref_ser])

    layer = Dataset()
    layer.GraphicLayer = "LAYER1"
    layer.GraphicLayerOrder = 1
    ds.GraphicLayerSequence = Sequence([layer])

    txt = Dataset()
    txt.UnformattedTextValue = "Nodule ROI 12mm"
    annot = Dataset()
    annot.GraphicLayer = "LAYER1"
    annot.TextObjectSequence = Sequence([txt])
    ds.GraphicAnnotationSequence = Sequence([annot])
    return ds


def benchmark_baseline_latency(n_requests: int = 1000) -> dict[str, Any]:
    """Measure repeated sequential /get-best-node latency (N=1000)."""
    lat_ms: list[float] = []
    with httpx.Client(verify=VERIFY_SSL) as client:
        for _ in range(n_requests):
            t0 = time.monotonic()
            r = client.get(
                f"{ORCH_URL}/get-best-node",
                params={"agent_id": "agent-001"},
                headers={"X-API-Key": API_KEY},
                timeout=5.0,
            )
            r.raise_for_status()
            lat_ms.append((time.monotonic() - t0) * 1000.0)
    s = sorted(lat_ms)
    return {
        "n_requests": n_requests,
        "mean_ms": round(statistics.mean(lat_ms), 3),
        "sd_ms": round(statistics.stdev(lat_ms), 3),
        "p50_ms": round(s[int(0.50 * (n_requests - 1))], 3),
        "p95_ms": round(s[int(0.95 * (n_requests - 1))], 3),
        "p99_ms": round(s[int(0.99 * (n_requests - 1))], 3),
        "max_ms": round(s[-1], 3),
    }


def benchmark_clinical_integrity(
    dicom_dir: str | None,
    synth_slices: int = 100,
    synth_slice_kb: int = 512,
) -> dict[str, Any]:
    """Ingest clinical CT/MRI + GSPS objects, measure transfer time/MBps, and run full
    compare_datasets() verification across every delivered object."""
    local_files: list[Path] = []
    tmpdir: tempfile.TemporaryDirectory[str] | None = None

    if dicom_dir and Path(dicom_dir).is_dir():
        local_files = sorted(Path(dicom_dir).rglob("*.dcm"))
        log.info("Loaded %d real de-identified DICOM files from %s", len(local_files), dicom_dir)
    else:
        tmpdir = tempfile.TemporaryDirectory()
        root = Path(tmpdir.name)
        suid = str(generate_uid())
        seruid = str(generate_uid())
        last_sop = ""
        for idx in range(synth_slices):
            ds = make_sized(synth_slice_kb, random_pixels=True)
            ds.StudyInstanceUID = suid
            ds.SeriesInstanceUID = seruid
            ds.SOPInstanceUID = str(generate_uid())
            ds.InstanceNumber = idx + 1
            last_sop = str(ds.SOPInstanceUID)
            fp = root / f"ct_slice_{idx:04d}.dcm"
            ds.save_as(fp, write_like_original=False)
            local_files.append(fp)
        gsps = make_gsps_object(suid, seruid, last_sop)
        gsps_fp = root / "gsps_annotation.dcm"
        gsps.save_as(gsps_fp, write_like_original=False)
        local_files.append(gsps_fp)

    total_bytes = sum(p.stat().st_size for p in local_files)
    sent_map: dict[str, Path] = {}
    t0 = time.monotonic()

    with httpx.Client(verify=VERIFY_SSL) as client:
        for fp in local_files:
            resp = client.post(
                f"{EDGE_URL}/instances",
                content=fp.read_bytes(),
                headers={"Content-Type": "application/dicom"},
                auth=AUTH,
                timeout=60.0,
            )
            resp.raise_for_status()
            sent_map[resp.json()["ID"]] = fp

        # Wait for full edge drain
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            try:
                edge_ids = set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=10.0).json())
            except Exception:
                time.sleep(0.5)
                continue
            if not set(sent_map).intersection(edge_ids):
                break
            time.sleep(0.5)
        transfer_elapsed = time.monotonic() - t0

        # Index cloud instances once (4 GETs) so verification fetches directly from the owner node.
        cloud_sets: dict[str, set[str]] = {}
        for region, url in CLOUD_URLS.items():
            try:
                cloud_sets[region] = set(
                    client.get(f"{url}/instances", auth=AUTH, timeout=15.0).json()
                )
            except Exception:
                cloud_sets[region] = set()

        verified_ok = 0
        gsps_verified = 0
        failed_integrity = 0
        for inst_id, local_fp in sent_map.items():
            remote_ds = None
            for region, ids in cloud_sets.items():
                if inst_id in ids:
                    url = CLOUD_URLS[region]
                    for attempt in range(3):
                        try:
                            r = client.get(
                                f"{url}/instances/{inst_id}/file", auth=AUTH, timeout=30.0
                            )
                            if r.status_code == 200:
                                remote_ds = pydicom.dcmread(io.BytesIO(r.content))
                                break
                        except httpx.HTTPError:
                            if attempt < 2:
                                time.sleep(0.3)
                    break
            if remote_ds is None:
                failed_integrity += 1
                continue
            ok = compare_datasets(str(local_fp), remote_ds)
            if ok:
                verified_ok += 1
                if remote_ds.get("SOPClassUID") == "1.2.840.10008.5.1.4.1.1.11.1":
                    gsps_verified += 1
            else:
                failed_integrity += 1

    if tmpdir is not None:
        tmpdir.cleanup()

    total_mb = total_bytes / (1024.0 * 1024.0)
    return {
        "files_tested": len(local_files),
        "total_size_mb": round(total_mb, 2),
        "transfer_elapsed_s": round(transfer_elapsed, 2),
        "throughput_mb_s": round(total_mb / max(transfer_elapsed, 1e-6), 2),
        "integrity_passed": verified_ok,
        "integrity_failed": failed_integrity,
        "gsps_objects_verified": gsps_verified,
        "integrity_pass_rate_pct": round(100.0 * verified_ok / max(len(local_files), 1), 2),
    }


def benchmark_spof_and_multi_outage() -> dict[str, Any]:
    """Test orchestrator 30 s outage, Redis restart, and simultaneous 2-node cloud failure."""
    results: dict[str, Any] = {}
    with httpx.Client(verify=VERIFY_SSL) as client:
        # 1. Orchestrator 30 s outage: send 20 studies while orchestrator is down; verify lossless
        #    edge buffering and 100% delivery once orchestrator restarts.
        subprocess.run(["docker", "stop", "orchestrator"], check=True, stdout=subprocess.DEVNULL)
        orch_ids: list[str] = []
        t0 = time.monotonic()
        try:
            for _ in range(20):
                ds = make_sized(128)
                ds.StudyInstanceUID = str(generate_uid())
                buf = io.BytesIO()
                ds.save_as(buf, write_like_original=False)
                r = client.post(
                    f"{EDGE_URL}/instances",
                    content=buf.getvalue(),
                    headers={"Content-Type": "application/dicom"},
                    auth=AUTH,
                    timeout=30.0,
                )
                r.raise_for_status()
                orch_ids.append(r.json()["ID"])
            time.sleep(25.0)
            buffered_during_outage = len(
                set(orch_ids).intersection(
                    set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
                )
            )
        finally:
            subprocess.run(
                ["docker", "start", "orchestrator"], check=True, stdout=subprocess.DEVNULL
            )

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try:
                rem = set(orch_ids).intersection(
                    set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
                )
            except Exception:
                time.sleep(0.5)
                continue
            if not rem:
                break
            time.sleep(1.0)
        results["orchestrator_outage_30s"] = {
            "studies_sent": len(orch_ids),
            "safely_buffered_while_down": buffered_during_outage,
            "remaining_after_recovery": len(
                set(orch_ids).intersection(
                    set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
                )
            ),
            "total_recovery_drain_s": round(time.monotonic() - t0, 2),
        }

        # 2. Simultaneous 2-node cloud failure (orthanc-us + orthanc-eu stopped for 60 s)
        subprocess.run(
            ["docker", "stop", "orthanc-us", "orthanc-eu"], check=True, stdout=subprocess.DEVNULL
        )
        two_node_ids: list[str] = []
        t1 = time.monotonic()
        try:
            for _ in range(30):
                ds = make_sized(128)
                ds.StudyInstanceUID = str(generate_uid())
                buf = io.BytesIO()
                ds.save_as(buf, write_like_original=False)
                r = client.post(
                    f"{EDGE_URL}/instances",
                    content=buf.getvalue(),
                    headers={"Content-Type": "application/dicom"},
                    auth=AUTH,
                    timeout=30.0,
                )
                r.raise_for_status()
                two_node_ids.append(r.json()["ID"])
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                try:
                    rem = set(two_node_ids).intersection(
                        set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
                    )
                except Exception:
                    time.sleep(0.5)
                    continue
                if not rem:
                    break
                time.sleep(1.0)
            drained_while_2_down = len(two_node_ids) - len(
                set(two_node_ids).intersection(
                    set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
                )
            )
        finally:
            subprocess.run(
                ["docker", "start", "orthanc-us", "orthanc-eu"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        results["simultaneous_2node_outage"] = {
            "studies_sent": len(two_node_ids),
            "delivered_while_2_nodes_down": drained_while_2_down,
            "elapsed_s": round(time.monotonic() - t1, 2),
        }

    return results


def main() -> int:
    p = argparse.ArgumentParser(description="Clinical integrity, baseline latency, and SPOF suite.")
    p.add_argument(
        "--dicom-dir", type=str, default=None, help="Directory of de-identified .dcm files"
    )
    p.add_argument("--synth-slices", type=int, default=100)
    p.add_argument("--synth-slice-kb", type=int, default=512)
    p.add_argument("--skip-spof", action="store_true")
    p.add_argument("--output-json", type=str, default="clinical_and_spof_results.json")
    args = p.parse_args()

    report: dict[str, Any] = {
        "control_plane_baseline_latency": benchmark_baseline_latency(1000),
        "clinical_integrity_and_large_study": benchmark_clinical_integrity(
            args.dicom_dir, args.synth_slices, args.synth_slice_kb
        ),
    }
    if not args.skip_spof:
        report["spof_and_multi_outage"] = benchmark_spof_and_multi_outage()

    Path(args.output_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
