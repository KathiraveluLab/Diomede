"""Fetch a public de-identified DICOM series from The Cancer Imaging Archive (TCIA).

Uses the unauthenticated TCIA NBIA v1 REST API to download a single open-access
clinical CT/MR series into a local directory (outside version control) so
``src.benchmark.clinical_and_spof --dicom-dir`` can be reproduced without storing
DICOM binaries in Git.

Default series:
    Collection:        LIDC-IDRI (Chest CT)
    SeriesInstanceUID: 1.3.6.1.4.1.14519.5.2.1.6279.6001.179049373636438705059720603192
    Slices:            133 (~66.8 MB)

Usage:
    python scripts/fetch_tcia_sample.py --output-dir /tmp/tcia_lidc_ct --add-gsps
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pydicom

from src.benchmark.clinical_and_spof import make_gsps_object

NBIA_BASE = "https://services.cancerimagingarchive.net/nbia-api/services/v1"
DEFAULT_SERIES_UID = "1.3.6.1.4.1.14519.5.2.1.6279.6001.179049373636438705059720603192"


def fetch_series(
    series_uid: str,
    output_dir: Path,
    add_gsps: bool = False,
    timeout_s: float = 180.0,
) -> dict[str, object]:
    """Download and extract a public TCIA DICOM series into ``output_dir``."""
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(output_dir.rglob("*.dcm"))
    if not existing:
        url = f"{NBIA_BASE}/getImage"
        with httpx.Client(follow_redirects=True, timeout=timeout_s) as client:
            resp = client.get(url, params={"SeriesInstanceUID": series_uid})
            resp.raise_for_status()
            payload = resp.content

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for member in zf.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".dcm"):
                    continue
                target = output_dir / Path(member.filename).name
                target.write_bytes(zf.read(member))

    dcm_files = sorted(output_dir.glob("*.dcm"))
    if not dcm_files:
        raise RuntimeError(f"No .dcm files extracted for series {series_uid}")

    gsps_path = output_dir / "gsps_annotation.dcm"
    if add_gsps and not gsps_path.exists():
        ref_ds = pydicom.dcmread(dcm_files[-1], stop_before_pixels=True)
        gsps = make_gsps_object(
            str(ref_ds.StudyInstanceUID),
            str(ref_ds.SeriesInstanceUID),
            str(ref_ds.SOPInstanceUID),
        )
        gsps.save_as(gsps_path, write_like_original=False)
        dcm_files = sorted(output_dir.glob("*.dcm"))

    sample = pydicom.dcmread(dcm_files[0], stop_before_pixels=True)
    total_bytes = sum(p.stat().st_size for p in dcm_files)
    summary: dict[str, object] = {
        "series_instance_uid": series_uid,
        "output_dir": str(output_dir),
        "modality": str(sample.get("Modality", "Unknown")),
        "body_part": str(sample.get("BodyPartExamined", "Unknown")),
        "file_count": len(dcm_files),
        "gsps_added": gsps_path.exists(),
        "total_size_mb": round(total_bytes / (1024.0 * 1024.0), 2),
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a public de-identified TCIA DICOM series for clinical benchmarking."
    )
    parser.add_argument("--series-uid", type=str, default=DEFAULT_SERIES_UID)
    parser.add_argument("--output-dir", type=str, default="/tmp/tcia_lidc_ct")
    parser.add_argument(
        "--add-gsps",
        action="store_true",
        help="Also generate a companion GSPS object referencing the final slice.",
    )
    args = parser.parse_args()
    info = fetch_series(args.series_uid, Path(args.output_dir), add_gsps=args.add_gsps)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
