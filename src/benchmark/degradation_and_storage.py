"""
src/benchmark/degradation_and_storage.py

Evaluates WAN degradation and storage exhaustion failure modes under active transfers:
  1. Network degradation (tc netem delay, jitter, and packet loss injected mid-workload)
  2. Storage exhaustion (driving a regional node below the 2% free-storage quota floor
     with MaximumStorageMode: Reject during active transfers using incompressible DICOM
     payloads and docker-compose.storage-test.yml)

Usage:
    python -m src.benchmark.degradation_and_storage --mode netem
    docker compose -f docker-compose.yml -f docker-compose.storage-test.yml up -d \
        --force-recreate orthanc-us
    python -m src.benchmark.degradation_and_storage --mode storage --slice-kb 512
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from pydicom.uid import generate_uid

from src.simulator.generate_dicom import make_sized
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_DEGRADATION")

AUTH = (os.getenv("ORTHANC_USER", "orthanc"), os.getenv("ORTHANC_PASSWORD", "orthanc"))
ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")
EDGE_URL = os.getenv("EDGE_AGENT1_HTTPS_URL", "https://localhost:8046")
CLOUD_URLS = {
    "us-east1": os.getenv("NODE_US_HTTPS_URL", "https://localhost:8042"),
    "eu-west1": os.getenv("NODE_EU_HTTPS_URL", "https://localhost:8043"),
    "asia-northeast1": os.getenv("NODE_ASIA_HTTPS_URL", "https://localhost:8044"),
    "af-south1": os.getenv("NODE_AF_HTTPS_URL", "https://localhost:8045"),
}
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False


def _tc_apply(container: str, delay_ms: int, jitter_ms: int, loss_pct: float) -> None:
    subprocess.run(
        ["docker", "exec", container, "tc", "qdisc", "del", "dev", "eth0", "root"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "docker",
            "exec",
            container,
            "tc",
            "qdisc",
            "add",
            "dev",
            "eth0",
            "root",
            "netem",
            "delay",
            f"{delay_ms}ms",
            f"{jitter_ms}ms",
            "25%",
            "loss",
            f"{loss_pct}%",
            "25%",
        ],
        check=True,
    )


def _tc_reset(container: str) -> None:
    subprocess.run(
        ["docker", "exec", container, "tc", "qdisc", "del", "dev", "eth0", "root"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_network_degradation(
    target_container: str = "orthanc-us", studies: int = 20
) -> dict[str, Any]:
    """Inject severe delay, jitter, and packet loss on primary node and observe rerouting."""
    log.info("Starting tc netem degradation experiment on %s", target_container)
    pre_routes: list[str] = []
    degraded_routes: list[str] = []

    with httpx.Client(verify=VERIFY_SSL) as client:
        for _ in range(5):
            r = client.get(
                f"{ORCH_URL}/get-best-node",
                params={"agent_id": "agent-001"},
                headers={"X-API-Key": API_KEY},
                timeout=5.0,
            )
            r.raise_for_status()
            pre_routes.append(r.json()["node_id"])

        try:
            _tc_apply(target_container, delay_ms=350, jitter_ms=60, loss_pct=5.0)
            hb = client.post(
                f"{ORCH_URL}/heartbeat",
                json={
                    "agent_id": "agent-001",
                    "rtt_dict": {
                        "us-east1": 350.0,
                        "eu-west1": 120.0,
                        "asia-northeast1": 90.0,
                        "af-south1": 280.0,
                    },
                },
                headers={"X-API-Key": API_KEY},
                timeout=5.0,
            )
            hb.raise_for_status()

            sent_ids: list[str] = []
            t0 = time.monotonic()
            for _ in range(studies):
                ds = make_sized(256)
                ds.StudyInstanceUID = str(generate_uid())
                buf = io.BytesIO()
                ds.save_as(buf, write_like_original=False)
                resp = client.post(
                    f"{EDGE_URL}/instances",
                    content=buf.getvalue(),
                    headers={"Content-Type": "application/dicom"},
                    auth=AUTH,
                    timeout=30.0,
                )
                resp.raise_for_status()
                sent_ids.append(resp.json()["ID"])

                best = client.get(
                    f"{ORCH_URL}/get-best-node",
                    params={"agent_id": "agent-001"},
                    headers={"X-API-Key": API_KEY},
                    timeout=5.0,
                ).json()["node_id"]
                degraded_routes.append(best)

            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                try:
                    edge_ids = set(
                        client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json()
                    )
                except Exception:
                    time.sleep(0.5)
                    continue
                if not set(sent_ids).intersection(edge_ids):
                    break
                time.sleep(1.0)
            elapsed = time.monotonic() - t0
        finally:
            _tc_reset(target_container)

    return {
        "experiment": "network_degradation_netem",
        "degraded_container": target_container,
        "injected_profile": "delay=350ms jitter=60ms loss=5.0%",
        "pre_degradation_routes": pre_routes,
        "during_degradation_routes": degraded_routes,
        "studies_sent": studies,
        "drain_elapsed_s": round(elapsed, 2),
        "shifted_away_from_degraded": all(r != "us-east1" for r in degraded_routes),
    }


def run_storage_exhaustion(
    target_node: str = "us-east1",
    slice_kb: int = 512,
    max_studies: int = 40,
) -> dict[str, Any]:
    """Drive target_node past its 2% free-storage floor using incompressible DICOM payloads
    and confirm telemetry unhealthy transition + lossless rerouting to healthy peers."""
    log.info("Starting storage-exhaustion verification on %s", target_node)
    with httpx.Client(verify=VERIFY_SSL) as client:
        nodes_before = {
            n["node_id"]: n
            for n in client.get(
                f"{ORCH_URL}/nodes", headers={"X-API-Key": API_KEY}, timeout=5.0
            ).json()
        }
        sys_info = client.get(f"{CLOUD_URLS[target_node]}/system", auth=AUTH, timeout=5.0).json()
        quota_mb = int(sys_info.get("MaximumStorageSize") or 0)
        storage_mode = sys_info.get("MaximumStorageMode", "Unknown")

        # Favor target_node initially via low RTT so incoming studies fill it until the 2% floor.
        client.post(
            f"{ORCH_URL}/heartbeat",
            json={
                "agent_id": "agent-001",
                "rtt_dict": {
                    "us-east1": 5.0,
                    "eu-west1": 200.0,
                    "asia-northeast1": 220.0,
                    "af-south1": 300.0,
                },
            },
            headers={"X-API-Key": API_KEY},
            timeout=5.0,
        ).raise_for_status()

        free_before = nodes_before.get(target_node, {}).get("disk_free_mb")
        if free_before is not None and free_before > 0:
            needed = int((float(free_before) * 1.25) / (slice_kb / 1024.0)) + 4
            num_studies = max(10, min(max_studies, needed))
        else:
            num_studies = min(max_studies, 24)

        sent_ids: list[str] = []
        t0 = time.monotonic()
        for _ in range(num_studies):
            ds = make_sized(slice_kb, random_pixels=True)
            ds.StudyInstanceUID = str(generate_uid())
            buf = io.BytesIO()
            ds.save_as(buf, write_like_original=False)
            resp = client.post(
                f"{EDGE_URL}/instances",
                content=buf.getvalue(),
                headers={"Content-Type": "application/dicom"},
                auth=AUTH,
                timeout=30.0,
            )
            resp.raise_for_status()
            sent_ids.append(resp.json()["ID"])
            time.sleep(0.25)

        deadline = time.monotonic() + 180.0
        while time.monotonic() < deadline:
            try:
                edge_ids = set(client.get(f"{EDGE_URL}/instances", auth=AUTH, timeout=5.0).json())
            except Exception:
                time.sleep(0.5)
                continue
            if not set(sent_ids).intersection(edge_ids):
                break
            time.sleep(1.0)
        elapsed = time.monotonic() - t0

        # Allow one telemetry poll cycle so daemon updates healthy status after final uploads.
        time.sleep(11.0)
        nodes_after = {
            n["node_id"]: n
            for n in client.get(
                f"{ORCH_URL}/nodes", headers={"X-API-Key": API_KEY}, timeout=5.0
            ).json()
        }

        cloud_sets: dict[str, set[str]] = {}
        for region, url in CLOUD_URLS.items():
            try:
                cloud_sets[region] = set(
                    client.get(f"{url}/instances", auth=AUTH, timeout=15.0).json()
                )
            except Exception:
                cloud_sets[region] = set()

        per_node_found: dict[str, int] = {r: 0 for r in CLOUD_URLS}
        missing = 0
        for iid in sent_ids:
            found = False
            for region, ids in cloud_sets.items():
                if iid in ids:
                    per_node_found[region] += 1
                    found = True
                    break
            if not found:
                missing += 1

    return {
        "experiment": "storage_exhaustion",
        "target_node": target_node,
        "quota_mb": quota_mb,
        "storage_mode": storage_mode,
        "disk_free_before_mb": free_before,
        "disk_free_after_mb": nodes_after.get(target_node, {}).get("disk_free_mb"),
        "healthy_after": nodes_after.get(target_node, {}).get("healthy"),
        "studies_sent": num_studies,
        "confirmed_on_cloud": len(sent_ids) - missing,
        "missing_from_cloud": missing,
        "per_node_distribution": per_node_found,
        "rerouted_to_peers": sum(v for k, v in per_node_found.items() if k != target_node),
        "drain_elapsed_s": round(elapsed, 2),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Network degradation and storage exhaustion benchmarks."
    )
    p.add_argument("--mode", choices=["netem", "storage", "both"], default="both")
    p.add_argument("--slice-kb", type=int, default=512)
    p.add_argument("--max-studies", type=int, default=40)
    p.add_argument("--output-json", type=str, default="degradation_storage_results.json")
    args = p.parse_args()

    out: list[dict[str, Any]] = []
    if args.mode in ("netem", "both"):
        out.append(run_network_degradation())
    if args.mode in ("storage", "both"):
        out.append(run_storage_exhaustion(slice_kb=args.slice_kb, max_studies=args.max_studies))

    Path(args.output_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
