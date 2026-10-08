# Setup Guide — Local Test Environment

This guide takes you from a **fresh clone** to a live 6-container Diomede
environment, then through sending real DICOM studies, simulating WAN latency,
watching a failover, and running the test suite.

Follow the steps in order — each one builds on the previous. By the end you can:

- Run every code snippet against live Orthanc instances
- Watch routing decisions happen in real time
- Kill a node mid-transfer to verify failover
- Inject realistic WAN latency with `tc netem`

---

## Topology

| Container | Role | Host ports (REST · DICOM) |
|---|---|---|
| `orthanc-us` | Cloud PACS node (GCP us-east1) | 8042 · 4242 |
| `orthanc-eu` | Cloud PACS node (GCP eu-west1) | 8043 · 4243 |
| `orthanc-asia` | Cloud PACS node (GCP asia-northeast1) | 8044 · 4244 |
| `orthanc-af` | Cloud PACS node (GCP af-south1) | 8045 · 4245 |
| `orchestrator` | Redis + Telemetry Daemon + FastAPI (co-located) | 8000 |
| `agent-001` | Edge Orthanc + Forwarder Daemon (co-located) | 8046 · 4246 |

The **orchestrator container** runs three co-located processes, mirroring the
production VM where all three always live on the same host:

- `redis-server` — node registry; keys have a **30 s TTL** (an expired key means a
  dead node), bound to `127.0.0.1` inside the container only.
- `daemon.py` — async Telemetry Daemon; polls all four cloud Orthanc nodes **every
  10 s** and writes JSON heartbeats to Redis over `localhost`.
- `main.py` (via `uvicorn`) — FastAPI Orchestrator; reads Redis over `localhost`
  and serves `GET /get-best-node`, `POST /heartbeat`, `GET /nodes` over HTTPS.

The **edge agent** is a single container running two co-located processes,
following the same pattern:

- **Edge Orthanc** — standard Orthanc PACS; legacy scanners (or the simulator
  scripts) send DICOM C-STORE here on port 4246.
- **Forwarder Daemon** (`forwarder.py`) — polls Orthanc's `/changes` every 5 s on
  `localhost:8042`, downloads new instances, queries the Orchestrator, forwards to
  the winning cloud node, and deletes the local copy. It also probes each cloud
  node's RTT **once per hour** and reports it via `POST /heartbeat`.

Both processes in a container share one network namespace, so they talk to their
co-located Orthanc on `localhost` with zero network hops — mirroring the
production VMs.

---

## Step 1 — Prerequisites

- Docker Desktop ≥ 4.x (macOS/Windows) **or** Docker Engine + Compose plugin (Linux).
  `docker compose version` should print `v2.x`.
- Python 3.12+ on your host.
- `git`, `bash`, and `openssl` (already present on macOS/Linux).

## Step 2 — Clone the repository

```bash
git clone https://github.com/KathiraveluLab/Diomede.git
cd Diomede
```

## Step 3 — Create the Python environment

Unit tests, linting, and the simulator scripts run from your host, so set up a
virtual environment and install the package in editable mode.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[orchestrator,edge,scripts,test,dev]"
```

The `-e` (editable) flag installs the package so Python imports resolve directly
from your working directory — edits to source files take effect immediately
without reinstalling. The groups after the dot pull in optional dependencies:

- `orchestrator` — fastapi, uvicorn, redis, httpx, pydantic
- `edge` — httpx
- `scripts` — httpx, pydicom, pynetdicom, python-dotenv (for the simulator scripts)
- `test` — pytest, fakeredis, respx, pytest-cov, and friends
- `dev` — ruff, mypy, types-redis, pre-commit
- `load` — locust (add `,load` if you plan to run the load tests in Step 11)

## Step 4 — Configure `.env`

The Orthanc nodes and the orchestrator read their secrets from `.env`. Copy the
template and set real values **before starting any container** — Docker Compose
interpolates these variables at startup.

```bash
cp .env.example .env
```

Open `.env` and replace every `CHANGE_IN_PRODUCTION` with a strong value:

```ini
ORTHANC_USER=orthanc
ORTHANC_PASSWORD=your-strong-password-here
ORCHESTRATOR_API_KEY=your-strong-api-key-here
```

- **`ORTHANC_PASSWORD`** — the templates in `config/orthanc/*.template.json`
  substitute `${ORTHANC_USER}` / `${ORTHANC_PASSWORD}` at container startup, so
  every Orthanc node shares the same credentials from this one file.
- **`ORCHESTRATOR_API_KEY`** — required by both the Orchestrator (enforced on every
  endpoint) and the Forwarder (sent as `X-API-Key`). Both services fail fast at
  startup if it is missing.

- **`STUDY_AFFINITY_TTL_S`** (optional, default `3600`) — how long the
  Orchestrator keeps a study pinned to its node after the last routing request
  for that study (see *Study affinity* in `docs/architecture.md`).
- **`FORWARDER_WAIT_FOR_STABLE_STUDY`** (optional, default `false`) — set to `true`
  to route a study only after Edge Orthanc marks it stable (no new instance for
  Orthanc's `StableAge`, 60 s by default).
- **`RTT_TTL_S`** (optional, default `10800`) — how long an edge agent's RTT
  measurements are kept without a new heartbeat. Keep it above `PROBE_INTERVAL_S`.

`.env` is gitignored and must never be committed.

## Step 5 — Generate TLS certificates

Every REST and DICOM link runs over TLS, so the certs must exist before the
containers start (Orthanc won't come up without them).

```bash
bash scripts/gen_certs.sh
```

This creates a self-signed CA and per-service certificates under `certs/`:

```
certs/
├── ca.key                     # CA private key — never commit or share
├── ca.pem                     # CA public cert — distributed to every client for verification
├── orchestrator/              # server.crt + server.key (Uvicorn TLS)
├── orthanc-us/  … orthanc-af/ # server.crt, server.key, combined.pem, ca.pem per node
├── agent-001/                # same layout as a regional node
└── diomede-client/            # client.crt + client.key (clientAuth, used by the simulator)
```

`certs/` is gitignored — **re-run `gen_certs.sh` on every fresh clone**.

<details>
<summary>Inspect a certificate (optional)</summary>

```bash
openssl x509 -in certs/ca.pem -text -noout                  # the CA
openssl x509 -in certs/orchestrator/server.crt -text -noout # a server cert
openssl x509 -in certs/diomede-client/client.crt -text -noout
```
</details>

## Step 6 — Start the stack

The four regional nodes use the pre-built `orthancteam/orthanc:26.4.2` image from
Docker Hub; the orchestrator and edge agent build from local `Dockerfile`s.

```bash
# Pull the Orthanc image once
docker pull orthancteam/orthanc:26.4.2

# Start the 4 regional nodes first and let them become healthy
docker compose up -d orthanc-us orthanc-eu orthanc-asia orthanc-af
docker compose ps          # wait for all four to show Up (healthy)

# Build and start the orchestrator + edge agent
docker compose up -d --build orchestrator agent-001
```

The orchestrator waits (`depends_on: service_healthy`) for the four nodes, and the
edge agent waits for the orchestrator — so starting them in this order avoids
startup races.

## Step 7 — Verify it's running

```bash
docker compose ps          # all 6 containers should be Up (healthy)
```

Check that the Telemetry Daemon has populated Redis (one JSON heartbeat per node):

```bash
for node in us-east1 eu-west1 asia-northeast1 af-south1; do
  echo "=========== $node ==========="
  docker compose exec orchestrator redis-cli GET node:$node | python3 -m json.tool
done
```

Hit the orchestrator endpoints (all require the `X-API-Key` header; `-k` skips
verification of the self-signed cert):

```bash
# Best node for routing
curl -k -H "X-API-Key: your-api-key-here" \
  "https://localhost:8000/get-best-node?agent_id=agent-001"

# Best node for one study: the first call pins the study, later calls return the
# same node while it stays healthy ("rerouted": true if the study had to move)
curl -k -H "X-API-Key: your-api-key-here"   "https://localhost:8000/get-best-node?agent_id=agent-001&study_id=<orthanc-study-id>"

# All registered nodes and their current telemetry
curl -k -H "X-API-Key: your-api-key-here" "https://localhost:8000/nodes"

# Post a manual heartbeat (inject RTT measurements for a node)
curl -k \
  -H "X-API-Key: your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "agent-001", "rtt_dict": {"us-east1": 10000, "eu-west1": 10000, "asia-northeast1": 10000, "af-south1": 1000}}' \
  "https://localhost:8000/heartbeat"
```

## Step 8 — Send a test DICOM

Two simulator scripts model the two ingestion paths. Both read credentials from
`.env`.

**8a. Native DICOM (DIMSE-TLS)** — sends over a DICOM association on port 4242,
exercising the full DICOM protocol stack:

```bash
python -m src.simulator.send_dicom_native --host 127.0.0.1 --port 4242 --called-aet Orthanc_US
# → C-STORE success → Orthanc_US at 127.0.0.1:4242
```

**8b. REST** — posts raw DICOM bytes via `POST /instances` over HTTPS:

```bash
python -m src.simulator.send_dicom_rest --base-url https://127.0.0.1:8042
# → REST send success → https://127.0.0.1:8042 (HTTP 200)
```

**8c. End-to-end through the edge agent** — the real routing path. Send a study to
the edge, then confirm it landed on a cloud node and was deleted from the edge:

```bash
python -m src.simulator.send_dicom_rest --base-url https://localhost:8046   # send to edge

curl -k -u orthanc:your-password https://localhost:8042/instances           # arrived on a cloud node
curl -k -u orthanc:your-password https://localhost:8046/instances           # [] — edge copy deleted
```

**8d. Real DICOM file** — the simulator scripts only generate synthetic 8×8 images,
so to send a real study use the verifier. It uploads the file to the edge, waits
for the forwarder to route it, downloads it back from whichever cloud node won, and
diffs every header tag and pixel against the original (exit `0` = lossless).

```bash
python -m src.utils.verify_pipeline --local-path data/sample_dicom/IM_0001
```

`data/sample_dicom/IM_0001` is a real anonymized MR slice (Philips Ingenia, 480×480,
~460 KB) included in the repo. Drop in any other anonymized DICOM from
[Orthanc's sample datasets](https://orthanc.uclouvain.be/downloads/sample-datasets/)
or [Rubo Medical](https://www.rubomedical.com/dicom_files/) — `*.dcm` is gitignored,
so downloads stay local. Never commit real patient data.

## Step 9 — Inject simulated WAN latency

Adds three WAN metrics per node (latency, jitter, packet loss) modeled on real
Alaska → GCP paths, so routing decisions reflect geographic distance.

| Node | Latency | Jitter | Packet Loss | Path |
|---|---|---|---|---|
| `orthanc-us` | 85 ms | 8 ms | 0.08% | Alaska → US-East (South Carolina) |
| `orthanc-eu` | 165 ms | 17 ms | 0.12% | Alaska → EU-West (Belgium) |
| `orthanc-asia` | 115 ms | 11 ms | 0.08% | Alaska → Asia-Northeast (Tokyo) |
| `orthanc-af` | 300 ms | 35 ms | 0.75% | Alaska → Africa-South (Johannesburg) |

```bash
bash scripts/inject_latency.sh           # apply (NET_ADMIN is already set in compose)
bash scripts/inject_latency.sh --reset   # remove all rules
docker exec orthanc-us tc qdisc show dev eth0   # inspect active rules
```

> `tc netem` delays **outbound** packets, so it captures the download half of the
> RTT from the edge agent's perspective. Rules are **not persistent** — re-run the
> script after any `docker compose up` or container restart.

## Step 10 — Watch a failover

1. Ask for the best node: `curl -k -H "X-API-Key: ..." "https://localhost:8000/get-best-node?agent_id=agent-001"`.
2. Stop that node: `docker compose stop orthanc-<region>`.
3. Wait ~10–30 s (one poll cycle up to the 30 s TTL), then ask again — a **different**
   node is now returned. The stopped node has been excluded from routing.
4. Restart it (`docker compose start orthanc-<region>`) and it rejoins within a poll cycle.

## Step 11 — Run the tests

**Unit tests** — mocked, no Docker:

```bash
python -m pytest tests/unit/ -v -m unit --cov=src --cov-fail-under=80
```

**Lint + type checks:**

```bash
ruff check src/
ruff format --check src/

# mypy runs from inside each service dir because they use bare imports
# (e.g. `from scorer import ...`) that only resolve on that dir's path — the
# same way the containers import at runtime.
for src_dir in src/orchestrator src/edge src/simulator; do
  (cd "$src_dir" && mypy .)
done
```

**Integration tests** — require the full stack (Steps 6–7) to be healthy:

```bash
pytest tests/integration/ -v -m integration
```

**Pre-commit hooks** (ruff, mypy, hadolint, check-yaml/json, trailing-whitespace):

```bash
pre-commit install          # registers hooks once per clone; they run on every commit
pre-commit run --all-files  # run all hooks manually
```

**Load tests (optional)** — need the `load` extra (`pip install -e ".[load]"`):

```bash
locust --config tests/load/locust.conf RoutingUser --users 100 --run-time 1m
```

See [`tests/load/README.md`](../tests/load/README.md) for the full load-test options.

## Step 12 — Developing new code

- **Host code** (`src/simulator`, tests, the scorer under unit test) — the editable
  install means changes are live; just re-run pytest/ruff/mypy.
- **Containerized code** (`src/orchestrator`, `src/edge`) — the images **bake in**
  `src/` at build time, so after editing that code rebuild and restart:

  ```bash
  docker compose up -d --build orchestrator agent-001
  ```

- **Config/template changes** (`config/orthanc/*.template.json`, `config/*/start.sh`,
  `.env`) — restart the affected container so it re-reads them:
  `docker compose restart <service>`.

See [Contributing](../CONTRIBUTING.md) for the branch/PR workflow and scoring-weight
tuning, and [architecture.md](architecture.md) for how routing and dead-node
detection work internally.

---

## Troubleshooting

Start with `docker compose ps` to list all 6 containers and their state. The
`STATUS` column shows `Up (health: starting)`, `Up (healthy)`, `Up (unhealthy)`,
or `Exited`:

- **`Up (health: starting)`** — the service URL isn't responding yet; wait and re-check.
- **`Exited`** — the process crashed; check logs immediately.
- **`Up (unhealthy)`** — the process runs but its health check is failing.
- **Missing from the list** — it failed before Docker could track it; check its logs.

**Get logs:**

```bash
docker compose logs <service>            # all output (most useful after a startup failure)
docker compose logs -f <service>         # follow in real time
docker compose logs --tail=50 <service>  # last 50 lines
```

Service names: `orthanc-us`, `orthanc-eu`, `orthanc-asia`, `orthanc-af`,
`orchestrator`, `agent-001`.

**Inspect health-check output** (`docker compose logs` only shows the main process —
Orthanc or Uvicorn — not the individual health probes):

```bash
docker inspect --format='{{json .State.Health}}' <service> | python3 -m json.tool
```

The `Log` array lists the last five health-check attempts with exit codes and output.

**Common failures:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `orchestrator` exits immediately | `ORCHESTRATOR_API_KEY` missing from `.env` | Add it to `.env`, restart |
| `orchestrator` unhealthy | Redis or Uvicorn not ready within the healthcheck window | `docker compose logs orchestrator`, then `docker compose restart orchestrator` |
| `agent-001` unhealthy | `orchestrator` not healthy yet (`depends_on` blocks it) | Wait for the orchestrator to become healthy first |
| Regional node unhealthy | Missing certs or template substitution failed | `docker compose logs orthanc-<region>`; confirm `certs/` exists (Step 5) |
| TLS errors from host `curl` | Verifying a self-signed cert | Use `-k`, or pass `--cacert certs/ca.pem` |
