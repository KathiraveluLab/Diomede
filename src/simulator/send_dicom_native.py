"""
Send a minimal 8×8 DICOM image to an Orthanc node via DIMSE-TLS (C-STORE).

Usage:
    python send_dicom_native.py [options]

"""

import argparse
import os
import ssl
import sys

from pydicom import uid as dcm_uid
from pynetdicom import AE

from src.simulator.generate_dicom import make_ct_8x8

SecondaryCaptureImageStorage = dcm_uid.SecondaryCaptureImageStorage

_DEFAULT_CA_CERT = "certs/ca.pem"
_DEFAULT_CLIENT_CERT = "certs/diomede-client/client.crt"
_DEFAULT_CLIENT_KEY = "certs/diomede-client/client.key"


def send(
    host: str,
    port: int,
    called_aet: str,
    calling_aet: str,
    ca_cert: str,
    client_cert: str,
    client_key: str,
) -> None:
    ds = make_ct_8x8()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.load_verify_locations(ca_cert)
    ssl_ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    ssl_ctx.check_hostname = False

    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(SecondaryCaptureImageStorage)

    assoc = ae.associate(host, port, ae_title=called_aet, tls_args=(ssl_ctx, host))
    if not assoc.is_established:
        print(f"ERROR: association rejected by {called_aet} at {host}:{port}", file=sys.stderr)
        sys.exit(1)

    try:
        status = assoc.send_c_store(ds)
        if status and status.Status == 0x0000:
            print(f"C-STORE success → {called_aet} at {host}:{port}")
        else:
            if status:
                print(f"ERROR: C-STORE failed, status=0x{status.Status:04X}", file=sys.stderr)
            else:
                print("ERROR: C-STORE failed, no status returned", file=sys.stderr)
            sys.exit(1)
    finally:
        assoc.release()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send 8×8 DICOM to an Orthanc node via DIMSE-TLS")
    p.add_argument(
        "--host",
        default=os.environ.get("ORTHANC_DICOM_HOST", "localhost"),
        help="Orthanc DICOM host",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ORTHANC_DICOM_PORT", "4242")),
        help="Orthanc DICOM port",
    )
    p.add_argument(
        "--called-aet",
        default=os.environ.get("ORTHANC_CALLED_AET", "Orthanc_US"),
        help="Called AET (remote)",
    )
    p.add_argument(
        "--calling-aet",
        default=os.environ.get("ORTHANC_CALLING_AET", "Simulator"),
        help="Calling AET (ours)",
    )
    p.add_argument("--ca-cert", default=_DEFAULT_CA_CERT, help="CA certificate path")
    p.add_argument("--client-cert", default=_DEFAULT_CLIENT_CERT, help="Client certificate path")
    p.add_argument("--client-key", default=_DEFAULT_CLIENT_KEY, help="Client private-key path")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    send(
        host=args.host,
        port=args.port,
        called_aet=args.called_aet,
        calling_aet=args.calling_aet,
        ca_cert=args.ca_cert,
        client_cert=args.client_cert,
        client_key=args.client_key,
    )
