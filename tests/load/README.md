# Load tests (Locust)

## Setup

```bash
pip install -e ".[load,scripts]"     # locust + pydicom (for synthetic DICOM)
docker compose up -d --build         # the stack must be running
```

Configuration is read from `.env`:
`ORCHESTRATOR_HTTPS_URL`, `EDGE_AGENT1_HTTPS_URL`, `ORCHESTRATOR_API_KEY`,
`EDGE_AGENT1`, `ORTHANC_USER`/`ORTHANC_PASSWORD`. TLS uses the local dev CA
(`certs/ca.pem`) automatically; set `LOAD_VERIFY_TLS=false` to skip verification.

## Run

```bash
# Routing-latency KPI: 100 concurrent clients for 1 minute
locust --config tests/load/locust.conf RoutingUser --users 100 --run-time 1m

# Transfer-success KPI: a 100-image burst (stops after 100 POSTs)
# (set LOAD_BURST_COUNT=100 in .env first, see below)
locust --config tests/load/locust.conf IngestUser --users 10

# Interactive web UI instead of headless (drop --headless via CLI):
locust -f tests/load/locustfile.py RoutingUser --host https://localhost:8000
```

The tunables are also read from `.env` (`.env.example` lists the first six, with
these values): `LOAD_BURST_COUNT` (`0` = no burst limit), `LOAD_FILE_SIZE_KB` (image size,
50), `LOAD_MAX_P99_MS` (50), `LOAD_MAX_FAIL_RATIO` (0.005), `LOAD_MIN_INTERVAL` and
`LOAD_MAX_INTERVAL` (seconds each user waits between requests, 0.02 and 0.10). Optional:
`LOAD_VERIFY_TLS`, `LOAD_CA_CERT` and `LOAD_DRAIN_POLL_INTERVAL_S` /
`LOAD_DRAIN_TIMEOUT_S` (1 and 5).

Edit `.env` to change them: the locustfile loads it with `override=True`, so `.env`
wins over the shell and `LOAD_BURST_COUNT=100 locust ...` has no effect.

## Notes

- `IngestUser` success = the edge **accepted** the instance (client → edge). True
  end-to-end delivery (edge → cloud node) is measured by
  [`src/benchmark/success_rate.py`](../../src/benchmark/success_rate.py), which polls
  the cloud nodes for arrivals.
- **Failover** (node offline mid-transfer) is timed by
  [`src/benchmark/failover_time.py`](../../src/benchmark/failover_time.py). To observe
  failover under load, start an `IngestUser` burst and `docker stop` a cloud node
  mid-run; the transfer-success KPI reflects the impact.
