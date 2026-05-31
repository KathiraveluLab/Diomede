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

import httpx

from src.simulator.generate_dicom import make_ct_8x8

_DEFAULT_CA_CERT = "certs/ca.pem"


def send(
    base_url: str,
    user: str,
    password: str,
    ca_cert: str,
) -> None:
    ds = make_ct_8x8()

    buf = io.BytesIO()
    ds.save_as(buf)
    dicom_bytes = buf.getvalue()

    ssl_ctx = ssl.create_default_context(cafile=ca_cert)

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

    if resp.status_code == 200:
        orthanc_status = resp.json().get("Status", "Unknown")
        print(f"REST send success → {base_url} (Orthanc: {orthanc_status})")
    else:
        print(
            f"ERROR: REST send failed, HTTP {resp.status_code}: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)


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
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    send(
        base_url=args.base_url,
        user=args.user,
        password=args.password,
        ca_cert=args.ca_cert,
    )
