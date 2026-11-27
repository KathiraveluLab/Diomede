"""
src/benchmark/baseline_comparison.py

Four-way baseline comparison benchmark addressing JIIM Reviewer #1 (#1),
Reviewer #2 (#4), and Reviewer #4 (Major #1):
  (a) Static endpoint (direct fixed cloud Orthanc node)
  (b) Round-robin dynamic routing across the 4 cloud nodes
  (c) Least-connections / HAProxy active-health-checked reverse proxy (port 8049)
  (d) Diomede telemetry-driven orchestration (via Edge Agent in rest / stow-rs / dimse)

Measures under identical multi-slice DICOM workloads (normal WAN load and mid-run node
outage):
  - Study-level delivery completion time (mean +/- SD, median, p95)
  - Aggregate throughput (MB/s)
  - Failed and retried transfers
  - Split-study rate across cloud nodes
  - Node load balance (Jain's fairness index across destination nodes)

Usage:
    docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build
    python -m src.benchmark.baseline_comparison --studies 40 --slices-per-study 10 --slice-kb 256
"""

from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from pydicom.uid import generate_uid

from src.simulator.generate_dicom import make_sized
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_BASELINES")

AUTH = (os.getenv("ORTHANC_USER", "orthanc"), os.getenv("ORTHANC_PASSWORD", "orthanc"))
EDGE_URL = os.getenv("EDGE_AGENT1_HTTPS_URL", "https://localhost:8046")
HAPROXY_URL = os.getenv("HAPROXY_HTTPS_URL", "https://localhost:8049")
CLOUD_URLS: dict[str, str] = {
    "us-east1": os.getenv("NODE_US_HTTPS_URL", "https://localhost:8042"),
    "eu-west1": os.getenv("NODE_EU_HTTPS_URL", "https://localhost:8043"),
    "asia-northeast1": os.getenv("NODE_ASIA_HTTPS_URL", "https://localhost:8044"),
    "af-south1": os.getenv("NODE_AF_HTTPS_URL", "https://localhost:8045"),
}
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False


def jains_fairness(counts: list[int]) -> float:
    """Compute Jain's fairness index J = (sum x)^2 / (n * sum x^2) in [1/n, 1]."""
    n = len(counts)
    if n == 0 or sum(counts) == 0:
        return 0.0
    s = float(sum(counts))
    sq = float(sum(x * x for x in counts))
    return (s * s) / (n * sq)


def build_study_payloads(
    num_studies: int, slices_per_study: int, slice_kb: int
) -> list[tuple[str, list[bytes]]]:
    """Generate synthetic multi-slice DICOM studies sharing a StudyInstanceUID per study."""
    studies: list[tuple[str, list[bytes]]] = []
    for _ in range(num_studies):
        study_uid = str(generate_uid())
        series_uid = str(generate_uid())
        slices: list[bytes] = []
        for idx in range(slices_per_study):
            ds = make_sized(slice_kb)
            ds.StudyInstanceUID = study_uid
            ds.SeriesInstanceUID = series_uid
            ds.SOPInstanceUID = str(generate_uid())
            ds.InstanceNumber = idx + 1
            buf = io.BytesIO()
            ds.save_as(buf, write_like_original=False)
            slices.append(buf.getvalue())
        studies.append((study_uid, slices))
    return studies


def _schedule_outage(container: str, delay_s: float, duration_s: float) -> threading.Thread:
    def _worker() -> None:
        time.sleep(delay_s)
        log.warning("Injecting outage: stopping %s for %.1fs", container, duration_s)
        subprocess.run(["docker", "stop", container], check=False, stdout=subprocess.DEVNULL)
        time.sleep(duration_s)
        log.info("Restoring %s", container)
        subprocess.run(["docker", "start", container], check=False, stdout=subprocess.DEVNULL)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _run_direct_strategy(
    name: str,
    studies: list[tuple[str, list[bytes]]],
    target_urls: list[str],
    concurrency: int,
    max_retries: int = 2,
) -> dict[str, Any]:
    """Run static, round-robin, or HAProxy strategy by posting study slices over HTTPS."""
    study_times: list[float] = []
    per_target_counts: dict[str, int] = {u: 0 for u in target_urls}
    failed_studies = 0
    total_retries = 0
    total_bytes = 0
    t_run_start = time.monotonic()

    def _send_one_study(idx: int, slices: list[bytes]) -> tuple[bool, float, int, int, str]:
        target = target_urls[idx % len(target_urls)]
        t0 = time.monotonic()
        retries = 0
        sent_bytes = 0
        ok = True
        with httpx.Client(verify=VERIFY_SSL) as client:
            for payload in slices:
                delivered = False
                for attempt in range(max_retries + 1):
                    try:
                        resp = client.post(
                            f"{target}/instances",
                            content=payload,
                            headers={"Content-Type": "application/dicom"},
                            auth=AUTH,
                            timeout=30.0,
                        )
                        resp.raise_for_status()
                        delivered = True
                        sent_bytes += len(payload)
                        break
                    except Exception:
                        if attempt < max_retries:
                            retries += 1
                            time.sleep(0.5)
                if not delivered:
                    ok = False
        return ok, time.monotonic() - t0, retries, sent_bytes, target

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(_send_one_study, i, slices) for i, (_, slices) in enumerate(studies)]
        for fut in as_completed(futs):
            ok, dt, retries, nbytes, target = fut.result()
            total_retries += retries
            total_bytes += nbytes
            if ok:
                study_times.append(dt)
                per_target_counts[target] = per_target_counts.get(target, 0) + 1
            else:
                failed_studies += 1

    elapsed = time.monotonic() - t_run_start
    return _summarize(
        name,
        len(studies),
        study_times,
        failed_studies,
        total_retries,
        total_bytes,
        elapsed,
        list(per_target_counts.values()),
    )


def _run_diomede_strategy(
    name: str,
    studies: list[tuple[str, list[bytes]]],
    drain_timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Ingest studies into Edge Orthanc and measure full end-to-end delivery across cloud nodes."""
    sent_by_study: dict[str, list[str]] = {}
    t_submit: dict[str, float] = {}
    total_bytes = 0
    t_run_start = time.monotonic()

    with httpx.Client(verify=VERIFY_SSL) as client:
        for study_uid, slices in studies:
            t_submit[study_uid] = time.monotonic()
            inst_ids: list[str] = []
            for payload in slices:
                resp = client.post(
                    f"{EDGE_URL}/instances",
                    content=payload,
                    headers={"Content-Type": "application/dicom"},
                    auth=AUTH,
                    timeout=30.0,
                )
                resp.raise_for_status()
                inst_id = resp.json().get("ID")
                if inst_id:
                    inst_ids.append(inst_id)
                total_bytes += len(payload)
            sent_by_study[study_uid] = inst_ids

        # Track study completion time as instances disappear from Edge and appear on Cloud
        completed_times: list[float] = []
        pending = dict(sent_by_study)
        deadline = time.monotonic() + drain_timeout_s
        while pending and time.monotonic() < deadline:
            try:
                edge_ids = set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
            except Exception:
                time.sleep(0.5)
                continue
            now = time.monotonic()
            for suid in list(pending.keys()):
                if not any(iid in edge_ids for iid in pending[suid]):
                    completed_times.append(now - t_submit[suid])
                    del pending[suid]
            if pending:
                time.sleep(0.5)

        # Verify cloud locations and check for split studies
        per_node_studies: dict[str, int] = {r: 0 for r in CLOUD_URLS}
        split_studies = 0
        missing_studies = 0
        for _suid, inst_ids in sent_by_study.items():
            nodes_seen: set[str] = set()
            found_count = 0
            for iid in inst_ids:
                for region, url in CLOUD_URLS.items():
                    try:
                        r = client.get(f"{url}/instances/{iid}", auth=AUTH, timeout=5.0)
                        if r.status_code == 200:
                            nodes_seen.add(region)
                            found_count += 1
                            break
                    except Exception:
                        continue
            if found_count < len(inst_ids):
                missing_studies += 1
            elif len(nodes_seen) > 1:
                split_studies += 1
                for r in nodes_seen:
                    per_node_studies[r] += 1
            elif len(nodes_seen) == 1:
                only = next(iter(nodes_seen))
                per_node_studies[only] += 1

    elapsed = time.monotonic() - t_run_start
    summary = _summarize(
        name,
        len(studies),
        completed_times,
        missing_studies,
        0,
        total_bytes,
        elapsed,
        list(per_node_studies.values()),
    )
    summary["split_studies"] = split_studies
    summary["per_node_studies"] = per_node_studies
    return summary


def _summarize(
    strategy: str,
    total_studies: int,
    study_times: list[float],
    failed: int,
    retries: int,
    total_bytes: int,
    elapsed_s: float,
    node_counts: list[int],
) -> dict[str, Any]:
    sorted_t = sorted(study_times)
    p95 = sorted_t[int(0.95 * (len(sorted_t) - 1))] if sorted_t else 0.0
    return {
        "strategy": strategy,
        "total_studies": total_studies,
        "delivered_studies": total_studies - failed,
        "failed_studies": failed,
        "delivery_rate_pct": round(100.0 * (total_studies - failed) / max(total_studies, 1), 2),
        "retries": retries,
        "study_time_mean_s": round(statistics.mean(study_times), 3) if study_times else 0.0,
        "study_time_sd_s": round(statistics.stdev(study_times), 3) if len(study_times) > 1 else 0.0,
        "study_time_median_s": round(statistics.median(study_times), 3) if study_times else 0.0,
        "study_time_p95_s": round(p95, 3),
        "throughput_mb_s": round((total_bytes / (1024.0 * 1024.0)) / max(elapsed_s, 1e-6), 2),
        "jains_fairness": round(jains_fairness(node_counts), 4),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Compare Static, Round-Robin, HAProxy, and Diomede.")
    p.add_argument("--studies", type=int, default=40)
    p.add_argument("--slices-per-study", type=int, default=10)
    p.add_argument("--slice-kb", type=int, default=256)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--simulate-outage", action="store_true")
    p.add_argument("--outage-node", type=str, default="orthanc-us")
    p.add_argument("--outage-duration-s", type=float, default=20.0)
    p.add_argument("--output-json", type=str, default="baseline_comparison_results.json")
    args = p.parse_args()

    studies = build_study_payloads(args.studies, args.slices_per_study, args.slice_kb)
    results: list[dict[str, Any]] = []

    scenarios = [
        (
            "static_us",
            lambda: _run_direct_strategy(
                "static_us", studies, [CLOUD_URLS["us-east1"]], args.concurrency
            ),
        ),
        (
            "round_robin",
            lambda: _run_direct_strategy(
                "round_robin", studies, list(CLOUD_URLS.values()), args.concurrency
            ),
        ),
        (
            "haproxy_leastconn",
            lambda: _run_direct_strategy(
                "haproxy_leastconn", studies, [HAPROXY_URL], args.concurrency
            ),
        ),
        ("diomede", lambda: _run_diomede_strategy("diomede", studies)),
    ]

    for label, fn in scenarios:
        log.info("=== Running baseline scenario: %s (outage=%s) ===", label, args.simulate_outage)
        outage_thread = None
        if args.simulate_outage:
            outage_thread = _schedule_outage(
                args.outage_node, delay_s=3.0, duration_s=args.outage_duration_s
            )
        res = fn()
        if outage_thread is not None:
            outage_thread.join()
            time.sleep(5.0)
        results.append(res)
        print(json.dumps(res, indent=2))

    Path(args.output_json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Wrote baseline comparison results to %s", args.output_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
