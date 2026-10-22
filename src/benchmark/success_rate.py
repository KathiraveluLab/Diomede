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
import threading
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
    os.getenv("REGION1_NAME", "us-east1"): os.getenv("NODE_US_HTTPS_URL", "https://localhost:8042"),
    os.getenv("REGION2_NAME", "eu-west1"): os.getenv("NODE_EU_HTTPS_URL", "https://localhost:8043"),
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


def upload_instance(client: httpx.Client, edge_url: str, instance_bytes: bytes) -> str | None:
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
    """Instances stored on each cloud node; -1 for a node that could not be read."""
    counts: dict[str, int] = {}
    for region, base_url in CLOUD_URLS.items():
        try:
            resp = client.get(f"{base_url}/statistics", auth=CLOUD_AUTH, timeout=5.0)
            resp.raise_for_status()
            counts[region] = resp.json().get("CountInstances", 0)
        except Exception:
            counts[region] = -1
    return counts


def get_final_instance_counts(client: httpx.Client, attempts: int = 10) -> dict[str, int]:
    """Like get_instance_counts, but waits for nodes that are still restarting."""
    counts = get_instance_counts(client)
    for _ in range(attempts - 1):
        if -1 not in counts.values():
            break
        time.sleep(1.0)
        counts = get_instance_counts(client)
    return counts


def confirmed_on_cloud(initial: dict[str, int], final: dict[str, int]) -> tuple[int, list[str]]:
    """Instances gained across the cloud nodes, and the nodes that could not be compared."""
    gained = 0
    unreadable: list[str] = []
    for region, count in final.items():
        before = initial.get(region, -1)
        if count < 0 or before < 0:
            unreadable.append(region)
        else:
            gained += max(0, count - before)
    return gained, unreadable


def docker(action: str, container: str) -> bool:
    """Run `docker <action> <container>`; True only if it succeeded."""
    try:
        result = subprocess.run(
            ["docker", action, container],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        log.error("docker %s %s could not run: %s", action, container, exc)
        return False
    if result.returncode != 0:
        log.error(
            "docker %s %s failed (exit %d): %s",
            action,
            container,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    return True


def restore_node(container: str, delay_s: float) -> None:
    time.sleep(delay_s)
    log.info("RESTORING NODE: Restarting container '%s'...", container)
    docker("start", container)


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
        node_stopped = False
        restore_thread: threading.Thread | None = None
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
            if args.simulate_outage and not outage_triggered and idx >= args.burst_count // 2:
                outage_triggered = True
                log.warning(
                    "SIMULATING OUTAGE: Stopping container '%s' for %.1f seconds...",
                    args.outage_node,
                    args.outage_duration_s,
                )
                if docker("stop", args.outage_node):
                    node_stopped = True
                    restore_thread = threading.Thread(
                        target=restore_node, args=(args.outage_node, args.outage_duration_s)
                    )
                    restore_thread.start()
                else:
                    log.error("The outage was NOT applied; this run does not test failover.")

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

        # A node stopped for the outage must be back before its count can be read
        if restore_thread is not None:
            restore_thread.join()

        t_elapsed = time.monotonic() - t_start
        final_counts = get_final_instance_counts(client)
        cleared_from_edge = len(sent_ids) - remaining
        # The edge buffer emptying only shows the forwarder let go of the instances; the cloud
        # nodes must also hold them, or the data was lost on the way.
        on_cloud, unreadable = confirmed_on_cloud(initial_counts, final_counts)
        delivered_count = min(cleared_from_edge, on_cloud)
        success_rate = (delivered_count / args.burst_count) * 100.0
        outage_applied = node_stopped if args.simulate_outage else None

        print("\n" + "=" * 60)
        print("DIOMEDE END-TO-END TRANSFER BENCHMARK REPORT")
        print("=" * 60)
        print(f"Total Sent:             {args.burst_count}")
        print(f"Accepted by Edge:       {len(sent_ids)}")
        print(f"Cleared from Edge:      {cleared_from_edge}")
        print(f"Confirmed on Cloud:     {on_cloud}")
        print(f"Successfully Delivered: {delivered_count}")
        print(f"Remaining on Edge:      {remaining}")
        print(f"Elapsed Time:           {t_elapsed:.2f} s")
        print(f"Delivery Success Rate:  {success_rate:.2f}%")
        if outage_applied is None:
            print("Mid-Transfer Outage:    False")
        else:
            print(f"Mid-Transfer Outage:    {'True' if outage_applied else 'NOT APPLIED'}")
        print("-" * 60)
        print("Final Cloud Node Distribution:")
        for region, count in final_counts.items():
            init = initial_counts.get(region, -1)
            if count < 0 or init < 0:
                print(f"  - {region:<18}: unreadable (before: {init}, after: {count})")
            else:
                print(f"  - {region:<18}: +{count - init} instances (total: {count})")
        if unreadable:
            names = ", ".join(unreadable)
            print(f"WARNING: could not compare {names}; delivery is not fully confirmed.")
        print("=" * 60 + "\n")

        if outage_applied is False:
            return 2  # the run did not test what was asked for
        if unreadable:
            return 1
        return 0 if delivered_count == args.burst_count else 1


if __name__ == "__main__":
    sys.exit(run_benchmark())
