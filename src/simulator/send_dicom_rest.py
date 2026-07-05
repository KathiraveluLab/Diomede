"""
Send a minimal 8×8 DICOM image to an Orthanc node via REST (POST /instances).

Posts raw DICOM bytes over HTTPS to the Edge Orthanc REST API, driving the
Forwarder Daemon without needing a DIMSE association.

Usage:
    python send_dicom_rest.py [options]

"""

import argparse
import io
import os
import ssl
import sys
import time

import httpx

from src.simulator.generate_dicom import make_ct_8x8, make_sized
from src.utils.logging_config import get_logger

log = get_logger(__name__, "SIMULATOR")


_DEFAULT_CA_CERT = "certs/ca.pem"


def send(
    base_url: str,
    user: str,
    password: str,
    dicom_bytes: bytes,
    ssl_ctx: ssl.SSLContext,
) -> None:

    t0 = time.monotonic()
    try:
        resp = httpx.post(
            f"{base_url}/instances",
            content=dicom_bytes,
            headers={"Content-Type": "application/dicom"},
            auth=(user, password),
            verify=ssl_ctx,
            timeout=30,
        )
    except httpx.ConnectError as exc:
        print(f"ERROR: connection failed: {exc}", file=sys.stderr)
        sys.exit(1)

    elapsed_time = (time.monotonic() - t0) * 1000

    if resp.status_code == 200:
        orthanc_status = resp.json().get("Status", "Unknown")
        print(f"REST send success → {base_url} (Orthanc: {orthanc_status}) in {elapsed_time} ms")
    else:
        print(
            f"ERROR: REST send failed, HTTP {resp.status_code}: {resp.text} in {elapsed_time} ms",
            file=sys.stderr,
        )
        sys.exit(1)


def send_batch(
    base_url: str,
    user: str,
    password: str,
    ca_cert: str,
    file_size: int | None,
    batch_size: int = 1,
    interval: float = 0.0,
) -> None:

    t0 = time.monotonic()
    ssl_ctx = ssl.create_default_context(cafile=ca_cert)

    for i in range(batch_size):
        if file_size:
            ds = make_sized(file_size)
        else:
            ds = make_ct_8x8()

        buf = io.BytesIO()
        ds.save_as(buf)
        dicom_bytes = buf.getvalue()

        send(
            base_url=base_url,
            user=user,
            password=password,
            dicom_bytes=dicom_bytes,
            ssl_ctx=ssl_ctx,
        )
        if file_size:
            log.info(f"Batch {i + 1}: send a file with {file_size} kb")
        else:
            log.info(f"Batch {i + 1}: send a 8x8 DICOM file")

        if interval > 0 and i < batch_size - 1:
            time.sleep(interval)

    elapsed_time = (time.monotonic() - t0) * 1000
    log.info(f"Finished sending batch of {batch_size} in time {elapsed_time} ms")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send 8×8 DICOM to an Orthanc node via REST POST /instances"
    )
    p.add_argument("--base-url", default="https://localhost:8042", help="Orthanc base URL")
    p.add_argument(
        "--user", default=os.environ.get("ORTHANC_USER", "orthanc"), help="Orthanc username"
    )
    p.add_argument(
        "--password",
        default=os.environ.get("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION"),
        help="Orthanc password",
    )
    p.add_argument("--ca-cert", default=_DEFAULT_CA_CERT, help="CA certificate path")
    p.add_argument("--file-size", type=int, default=None, help="Size of file sent in kb")
    p.add_argument("--batch-size", type=int, default=1, help="Number of files sent")
    p.add_argument("--interval", type=float, default=0.0, help="Time to pause in between sends")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    send_batch(
        base_url=args.base_url,
        user=args.user,
        password=args.password,
        ca_cert=args.ca_cert,
        file_size=args.file_size,
        batch_size=args.batch_size,
        interval=args.interval,
    )
