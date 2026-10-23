"""
src/benchmark/routing_correctness.py

Benchmark script for validating routing decision correctness across synthetic load permutations.
Reproduces the 35 load permutations across the 4 Orthanc nodes described in
Section 4.2 (Routing Correctness) of the paper, validating 100% routing accuracy.

It writes synthetic telemetry straight to Redis, which the stack does not publish to the
host, so start it with the benchmark override first:

    docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d
    python -m src.benchmark.routing_correctness
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

from src.orchestrator.weighted_scorer import WeightedScorer
from src.utils.logging_config import get_logger

load_dotenv()
log = get_logger(__name__, "BENCHMARK_CORRECTNESS")

ORCH_URL = os.getenv("ORCHESTRATOR_HTTPS_URL", "https://localhost:8000")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "CHANGE_IN_PRODUCTION")
# Not REDIS_URL: .env sets that to redis://redis:6379, which only resolves inside Docker.
REDIS_URL = os.getenv("BENCHMARK_REDIS_URL", "redis://localhost:6379")
# Not a real edge agent's ID, so the synthetic RTTs never replace measured ones.
AGENT_ID = "routing-benchmark"
DAEMON_CONTAINER = "telemetry-daemon"
CA_CERT = os.getenv("REQUESTS_CA_BUNDLE", "certs/ca.pem")
VERIFY_SSL: str | bool = CA_CERT if os.path.exists(CA_CERT) else False

REGIONS = [
    os.getenv("REGION1_NAME", "us-east1"),
    os.getenv("REGION2_NAME", "eu-west1"),
    os.getenv("REGION3_NAME", "asia-northeast1"),
    os.getenv("REGION4_NAME", "af-south1"),
]


def generate_permutations(num_trials: int = 35) -> list[dict[str, int]]:
    """Generate 35 synthetic queue-depth permutations across 4 regional nodes."""
    permutations: list[dict[str, int]] = []
    base_levels = [0, 1, 3, 5, 8, 12, 20]
    trial_count = 0

    for i in range(len(REGIONS)):
        for level in base_levels:
            if trial_count >= num_trials:
                break
            # Assign lowest queue to one region, higher queues to others
            perm: dict[str, int] = {}
            for idx, reg in enumerate(REGIONS):
                if idx == i:
                    perm[reg] = 0
                else:
                    perm[reg] = max(level + idx * 2, 1)
            permutations.append(perm)
            trial_count += 1

    # Fill remaining trials with dynamic variations if needed
    while len(permutations) < num_trials:
        idx = len(permutations) % len(REGIONS)
        perm = {r: 10 for r in REGIONS}
        perm[REGIONS[idx]] = 1
        permutations.append(perm)

    return permutations[:num_trials]


async def set_synthetic_telemetry(
    redis: aioredis.Redis[str],  # type: ignore[type-arg]
    queues: dict[str, int],
    disk_free_mb: float = 8000.0,
    disk_total_mb: int = 10000,
    rtt_ms: float = 50.0,
) -> None:
    now_iso = datetime.now(tz=UTC).isoformat()
    rtt_dict = {reg: rtt_ms for reg in REGIONS}
    await redis.set(f"rtt:{AGENT_ID}", json.dumps(rtt_dict), ex=60)

    for reg, q in queues.items():
        payload = {
            "node_id": reg,
            "ae_title": f"Orthanc_{reg.upper()}",
            "base_url": f"https://{reg}:8042",
            "queue_size": q,
            "disk_free_mb": disk_free_mb,
            "disk_total_mb": disk_total_mb,
            "instance_count": 100 + q,
            "healthy": True,
            "ts": now_iso,
        }
        await redis.set(f"node:{reg}", json.dumps(payload), ex=60)


async def evaluate_correctness(num_trials: int = 35) -> int:
    scorer = WeightedScorer()
    perms = generate_permutations(num_trials)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    print("\n" + "=" * 70)
    print("DIOMEDE ROUTING CORRECTNESS BENCHMARK")
    print(f"Total Trials: {num_trials} Load Permutations")
    print("=" * 70)

    correct_count = 0

    async with httpx.AsyncClient(verify=VERIFY_SSL) as client:
        for idx, perm in enumerate(perms, start=1):
            await set_synthetic_telemetry(redis, perm)
            # Allow telemetry propagation
            await asyncio.sleep(0.05)

            # Calculate mathematical winner
            candidates = [
                {
                    "node_id": reg,
                    "queue_size": perm[reg],
                    "disk_free_mb": 8000.0,
                    "disk_total_mb": 10000,
                    "rtt_ms": 50.0,
                }
                for reg in REGIONS
            ]
            expected_winner = max(candidates, key=scorer.score)["node_id"]

            # Query orchestrator
            try:
                resp = await client.get(
                    f"{ORCH_URL}/get-best-node",
                    params={"agent_id": AGENT_ID},
                    headers={"X-API-Key": API_KEY},
                    timeout=5.0,
                )
                resp.raise_for_status()
                actual_winner = resp.json().get("node_id")
            except Exception as exc:
                log.error("Trial %d failed to query orchestrator: %s", idx, exc)
                actual_winner = None

            is_correct = actual_winner == expected_winner
            if is_correct:
                correct_count += 1

            status_str = "MATCH" if is_correct else "MISMATCH"
            print(
                f"Trial {idx:2d}/{num_trials}: Queues={perm} -> "
                f"Expected: {expected_winner:<16} Actual: {str(actual_winner):<16} [{status_str}]"
            )

    await redis.close()
    accuracy = (correct_count / num_trials) * 100.0

    print("\n" + "=" * 70)
    print(f"Correct Decisions: {correct_count} / {num_trials}")
    print(f"Routing Accuracy:  {accuracy:.2f}%")
    print("=" * 70 + "\n")

    return 0 if correct_count == num_trials else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate routing decision correctness.")
    parser.add_argument(
        "--trials", type=int, default=35, help="Number of load permutations (default: 35)."
    )
    args = parser.parse_args()
    # The daemon would overwrite the synthetic node:* telemetry with real readings mid-trial.
    subprocess.run(["docker", "pause", DAEMON_CONTAINER], check=True, stdout=subprocess.DEVNULL)
    try:
        return asyncio.run(evaluate_correctness(args.trials))
    finally:
        subprocess.run(
            ["docker", "unpause", DAEMON_CONTAINER], check=True, stdout=subprocess.DEVNULL
        )


if __name__ == "__main__":
    sys.exit(main())
