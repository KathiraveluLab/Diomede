"""
src/benchmark/baseline_comparison.py

Four-way comparative routing benchmark evaluating:
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
        time.sleep(2.0)
        # Re-apply WAN tc netem profile wiped when the container stopped.
        if Path("scripts/inject_latency.sh").exists():
            subprocess.run(
                ["bash", "scripts/inject_latency.sh"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _audit_cloud_studies(
    sent_by_study: dict[str, list[str]],
    expected_slices_per_study: int,
) -> tuple[dict[str, int], int, int]:
    """Inspect all 4 cloud Orthanc nodes to count per-node distribution and split studies."""
    per_node_studies: dict[str, int] = {r: 0 for r in CLOUD_URLS}
    split_studies = 0
    missing_studies = 0
    with httpx.Client(verify=VERIFY_SSL) as client:
        cloud_sets: dict[str, set[str]] = {}
        for region, url in CLOUD_URLS.items():
            try:
                cloud_sets[region] = set(
                    client.get(f"{url}/instances", auth=AUTH, timeout=15.0).json()
                )
            except Exception:
                cloud_sets[region] = set()
        for _suid, inst_ids in sent_by_study.items():
            # A study is complete (not split) if at least one single cloud node holds all slices.
            complete_nodes = [
                region
                for region, ids in cloud_sets.items()
                if len(inst_ids) >= expected_slices_per_study
                and all(iid in ids for iid in inst_ids)
            ]
            if complete_nodes:
                per_node_studies[complete_nodes[0]] += 1
                continue
            nodes_seen: set[str] = set()
            found_count = 0
            for iid in inst_ids:
                for region, ids in cloud_sets.items():
                    if iid in ids:
                        nodes_seen.add(region)
                        found_count += 1
                        break
            if found_count < expected_slices_per_study:
                missing_studies += 1
            elif len(nodes_seen) > 1:
                split_studies += 1
                for r in nodes_seen:
                    per_node_studies[r] += 1
            elif len(nodes_seen) == 1:
                only = next(iter(nodes_seen))
                per_node_studies[only] += 1
    return per_node_studies, split_studies, missing_studies


def _run_direct_strategy(
    name: str,
    studies: list[tuple[str, list[bytes]]],
    target_urls: list[str],
    concurrency: int,
    max_retries: int = 2,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Run static, round-robin, or HAProxy strategy by posting study slices over HTTPS."""
    study_times: list[float] = []
    sent_by_study: dict[str, list[str]] = {}
    failed_studies = 0
    total_retries = 0
    total_bytes = 0
    t_run_start = time.monotonic()

    def _send_one_study(
        idx: int, study_uid: str, slices: list[bytes]
    ) -> tuple[str, bool, float, int, int, list[str]]:
        target = target_urls[idx % len(target_urls)]
        t0 = time.monotonic()
        retries = 0
        sent_bytes = 0
        inst_ids: list[str] = []
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
                        iid = resp.json().get("ID")
                        if iid:
                            inst_ids.append(iid)
                        break
                    except Exception:
                        if attempt < max_retries:
                            retries += 1
                            time.sleep(0.5)
                if not delivered:
                    ok = False
        return study_uid, ok, time.monotonic() - t0, retries, sent_bytes, inst_ids

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [
            pool.submit(_send_one_study, i, suid, slices)
            for i, (suid, slices) in enumerate(studies)
        ]
        for fut in as_completed(futs):
            suid, ok, dt, retries, nbytes, inst_ids = fut.result()
            total_retries += retries
            total_bytes += nbytes
            sent_by_study[suid] = inst_ids
            if ok:
                study_times.append(dt)
            else:
                failed_studies += 1

    elapsed = time.monotonic() - t_run_start
    summary = _summarize(
        name,
        len(studies),
        study_times,
        failed_studies,
        total_retries,
        total_bytes,
        elapsed,
        [0] * len(CLOUD_URLS),
    )
    return summary, sent_by_study


def _run_diomede_strategy(
    name: str,
    studies: list[tuple[str, list[bytes]]],
    drain_timeout_s: float = 240.0,
) -> tuple[dict[str, Any], dict[str, list[str]]]:
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

    elapsed = time.monotonic() - t_run_start
    summary = _summarize(
        name,
        len(studies),
        completed_times,
        len(pending),
        0,
        total_bytes,
        elapsed,
        [0] * len(CLOUD_URLS),
    )
    return summary, sent_by_study


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
    p.add_argument(
        "--diomede-only",
        action="store_true",
        help="Run only the Diomede scenario (for sweeping rest / stow-rs / dimse modes).",
    )
    p.add_argument("--label", type=str, default="diomede", help="Label for the Diomede run.")
    p.add_argument("--output-json", type=str, default="baseline_comparison_results.json")
    args = p.parse_args()

    results: list[dict[str, Any]] = []

    if args.diomede_only:
        scenarios = [(args.label, lambda s: _run_diomede_strategy(args.label, s))]
    else:
        scenarios = [
            (
                "static_us",
                lambda s: _run_direct_strategy(
                    "static_us", s, [CLOUD_URLS["us-east1"]], args.concurrency
                ),
            ),
            (
                "round_robin",
                lambda s: _run_direct_strategy(
                    "round_robin", s, list(CLOUD_URLS.values()), args.concurrency
                ),
            ),
            (
                "haproxy_leastconn",
                lambda s: _run_direct_strategy(
                    "haproxy_leastconn", s, [HAPROXY_URL], args.concurrency
                ),
            ),
            (args.label, lambda s: _run_diomede_strategy(args.label, s)),
        ]

    for label, fn in scenarios:
        # Build fresh Study/SOP UIDs per scenario so cloud audit never collides across strategies.
        scenario_studies = build_study_payloads(args.studies, args.slices_per_study, args.slice_kb)
        log.info("=== Running baseline scenario: %s (outage=%s) ===", label, args.simulate_outage)
        outage_thread = None
        if args.simulate_outage:
            outage_thread = _schedule_outage(
                args.outage_node, delay_s=3.0, duration_s=args.outage_duration_s
            )
        res, sent_by_study = fn(scenario_studies)
        if outage_thread is not None:
            outage_thread.join()
            time.sleep(8.0)
        per_node, split_studies, missing = _audit_cloud_studies(
            sent_by_study, args.slices_per_study
        )
        res["split_studies"] = split_studies
        res["per_node_studies"] = per_node
        res["jains_fairness"] = round(jains_fairness(list(per_node.values())), 4)
        res["verified_missing_studies"] = missing
        results.append(res)
        print(json.dumps(res, indent=2))

    Path(args.output_json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("Wrote baseline comparison results to %s", args.output_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
