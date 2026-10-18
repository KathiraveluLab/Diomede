"""
src/benchmark/success_rate.py

Benchmark script for evaluating end-to-end DICOM transfer success rate.
Reproduces the 1,000 baseline transfers and 1,000 mid-transfer outage transfers
described in Section 4.4 (End-to-End Transfer Success) of the paper.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

from src.simulator.generate_dicom import make_sized
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_SUCCESS")

DEFAULT_EDGE_URL = os.getenv("EDGE_AGENT1_HTTPS_URL", "https://localhost:8046")
EDGE_AUTH = (
    os.getenv("EDGE_USER", os.getenv("ORTHANC_USER", "orthanc")),
    os.getenv("EDGE_PASS", os.getenv("ORTHANC_PASSWORD", "orthanc")),
)
CLOUD_AUTH = (
    os.getenv("ORTHANC_USER", "orthanc"),
    os.getenv("ORTHANC_PASSWORD", "orthanc"),
)
CLOUD_URLS: dict[str, str] = {
    os.getenv("REGION1_NAME", "us-east1"): os.getenv(
        "NODE_US_HTTPS_URL", "https://localhost:8042"
    ),
    os.getenv("REGION2_NAME", "eu-west1"): os.getenv(
        "NODE_EU_HTTPS_URL", "https://localhost:8043"
    ),
    os.getenv("REGION3_NAME", "asia-northeast1"): os.getenv(
        "NODE_ASIA_HTTPS_URL", "https://localhost:8044"
    ),
    os.getenv("REGION4_NAME", "af-south1"): os.getenv(
        "NODE_AF_HTTPS_URL", "https://localhost:8045"
    ),
}

CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark end-to-end DICOM transfer delivery success rate."
    )
    parser.add_argument(
        "--burst-count",
        type=int,
        default=int(os.getenv("LOAD_BURST_COUNT", "1000")),
        help="Number of synthetic DICOM studies/instances to transmit (default: 1000).",
    )
    parser.add_argument(
        "--file-size-kb",
        type=int,
        default=int(os.getenv("LOAD_FILE_SIZE_KB", "50")),
        help="Size of each synthetic DICOM instance in KB (default: 50).",
    )
    parser.add_argument(
        "--simulate-outage",
        action="store_true",
        help="Simulate a mid-transfer node outage during benchmark execution.",
    )
    parser.add_argument(
        "--outage-duration-s",
        type=float,
        default=15.0,
        help="Duration of the mid-transfer outage in seconds (default: 15.0).",
    )
    parser.add_argument(
        "--outage-node",
        type=str,
        default="orthanc-us",
        help="Docker container name of the cloud node to stop mid-transfer (default: orthanc-us).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=120.0,
        help="Maximum wait time for all studies to clear the edge buffer (default: 120.0).",
    )
    return parser.parse_args()


def upload_instance(
    client: httpx.Client, edge_url: str, instance_bytes: bytes
) -> str | None:
    try:
        resp = client.post(
            f"{edge_url}/instances",
            content=instance_bytes,
            headers={"Content-Type": "application/dicom"},
            auth=EDGE_AUTH,
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json().get("ID")
    except Exception as exc:
        log.error("Failed to upload instance to edge: %s", exc)
        return None


def get_instance_counts(client: httpx.Client) -> dict[str, int]:
    counts: dict[str, int] = {}
    for region, base_url in CLOUD_URLS.items():
        try:
            resp = client.get(f"{base_url}/statistics", auth=CLOUD_AUTH, timeout=5.0)
            if resp.status_code == 200:
                counts[region] = resp.json().get("CountInstances", 0)
        except Exception:
            counts[region] = -1
    return counts


def run_benchmark() -> int:
    args = parse_args()
    log.info(
        "Starting End-to-End Success Rate Benchmark: %d instances (%d KB each), outage=%s",
        args.burst_count,
        args.file_size_kb,
        args.simulate_outage,
    )

    with httpx.Client(verify=VERIFY_SSL) as client:
        initial_counts = get_instance_counts(client)
        log.info("Initial cloud node instance counts: %s", initial_counts)

        sent_ids: list[str] = []
        outage_triggered = False
        t_start = time.monotonic()

        for idx in range(1, args.burst_count + 1):
            ds = make_sized(args.file_size_kb)
            buf = io.BytesIO()
            ds.save_as(buf, write_like_original=False)
            dicom_bytes = buf.getvalue()

            inst_id = upload_instance(client, DEFAULT_EDGE_URL, dicom_bytes)
            if inst_id:
                sent_ids.append(inst_id)

            # Trigger mid-transfer outage at 50% completion if requested
            if (
                args.simulate_outage
                and not outage_triggered
                and idx >= args.burst_count // 2
            ):
                outage_triggered = True
                log.warning(
                    "SIMULATING OUTAGE: Stopping container '%s' for %.1f seconds...",
                    args.outage_node,
                    args.outage_duration_s,
                )
                subprocess.run(
                    ["docker", "stop", args.outage_node],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                def restore_node() -> None:
                    time.sleep(args.outage_duration_s)
                    log.info("RESTORING NODE: Restarting container '%s'...", args.outage_node)
                    subprocess.run(
                        ["docker", "start", args.outage_node],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

                import threading

                threading.Thread(target=restore_node, daemon=True).start()

        log.info(
            "Ingestion complete: %d/%d instances accepted by edge. Waiting for pipeline drain...",
            len(sent_ids),
            args.burst_count,
        )

        # Poll edge buffer until all sent instances are forwarded and cleared
        drain_deadline = time.monotonic() + args.timeout_s
        remaining = len(sent_ids)
        while time.monotonic() < drain_deadline:
            try:
                resp = client.get(f"{DEFAULT_EDGE_URL}/instances", auth=EDGE_AUTH, timeout=5.0)
                if resp.status_code == 200:
                    buffered_ids = set(resp.json())
                    remaining = len(set(sent_ids).intersection(buffered_ids))
                    if remaining == 0:
                        break
            except Exception as exc:
                log.warning("Edge polling transient error: %s", exc)
            time.sleep(1.0)

        t_elapsed = time.monotonic() - t_start
        final_counts = get_instance_counts(client)
        delivered_count = len(sent_ids) - remaining
        success_rate = (delivered_count / args.burst_count) * 100.0

        print("\n" + "=" * 60)
        print("DIOMEDE END-TO-END TRANSFER BENCHMARK REPORT")
        print("=" * 60)
        print(f"Total Sent:             {args.burst_count}")
        print(f"Accepted by Edge:       {len(sent_ids)}")
        print(f"Successfully Delivered: {delivered_count}")
        print(f"Remaining on Edge:      {remaining}")
        print(f"Elapsed Time:           {t_elapsed:.2f} s")
        print(f"Delivery Success Rate:  {success_rate:.2f}%")
        print(f"Mid-Transfer Outage:    {args.simulate_outage}")
        print("-" * 60)
        print("Final Cloud Node Distribution:")
        for region, count in final_counts.items():
            init = initial_counts.get(region, 0)
            delta = count - init if count >= 0 and init >= 0 else 0
            print(f"  - {region:<18}: +{delta} instances (total: {count})")
        print("=" * 60 + "\n")

        return 0 if delivered_count == args.burst_count else 1


if __name__ == "__main__":
    sys.exit(run_benchmark())
