# Benchmarks

Three scripts in `src/benchmark/` measure the running stack. Each docstring names
the section of the paper whose experiment it reproduces. They talk to the
Docker Compose stack from your host, so start it first (see the
[setup guide](setup.md), Steps 1-7) and make sure the Telemetry Daemon is running
(`docker compose ps telemetry-daemon`). If it is not `Up`, run
`docker compose up -d telemetry-daemon`.

| Script | Measures | Touches the stack? |
|---|---|---|
| `routing_correctness` | Whether `/get-best-node` picks the node the scorer ranks highest, over synthetic load permutations | Writes fake telemetry to Redis and **pauses the `telemetry-daemon` container** |
| `failover_time` | Seconds until the orchestrator reports a node unhealthy after it is stopped or cut off, and until it reports it healthy again | Stops or network-disconnects the real Orthanc containers |
| `success_rate` | Share of the sent DICOM instances that end up stored on a cloud node, optionally with a node stopped mid-transfer | Uploads to the edge Orthanc, optionally stops a cloud node |

## Install and configure

Run everything from the repository root, in a virtual environment. If you
installed the extras from setup Step 3 you need nothing else; otherwise:

```bash
pip install -e ".[bench,orchestrator]"
```

`bench` provides `httpx`, `pydicom` and `python-dotenv`. `routing_correctness`
also needs `redis`, which the `orchestrator` extra provides. The scripts call the `docker` CLI, so it must be on your
`PATH` and able to reach the daemon that runs the stack.

The scripts read your `.env` through `python-dotenv` and fall back to the
`.env.example` defaults for the host-side URLs:

| Variable | Used by | Default |
|---|---|---|
| `ORCHESTRATOR_HTTPS_URL`, `ORCHESTRATOR_API_KEY` | `routing_correctness`, `failover_time` | `https://localhost:8000`, `CHANGE_IN_PRODUCTION` |
| `EDGE_AGENT1_HTTPS_URL`, `EDGE_USER`, `EDGE_PASS` | `success_rate` | `https://localhost:8046`, then `ORTHANC_USER` / `ORTHANC_PASSWORD` |
| `NODE_US_HTTPS_URL` ... `NODE_AF_HTTPS_URL`, `ORTHANC_USER`, `ORTHANC_PASSWORD` | `success_rate` | `https://localhost:8042` ... `8045`, `orthanc` |
| `REGION1_HOST` ... `REGION4_HOST` | `failover_time` | `orthanc-us`, `orthanc-eu`, `orthanc-asia`, `orthanc-af` (container names) |
| `BENCHMARK_REDIS_URL` | `routing_correctness` | `redis://localhost:6379` |
| `DOCKER_NETWORK` | `failover_time` | `diomede-net` |

`BENCHMARK_REDIS_URL` is separate from `REDIS_URL` on purpose: `.env` sets
`REDIS_URL=redis://redis:6379`, a name that only resolves inside Docker.

## `routing_correctness`

For each trial it writes synthetic `node:<region>` telemetry (different queue
depths, 8000 of 10000 MB free, 50 ms RTT, 60 s expiry) and an `rtt:routing-benchmark`
entry to Redis, asks `GET /get-best-node?agent_id=routing-benchmark`, and compares
the answer with the winner it computes locally with `WeightedScorer`. It exits 0
when every trial matches and 1 otherwise.

Redis is not published to the host by default. Start the stack with the overlay,
which publishes it on `127.0.0.1:6379` only:

```bash
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d
python -m src.benchmark.routing_correctness            # 35 trials
python -m src.benchmark.routing_correctness --trials 10
```

| Flag | Default | Meaning |
|---|---|---|
| `--trials` | `35` | Number of load permutations |

Things to know before you run it:

- **It pauses the `telemetry-daemon` container** (`docker pause`) so the daemon
  does not overwrite the synthetic readings, and unpauses it when the run ends or
  fails. If the process is killed without cleanup, run
  `docker unpause telemetry-daemon`. The script stops at once if the container
  cannot be paused.
- The synthetic `node:*` entries expire after 60 s. Until the daemon polls again
  after it is unpaused, `/get-best-node` can still return the synthetic values.
- The expected winner comes from a local `WeightedScorer()`, which reads `W_QUEUE`,
  `W_DISK` and `W_RTT` from your environment or `.env`. It therefore assumes the
  orchestrator runs the default `weighted` scorer with the same weights. With
  another `SCORER` or different weights, expect mismatches that are not bugs.

## `failover_time`

For each region and failure mode it injects the failure, polls the orchestrator's
`/nodes` until the node is reported unhealthy (or has dropped out of the list),
restores the node, and polls until it is healthy again. It prints one line per
trial and a mean and standard deviation per region and mode.

```bash
python -m src.benchmark.failover_time                                  # 4 regions x 2 modes x 5 trials
python -m src.benchmark.failover_time --regions us-east1 --modes process_stop --trials 3
```

| Flag | Default | Meaning |
|---|---|---|
| `--regions` | all four configured regions | Region names such as `us-east1` (the `node_id` values `/nodes` returns), not container names |
| `--modes` | `process_stop network_partition` | `process_stop` runs `docker stop` / `docker start`; `network_partition` runs `docker network disconnect` / `connect` on `DOCKER_NETWORK` |
| `--trials` | `5` | Trials per region and mode |
| `--poll-interval-s` | `0.25` | Seconds between polls of `/nodes` |
| `--timeout-s` | `30.0` | How long to wait for each transition |

Things to know:

- Detection time is bounded by the Telemetry Daemon's cycle (`DAEMON_POLL_INTERVAL_S`,
  `HTTP_TIMEOUT_S`) and by `REDIS_TTL_S`, so raise `--timeout-s` if you raise those.
- A transition that is not seen within `--timeout-s` is logged as an error
  (`Failover detection timed out ...` / `Recovery detection timed out ...`).
- Every trial includes fixed sleeps of 2 s and 3 s on top of the detection and
  recovery times, so the default 40 trials take several minutes.
- `scripts/simulate_partition.sh` does the same disconnect by hand:
  `./scripts/simulate_partition.sh isolate|heal|status [container]`
  (default container `orthanc-us`, network `DOCKER_NETWORK` or `diomede-net`).

## `success_rate`

It uploads synthetic DICOM instances to the edge Orthanc, waits for the edge buffer
to empty, then looks every sent instance up by its Orthanc ID on the four cloud
nodes. The success rate is the instances found on a cloud node divided by
`--burst-count`, so instances the edge refused count as failures. It exits 0 when
every instance arrived, 1 when some are missing, and 2 when `--simulate-outage` was
requested but the node could not be stopped.

```bash
python -m src.benchmark.success_rate --burst-count 1000
python -m src.benchmark.success_rate --burst-count 1000 --simulate-outage
```

| Flag | Default | Meaning |
|---|---|---|
| `--burst-count` | `LOAD_BURST_COUNT`, else `1000` | Number of instances to send |
| `--file-size-kb` | `LOAD_FILE_SIZE_KB`, else `50` | Size of each instance |
| `--simulate-outage` | off | Stop `--outage-node` once half of the instances are sent |
| `--outage-duration-s` | `15.0` | Seconds before that container is started again |
| `--outage-node` | `orthanc-us` | Docker **container** name of the cloud node to stop |
| `--timeout-s` | `120.0` | How long to wait for the edge buffer to drain |

**Pass `--burst-count` explicitly.** `--help` prints `1000`, but the default is read
from `LOAD_BURST_COUNT`, and `.env.example` sets `LOAD_BURST_COUNT=0`. With a `.env`
copied from it, the script sends nothing and fails with `ZeroDivisionError` when it
computes the rate.

## See also

- [`tests/load/README.md`](../tests/load/README.md) for the Locust load tests,
  which measure the client-to-edge half of a transfer.
- [Architecture](architecture.md) for how health, scoring and failover work.
