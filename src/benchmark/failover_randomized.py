"""
src/benchmark/failover_randomized.py

Randomized-phase failover, recovery, and reactive in-band rerouting benchmark:
  - Injects faults after a uniform random jitter U[0, DAEMON_POLL_INTERVAL_S) so
    detection latency is sampled uniformly across the polling phase rather than phase-locked.
  - Runs N >= 15 trials per region and failure mode (process_stop, network_partition).
  - Measures reactive in-band failover latency for an active transfer hitting a newly dead
    node during the control-plane detection window.
  - Reports mean, SD, min, max, median, and 95% confidence interval.

Usage:
    python -m src.benchmark.failover_randomized --trials 15 --output-json failover_randomized.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from src.benchmark.failover_time import (
    REGION_CONTAINERS,
    inject_failure,
    is_node_healthy,
    restore_node,
)
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_FAILOVER_RAND")

ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")
POLL_INTERVAL_S = float(os.getenv("DAEMON_POLL_INTERVAL_S", "10"))
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False


def _stats(vals: list[float]) -> dict[str, float]:
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sd": 0.0, "ci95": 0.0, "min": 0.0, "median": 0.0, "max": 0.0}
    mean = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    ci95 = 1.96 * (sd / math.sqrt(n)) if n > 1 else 0.0
    return {
        "n": n,
        "mean": round(mean, 3),
        "sd": round(sd, 3),
        "ci95": round(ci95, 3),
        "min": round(min(vals), 3),
        "median": round(statistics.median(vals), 3),
        "max": round(max(vals), 3),
    }


def run_randomized_trial(
    client: httpx.Client,
    node_id: str,
    container: str,
    mode: str,
    rng: random.Random,
    poll_s: float = 0.2,
    timeout_s: float = 35.0,
) -> tuple[float, float, float]:
    """Return (failover_detect_s, recovery_detect_s, reactive_reroute_ms)."""
    if is_node_healthy(client, node_id) is False:
        restore_node(mode, container)
        time.sleep(POLL_INTERVAL_S + 2.0)

    # Randomize phase offset relative to the daemon's fixed polling interval.
    time.sleep(rng.uniform(0.0, POLL_INTERVAL_S))

    t0 = time.monotonic()
    inject_failure(mode, container)

    # Measure immediate reactive in-band reroute via ?exclude=<failed_node>
    t_react = time.monotonic()
    reroute_resp = client.get(
        f"{ORCH_URL}/get-best-node",
        params={"agent_id": "agent-001", "exclude": node_id},
        headers={"X-API-Key": API_KEY},
        timeout=5.0,
    )
    reroute_resp.raise_for_status()
    reactive_ms = (time.monotonic() - t_react) * 1000.0

    # Measure control-plane detection via telemetry daemon
    failover_s = timeout_s
    while (time.monotonic() - t0) < timeout_s:
        if is_node_healthy(client, node_id) is False:
            failover_s = time.monotonic() - t0
            break
        time.sleep(poll_s)

    # Randomize restore offset too
    time.sleep(rng.uniform(1.0, POLL_INTERVAL_S))
    t_res = time.monotonic()
    restore_node(mode, container)
    recovery_s = timeout_s
    while (time.monotonic() - t_res) < timeout_s:
        if is_node_healthy(client, node_id) is True:
            recovery_s = time.monotonic() - t_res
            break
        time.sleep(poll_s)

    return failover_s, recovery_s, reactive_ms


def main() -> int:
    p = argparse.ArgumentParser(description="Randomized-phase failover and recovery benchmark.")
    p.add_argument("--regions", nargs="+", default=list(REGION_CONTAINERS.keys()))
    p.add_argument("--modes", nargs="+", default=["process_stop", "network_partition"])
    p.add_argument("--trials", type=int, default=15)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output-json", type=str, default="failover_randomized_results.json")
    args = p.parse_args()

    rng = random.Random(args.seed)
    summary: dict[str, Any] = {}
    with httpx.Client(verify=VERIFY_SSL) as client:
        for mode in args.modes:
            summary[mode] = {}
            for region in args.regions:
                container = REGION_CONTAINERS[region]
                fo_vals: list[float] = []
                rec_vals: list[float] = []
                react_vals: list[float] = []
                for idx in range(1, args.trials + 1):
                    fo, rec, react = run_randomized_trial(client, region, container, mode, rng)
                    fo_vals.append(fo)
                    rec_vals.append(rec)
                    react_vals.append(react)
                    log.info(
                        "[%s/%s] trial %d/%d: detect=%.2fs recover=%.2fs reactive=%.1fms",
                        mode,
                        region,
                        idx,
                        args.trials,
                        fo,
                        rec,
                        react,
                    )
                summary[mode][region] = {
                    "failover_detect_s": _stats(fo_vals),
                    "recovery_detect_s": _stats(rec_vals),
                    "reactive_inband_ms": _stats(react_vals),
                }

    Path(args.output_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
