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


def wait_for_cloud_nodes(client: httpx.Client, attempts: int = 10) -> list[str]:
    """Wait until every cloud node answers (one may still be restarting after the outage).

    Returns the nodes that still do not answer after `attempts` tries.
    """
    unreachable = list(CLOUD_URLS)
    for attempt in range(attempts):
        unreachable = []
        for region, base_url in CLOUD_URLS.items():
            try:
                client.get(
                    f"{base_url}/statistics", auth=CLOUD_AUTH, timeout=5.0
                ).raise_for_status()
            except Exception:
                unreachable.append(region)
        if not unreachable:
            break
        if attempt < attempts - 1:
            time.sleep(1.0)
    return unreachable


def locate_on_cloud(
    client: httpx.Client, instance_ids: list[str]
) -> tuple[dict[str, int], list[str], set[str]]:
    """Find each sent instance on the cloud nodes by its Orthanc ID.

    Orthanc derives an instance ID from the DICOM identifiers, so the ID the edge returned is
    also the ID of that instance on whichever cloud node stores it. Unlike comparing instance
    counts, this only credits the instances this run sent.

    Returns the instances found per node, the IDs found on no node, and the nodes that could
    not be queried (an ID missing while a node is unreadable may still be stored there).
    """
    found_per_node = {region: 0 for region in CLOUD_URLS}
    missing: list[str] = []
    unreadable: set[str] = set()
    for instance_id in instance_ids:
        for region, base_url in CLOUD_URLS.items():
            try:
                resp = client.get(
                    f"{base_url}/instances/{instance_id}", auth=CLOUD_AUTH, timeout=5.0
                )
            except Exception:
                unreadable.add(region)
                continue
            if resp.status_code == 200:
                found_per_node[region] += 1
                break
            if resp.status_code != 404:
                unreadable.add(region)
        else:
            missing.append(instance_id)
    return found_per_node, missing, unreadable


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

        # A node stopped for the outage must be back before the instances are looked up on it
        if restore_thread is not None:
            restore_thread.join()
        not_answering = wait_for_cloud_nodes(client)

        t_elapsed = time.monotonic() - t_start
        cleared_from_edge = len(sent_ids) - remaining
        # The edge buffer emptying only shows the forwarder let go of the instances; each one
        # must also be stored on a cloud node, or the data was lost on the way.
        found_per_node, missing, unreadable = locate_on_cloud(client, sent_ids)
        delivered_count = len(sent_ids) - len(missing)
        success_rate = (delivered_count / args.burst_count) * 100.0
        outage_applied = node_stopped if args.simulate_outage else None

        print("\n" + "=" * 60)
        print("DIOMEDE END-TO-END TRANSFER BENCHMARK REPORT")
        print("=" * 60)
        print(f"Total Sent:             {args.burst_count}")
        print(f"Accepted by Edge:       {len(sent_ids)}")
        print(f"Cleared from Edge:      {cleared_from_edge}")
        print(f"Confirmed on Cloud:     {delivered_count}")
        print(f"Missing from Cloud:     {len(missing)}")
        print(f"Remaining on Edge:      {remaining}")
        print(f"Elapsed Time:           {t_elapsed:.2f} s")
        print(f"Delivery Success Rate:  {success_rate:.2f}%")
        if outage_applied is None:
            print("Mid-Transfer Outage:    False")
        else:
            print(f"Mid-Transfer Outage:    {'True' if outage_applied else 'NOT APPLIED'}")
        print("-" * 60)
        print("Where the sent instances were found:")
        for region, count in found_per_node.items():
            print(f"  - {region:<18}: {count} instances")
        silent = sorted(set(not_answering) | unreadable)
        if silent and missing:
            names = ", ".join(silent)
            print(f"WARNING: {names} could not be queried; some missing instances may be there.")
        print("=" * 60 + "\n")

        if outage_applied is False:
            return 2  # the run did not test what was asked for
        return 0 if delivered_count == args.burst_count else 1


if __name__ == "__main__":
    sys.exit(run_benchmark())
