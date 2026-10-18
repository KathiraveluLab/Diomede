"""
src/benchmark/failover_time.py

Benchmark script for quantifying failover detection time and recovery detection time.
Reproduces the 40 experimental trials across 4 regions and 2 failure modes
(process stop vs. network partition) described in Section 4.3 of the paper.
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_FAILOVER")

ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False

REGION_CONTAINERS = {
    os.getenv("REGION1_NAME", "us-east1"): os.getenv("REGION1_HOST", "orthanc-us"),
    os.getenv("REGION2_NAME", "eu-west1"): os.getenv("REGION2_HOST", "orthanc-eu"),
    os.getenv("REGION3_NAME", "asia-northeast1"): os.getenv("REGION3_HOST", "orthanc-asia"),
    os.getenv("REGION4_NAME", "af-south1"): os.getenv("REGION4_HOST", "orthanc-af"),
}

DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "diomede-net")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark failover detection and recovery times across regions "
            "and failure modes."
        )
    )
    parser.add_argument(
        "--regions",
        nargs="+",
        default=list(REGION_CONTAINERS.keys()),
        help="List of regions to evaluate (default: all 4 configured regions).",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["process_stop", "network_partition"],
        choices=["process_stop", "network_partition"],
        help="Failure modes to evaluate (default: process_stop network_partition).",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=5,
        help="Number of trials per region/mode combination (default: 5).",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=0.25,
        help="Polling interval in seconds when probing orchestrator health (default: 0.25).",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=30.0,
        help="Maximum time to wait for state transition before timing out (default: 30.0).",
    )
    return parser.parse_args()


def is_node_healthy(client: httpx.Client, node_id: str) -> bool:
    try:
        resp = client.get(
            f"{ORCH_URL}/nodes",
            headers={"X-API-Key": API_KEY},
            timeout=3.0,
        )
        if resp.status_code == 200:
            for node in resp.json():
                if node.get("node_id") == node_id:
                    return bool(node.get("healthy"))
    except Exception:
        pass
    return False


def inject_failure(mode: str, container_name: str) -> None:
    if mode == "process_stop":
        subprocess.run(["docker", "stop", container_name], check=True, stdout=subprocess.DEVNULL)
    elif mode == "network_partition":
        subprocess.run(
            ["docker", "network", "disconnect", DOCKER_NETWORK, container_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def restore_node(mode: str, container_name: str) -> None:
    if mode == "process_stop":
        subprocess.run(["docker", "start", container_name], check=True, stdout=subprocess.DEVNULL)
    elif mode == "network_partition":
        subprocess.run(
            ["docker", "network", "connect", DOCKER_NETWORK, container_name],
            check=True,
            stdout=subprocess.DEVNULL,
        )


def run_single_trial(
    client: httpx.Client,
    node_id: str,
    container_name: str,
    mode: str,
    poll_interval: float,
    timeout_s: float,
) -> tuple[float, float]:
    # 1. Ensure node starts healthy
    if not is_node_healthy(client, node_id):
        log.warning("Node %s is not healthy before trial; restoring and waiting...", node_id)
        restore_node(mode, container_name)
        time.sleep(12.0)

    # 2. Inject failure and measure detection time
    t0 = time.monotonic()
    inject_failure(mode, container_name)
    failover_detected = False
    failover_time = timeout_s

    while (time.monotonic() - t0) < timeout_s:
        if not is_node_healthy(client, node_id):
            failover_time = time.monotonic() - t0
            failover_detected = True
            break
        time.sleep(poll_interval)

    if not failover_detected:
        log.error("Failover detection timed out for %s under %s", node_id, mode)

    # 3. Restore node and measure recovery time
    time.sleep(2.0)  # Brief pause before restoration
    t_restore = time.monotonic()
    restore_node(mode, container_name)
    recovery_detected = False
    recovery_time = timeout_s

    while (time.monotonic() - t_restore) < timeout_s:
        if is_node_healthy(client, node_id):
            recovery_time = time.monotonic() - t_restore
            recovery_detected = True
            break
        time.sleep(poll_interval)

    if not recovery_detected:
        log.error("Recovery detection timed out for %s under %s", node_id, mode)

    # Settle period between trials
    time.sleep(3.0)
    return failover_time, recovery_time


def main() -> int:
    args = parse_args()
    print("\n" + "=" * 70)
    print("DIOMEDE FAILOVER & RECOVERY DETECTION BENCHMARK")
    print(f"Regions: {args.regions}")
    print(f"Modes:   {args.modes}")
    print(f"Trials:  {args.trials} per permutation")
    print("=" * 70)

    results: dict[str, dict[str, dict[str, list[float]]]] = {}

    with httpx.Client(verify=VERIFY_SSL) as client:
        for mode in args.modes:
            results[mode] = {}
            for region in args.regions:
                container = REGION_CONTAINERS.get(region)
                if not container:
                    log.warning("No container found for region %s, skipping", region)
                    continue

                results[mode][region] = {"failover": [], "recovery": []}
                print(f"\n--- Testing Region: {region:<16} Mode: {mode} ({args.trials} trials) ---")

                for trial_idx in range(1, args.trials + 1):
                    fo_time, rec_time = run_single_trial(
                        client, region, container, mode, args.poll_interval_s, args.timeout_s
                    )
                    results[mode][region]["failover"].append(fo_time)
                    print(
                        f"  Trial {trial_idx}/{args.trials}: "
                        f"Failover = {fo_time:5.2f} s | "
                        f"Recovery = {rec_time:5.2f} s"
                    )

    # Print summary table
    print("\n" + "=" * 76)
    print(
        f"{'Region':<18} | {'Mode':<18} | "
        f"{'Failover (Mean±Std)':<18} | {'Recovery (Mean±Std)':<18}"
    )
    print("-" * 76)

    for mode, reg_dict in results.items():
        for region, metrics in reg_dict.items():
            fo_vals = metrics["failover"]
            rec_vals = metrics["recovery"]
            fo_mean = statistics.mean(fo_vals) if fo_vals else 0
            fo_std = statistics.stdev(fo_vals) if len(fo_vals) > 1 else 0
            rec_mean = statistics.mean(rec_vals) if rec_vals else 0
            rec_std = statistics.stdev(rec_vals) if len(rec_vals) > 1 else 0
            print(
                f"{region:<18} | {mode:<18} | "
                f"{fo_mean:5.2f} ± {fo_std:4.2f} s       | "
                f"{rec_mean:5.2f} ± {rec_std:4.2f} s"
            )

    print("=" * 76 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
