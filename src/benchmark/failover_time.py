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

from src.utils.ca_bundle import ca_ssl_context
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_FAILOVER")

ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")

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
            "Benchmark failover detection and recovery times across regions and failure modes."
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


def is_node_healthy(client: httpx.Client, node_id: str) -> bool | None:
    """None when the orchestrator can't be asked, which says nothing about the node."""
    try:
        resp = client.get(
            f"{ORCH_URL}/nodes",
            headers={"X-API-Key": API_KEY},
            timeout=3.0,
        )
        resp.raise_for_status()
        nodes = resp.json()
    except Exception as exc:
        log.warning("Could not read node health from the orchestrator: %s", exc)
        return None
    for node in nodes:
        if node.get("node_id") == node_id:
            return bool(node.get("healthy"))
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
) -> tuple[float | None, float | None]:
    """Time one failure and recovery; a time is None when it was not seen within timeout_s."""
    # 1. Ensure node starts healthy
    if is_node_healthy(client, node_id) is False:
        log.warning("Node %s is not healthy before trial; restoring and waiting...", node_id)
        restore_node(mode, container_name)
        time.sleep(12.0)

    # 2. Inject failure and measure detection time. The node is restored even if something
    # fails or the run is interrupted in between.
    failover_time: float | None = None
    t0 = time.monotonic()
    try:
        inject_failure(mode, container_name)

        while (time.monotonic() - t0) < timeout_s:
            if is_node_healthy(client, node_id) is False:
                failover_time = time.monotonic() - t0
                break
            time.sleep(poll_interval)

        if failover_time is None:
            log.error("Failover detection timed out for %s under %s", node_id, mode)

        time.sleep(2.0)  # Brief pause before restoration
    finally:
        # 3. Restore node and measure recovery time
        t_restore = time.monotonic()
        restore_node(mode, container_name)

    recovery_time: float | None = None
    if failover_time is not None:  # a node never seen down has no recovery to time
        while (time.monotonic() - t_restore) < timeout_s:
            if is_node_healthy(client, node_id):
                recovery_time = time.monotonic() - t_restore
                break
            time.sleep(poll_interval)

        if recovery_time is None:
            log.error("Recovery detection timed out for %s under %s", node_id, mode)

    # Settle period between trials
    time.sleep(3.0)
    return failover_time, recovery_time


def _format_time(seconds: float | None) -> str:
    return "timed out" if seconds is None else f"{seconds:5.2f} s"


def _mean_std(times: list[float | None]) -> str:
    """Mean ± std of the times that were measured; timed-out trials (None) are left out."""
    measured = [t for t in times if t is not None]
    if not measured:
        return "n/a"
    std = statistics.stdev(measured) if len(measured) > 1 else 0
    return f"{statistics.mean(measured):5.2f} ± {std:4.2f} s"


def main() -> int:
    args = parse_args()
    print("\n" + "=" * 70)
    print("DIOMEDE FAILOVER & RECOVERY DETECTION BENCHMARK")
    print(f"Regions: {args.regions}")
    print(f"Modes:   {args.modes}")
    print(f"Trials:  {args.trials} per permutation")
    print("=" * 70)

    results: dict[str, dict[str, dict[str, list[float | None]]]] = {}

    with httpx.Client(verify=ca_ssl_context()) as client:
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
                    results[mode][region]["recovery"].append(rec_time)
                    # No recovery is timed for a failover that was never seen.
                    rec_text = "not measured" if fo_time is None else _format_time(rec_time)
                    print(
                        f"  Trial {trial_idx}/{args.trials}: "
                        f"Failover = {_format_time(fo_time)} | "
                        f"Recovery = {rec_text}"
                    )

    # Print summary table
    header = (
        f"{'Region':<18} | {'Mode':<18} | {'Failover (Mean±Std)':<18} | "
        f"{'Recovery (Mean±Std)':<18} | Timed out (failover, recovery)"
    )
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))

    timed_out = 0
    for mode, reg_dict in results.items():
        for region, metrics in reg_dict.items():
            fo_vals = metrics["failover"]
            rec_vals = metrics["recovery"]
            fo_timeouts = fo_vals.count(None)
            # Recovery is only timed after a failover was seen, so only those can time out.
            rec_attempts = len(fo_vals) - fo_timeouts
            rec_timeouts = rec_vals.count(None) - fo_timeouts
            timed_out += fo_timeouts + rec_timeouts
            print(
                f"{region:<18} | {mode:<18} | "
                f"{_mean_std(fo_vals):<19} | "
                f"{_mean_std(rec_vals):<19} | "
                f"{fo_timeouts}/{len(fo_vals)}, {rec_timeouts}/{rec_attempts}"
            )

    print("=" * len(header) + "\n")
    if timed_out:
        log.error(
            "%d trial(s) timed out and are not in the statistics above; "
            "raise --timeout-s or check that the orchestrator is reachable.",
            timed_out,
        )
    return 1 if timed_out else 0


if __name__ == "__main__":
    sys.exit(main())
