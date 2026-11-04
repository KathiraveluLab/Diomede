"""
Locust load tests for the Diomede orchestrator and edge ingestion path.

Two independent scenarios, each mapping to a proposal KPI (Section 7.1):

  RoutingUser  -> GET /get-best-node latency under concurrency
                  KPI: routing latency p99 < 50 ms @ 100 concurrent requests
  IngestUser   -> POST /instances DICOM ingestion at the edge Orthanc
                  KPI: >= 99.5% transfer success over a 100-image burst

Run one scenario at a time by naming its class (clean per-KPI numbers):

    locust --config tests/load/locust.conf RoutingUser --users 100 --run-time 1m
    LOAD_BURST_COUNT=100 locust --config tests/load/locust.conf IngestUser --users 10

Configuration (base URLs, API key, credentials, CA cert) is read from the same
.env the rest of the stack uses. See tests/load/README.md.
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import gevent
import httpx
from dotenv import load_dotenv
from locust import HttpUser, LoadTestShape, between, events, stats, task

from src.simulator.generate_dicom import make_sized

load_dotenv(override=True)

# --- Targets and credentials (host-facing; the simulator runs outside docker) ---
ORCH_URL = os.environ.get("ORCHESTRATOR_HTTPS_URL")
EDGE_URL = os.environ.get("EDGE_AGENT1_HTTPS_URL")
API_KEY = os.environ.get("ORCHESTRATOR_API_KEY")
AGENT_ID = os.environ.get("EDGE_AGENT1")
EDGE_AUTH = (
    os.environ.get("ORTHANC_USER"),
    os.environ.get("ORTHANC_PASSWORD"),
)
FILE_SIZE_KB = int(os.environ.get("LOAD_FILE_SIZE_KB"))
BURST_COUNT = int(os.environ.get("LOAD_BURST_COUNT"))
LOAD_MIN_INTERVAL = float(os.environ.get("LOAD_MIN_INTERVAL"))
LOAD_MAX_INTERVAL = float(os.environ.get("LOAD_MAX_INTERVAL"))
_ingest_sent = 0

# --- KPI thresholds (Section 7.1); override via env for experimentation ---
MAX_P99_MS = float(os.environ.get("LOAD_MAX_P99_MS"))
MAX_FAIL_RATIO = float(os.environ.get("LOAD_MAX_FAIL_RATIO"))

# Stable stat names so the KPI check can find each endpoint whether the scenarios
# run alone or together.
_ROUTE_NAME = "GET /get-best-node"
_INGEST_NAME = "POST /instances"

DRAIN_POLL_INTERVAL_S = float(os.environ.get("LOAD_DRAIN_POLL_INTERVAL_S", "1"))
DRAIN_TIMEOUT_S = float(os.environ.get("LOAD_DRAIN_TIMEOUT_S", "5"))

stats.PERCENTILES_TO_REPORT = [0.5, 0.95, 0.99]
stats.PERCENTILES_TO_CHART = [0.5, 0.95, 0.99]
stats.PERCENTILES_TO_STATISTICS = [0.5, 0.95, 0.99]


def _tls_verify() -> str | bool:
    """Resolve TLS verification for the self-signed dev CA.

    REQUESTS_CA_BUNDLE points at the in-container path (/certs/ca.pem), which does
    not exist on the host, so resolve explicitly: an override, then the repo-local
    CA, then fall back to skipping verification for local load runs.
    """
    override = os.environ.get("LOAD_CA_CERT")
    if override:
        return override
    if os.environ.get("LOAD_VERIFY_TLS", "").lower() in ("0", "false", "no"):
        return False
    local_ca = Path("certs/ca.pem")
    return str(local_ca) if local_ca.exists() else False


_VERIFY = _tls_verify()


class RoutingUser(HttpUser):
    """Hammer the orchestrator routing decision path (control-plane latency KPI)."""

    host = ORCH_URL
    wait_time = between(LOAD_MIN_INTERVAL, LOAD_MAX_INTERVAL)
    weight = 8

    def on_start(self) -> None:
        self.client.verify = _VERIFY
        self.client.headers.update({"X-API-Key": API_KEY})

    @task(8)
    def get_best_node(self) -> None:
        with self.client.get(
            "/get-best-node",
            params={"agent_id": AGENT_ID},
            name=_ROUTE_NAME,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")

    @task(1)
    def list_nodes(self) -> None:
        self.client.get("/nodes", name="GET /nodes")


class IngestUser(HttpUser):
    """Post synthetic DICOM to the edge Orthanc (data-plane success-rate KPI)."""

    host = EDGE_URL
    wait_time = between(LOAD_MIN_INTERVAL, LOAD_MAX_INTERVAL)
    weight = 2

    def on_start(self) -> None:
        self.client.verify = _VERIFY
        self.client.auth = EDGE_AUTH

    @task
    def post_instance(self) -> None:
        global _ingest_sent
        if BURST_COUNT and _ingest_sent >= BURST_COUNT:
            return  # burst complete; a quit() is already scheduled
        _ingest_sent += 1  # reserve a slot; check has no yield -> exact count
        ds = make_sized(FILE_SIZE_KB)
        buf = io.BytesIO()
        ds.save_as(buf)
        with self.client.post(
            "/instances",
            data=buf.getvalue(),
            headers={"Content-Type": "application/dicom"},
            name=_INGEST_NAME,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if BURST_COUNT and _ingest_sent >= BURST_COUNT:
            # Burst done: stop the run from a fresh greenlet so this request
            # finishes cleanly (quit() kills user greenlets, including this one).
            gevent.spawn_later(0, self.environment.runner.quit)


def _wait_for_edge_drain() -> int | None:
    """Poll the edge Orthanc's instance list until it's empty or DRAIN_TIMEOUT_S
    elapses. Returns the count still stuck (0 = fully drained), or None if the
    edge couldn't be reached at all."""
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    count: int | None = None
    with httpx.Client(base_url=EDGE_URL, auth=EDGE_AUTH, verify=_VERIFY) as client:
        while True:
            try:
                resp = client.get("/instances", timeout=10)
                resp.raise_for_status()
                count = len(resp.json())
            except Exception as exc:
                print(f"[KPI] drain check: edge poll failed: {exc}")
            if count == 0 or time.monotonic() >= deadline:
                return count
            gevent.sleep(DRAIN_POLL_INTERVAL_S)


@events.test_start.add_listener
def _reset_burst(environment, **_kwargs) -> None:
    """Reset the burst counter so repeated runs in one process (web UI) start fresh."""
    global _ingest_sent
    _ingest_sent = 0


@events.test_stop.add_listener
def _check_kpis(environment, **_kwargs) -> None:
    """After the run, evaluate each measured endpoint against its KPI and set a
    non-zero exit code on failure so a headless/CI run reflects the result."""
    stats = environment.stats
    failures: list[str] = []

    route = stats.get(_ROUTE_NAME, "GET")
    if route.num_requests:
        p99 = route.get_response_time_percentile(0.99)
        verdict = "PASS" if p99 <= MAX_P99_MS else "FAIL"
        if p99 > MAX_P99_MS:
            failures.append(f"routing p99 {p99:.1f}ms > {MAX_P99_MS:.0f}ms")
        print(f"[KPI] routing latency p99 = {p99:.1f} ms (limit {MAX_P99_MS:.0f}) -> {verdict}")

    ingest = stats.get(_INGEST_NAME, "POST")
    if ingest.num_requests:
        accepted = ingest.num_requests - ingest.num_failures
        stuck = _wait_for_edge_drain()
        if stuck is None:
            # Delivery can't be confirmed, so don't report a success rate.
            print("[KPI] drain check: could not read the edge buffer -> FAIL")
            failures.append("ingest delivery unconfirmed (edge buffer unreadable)")
        else:
            if stuck > accepted:
                print(
                    f"[KPI] drain check: {stuck} instance(s) left on edge but only "
                    f"{accepted} were accepted this run -- edge buffer had leftovers "
                    f"from before this burst started"
                )
            delivered = max(accepted - stuck, 0)
            ratio = (ingest.num_requests - delivered) / ingest.num_requests
            verdict = "PASS" if ratio <= MAX_FAIL_RATIO else "FAIL"
            if ratio > MAX_FAIL_RATIO:
                failures.append(f"ingest failure ratio {ratio:.4f} > {MAX_FAIL_RATIO:.4f}")
            pct, limit = (1 - ratio) * 100, (1 - MAX_FAIL_RATIO) * 100
            print(f"[KPI] transfer success = {pct:.2f}% (limit {limit:.1f}%) -> {verdict}")

    if failures:
        print("[KPI] LOAD TEST FAILED: " + "; ".join(failures))
        environment.process_exit_code = 1
    else:
        print("[KPI] all measured KPIs within target")


class AutoRampDownShape(LoadTestShape):
    """
    Natively reads standard configuration parameters (users, run-time)
    and automatically calculates a trailing 30-second ramp-down phase.
    """

    def tick(self):
        # 1. Fetch values directly from the configuration file
        options = self.runner.environment.parsed_options

        target_users = options.num_users or 100
        spawn_rate = options.spawn_rate or 5
        run_time_limit = options.run_time or 180  # Defaults to 180 seconds

        # 2. Check current execution timeline
        current_time = self.get_run_time()
        ramp_down_duration = 30.0  # 30 seconds fixed ramp down
        end_of_test = run_time_limit + ramp_down_duration

        # Phase A: Steady State (Read directly from the config file)
        if current_time < run_time_limit:
            return (target_users, spawn_rate)

        # Phase B: Automated 30-second Ramp Down
        elif current_time < end_of_test:
            time_into_ramp_down = current_time - run_time_limit
            progress_ratio = time_into_ramp_down / ramp_down_duration

            # Linearly reduce users from target_users down to 0
            remaining_users = max(int(target_users * (1.0 - progress_ratio)), 0)

            # Use a slightly aggressive spawn rate during ramp-down to disconnect users quickly
            return (remaining_users, spawn_rate)

        # Phase C: Test Gracefully Finished
        return None
