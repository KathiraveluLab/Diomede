# Diomede Dynamic DICOM Endpoints – Local Test Environment

This document walks through spinning up the full 6-container simulation
environment so you can:

- Run all the code snippets against live Orthanc instances
- Watch routing decisions happen in real time
- Kill a node mid-transfer to verify failover
- Inject realistic WAN latency with `tc netem`

---

## Container Topology

| Container | Role | Host Ports |
|---|---|---|
| `orthanc-us` | Cloud PACS node (GCP us-east1) | REST 8042 · DICOM 4242 |
| `orthanc-eu` | Cloud PACS node (GCP eu-west1) | REST 8043 · DICOM 4243 |
| `orthanc-asia` | Cloud PACS node (GCP asia-northeast1) | REST 8044 · DICOM 4244 |
| `orthanc-af` | Cloud PACS node (GCP af-south1) | REST 8045 · DICOM 4245 |
| `orchestrator` | Redis + Telemetry Daemon + FastAPI (co-located) | 8000 |
| `edge-agent` | Edge Orthanc + Forwarder Daemon (co-located) | REST 8046 · DICOM 4246 |

The **Orchestrator container** runs three co-located processes, mirroring the
production VM where all three always live on the same host:

- `redis-server` – node registry; keys have 30 s TTL (expired key = dead node),
  bound to `127.0.0.1` inside the container only
- `daemon.py` – async Telemetry Daemon; polls all four cloud Orthanc nodes every
  10 s and writes JSON heartbeats to Redis over `localhost`
- `main.py` (via `uvicorn`) – FastAPI Orchestrator; reads Redis over `localhost`
  and serves `GET /get-best-node`, `POST /heartbeat`, `GET /nodes`

The **Edge Agent** is a single container (`edge-agent`) running two co-located
processes, which has the same pattern as the Orchestrator container:

- **Edge Orthanc** - standard Orthanc PACS; legacy scanners (or the simulator
  script) send DICOM C-STORE here on port 4246
- **Forwarder Daemon** - polls Orthanc's `/changes` every 5 s on `localhost:8042`,
  downloads new instances, queries the Orchestrator, and forwards to the winning
  cloud node

They share the same network namespace so the Forwarder talks to Orthanc on
`localhost` with zero network hop. This mirrors the production edge VM where
both processes run on the same host and simplifies the security boundary.

---

## Prerequisites

- Docker Desktop ≥ 4.x (Mac/Windows) **or** Docker Engine + Compose plugin (Linux)
- `docker compose version` should print `v2.x`
- Python 3.12 or 3.13 on your host

---

## Local Development Setup

Unit tests run entirely on your host and no Docker is required.  Integration tests
require the full Docker stack (see Quick Start below).

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### 2. Install all dependencies

```bash
pip install -e ".[orchestrator,edge,scripts,test,dev]"
```

The `-e` flag installs the package so Python imports resolve directly from your working directory so edits to source files take effect immediately without reinstalling. The groups after the dot pull in optional dependencies:
- `orchestrator` - fastapi, uvicorn, redis, httpx, pydantic
- `edge` - httpx
- `scripts` - httpx, pydicom (for `src/simulator/send_test_dicom.py`)
- `test` - pytest, fakeredis, respx, pytest-cov, and friends
- `dev` - ruff, mypy, types-redis, pre-commit


### 3. Verify the Telemetry Daemon

Iterate through all nodes to see the values stored in Redis of the Orchestrator
```bash
for node in us-east1 eu-west1 asia-northeast1 af-south1; do
  echo "============ $node ==============="
  docker compose exec orchestrator redis-cli GET node:$node | python3 -m json.tool
done
```

### 4. Run unit tests

No Docker needed. All tests use mocks:

```bash
python -m pytest tests/unit/ -v -m unit --cov=src --cov-fail-under=80
```

### 5. Run linting and type checks

```bash
# Ruff linter + formatter check
ruff check src/
ruff format --check src/

# mypy type check
# Each subdirectory uses bare imports (e.g. `from scorer import ...`) that only
# resolve when that directory is on the Python path.  Running mypy from *inside*
# each directory replicates what Docker does at runtime.
for src_dir in src/orchestrator src/edge src/simulator; do
  (cd "$src_dir" && mypy .)
done

.venv/bin/python -m mypy src/orchestrator src/edge src/simulator
```

To run all pre-commit hooks (check-yaml, check-json, hadolint, ruff, mypy,
trailing-whitespace, etc.) install the tool and register it with git once:

```bash
pre-commit install          # registers hooks in .git/hooks, which runs once per clone
```

Then run all hooks manually against every file:

```bash
pre-commit run --all-files
```

After `pre-commit install`, hooks also run automatically on every `git commit`,
blocking the commit if any check fails.

### 5. Run integration tests

Requires the full 6-container stack to be healthy (see Quick Start below).
Start the stack first, then:

```bash
pytest tests/integration/ -v -m integration
```

---

## Quick Start

### 1. Create your `.env` file

The Orthanc nodes require credentials.  Copy the example file and set a
password before starting any containers:

```bash
cp .env.example .env
```

Open `.env` and replace every `CHANGE_ME_IN_PRODUCTION` with strong values:

```ini
ORTHANC_USER=orthanc
ORTHANC_PASSWORD=your-strong-password-here
ORCHESTRATOR_API_KEY=your-strong-api-key-here
```

`ORTHANC_PASSWORD` - the config templates in `src/config/orthanc/*.template.json` substitute
`${ORTHANC_USER}` and `${ORTHANC_PASSWORD}` at container startup, so every Orthanc
node shares the same credentials from this single file.

`ORCHESTRATOR_API_KEY` - required by both the Orchestrator (enforced on every
endpoint) and the Forwarder (sent as `X-API-Key`). Both services fail at startup
if the variable is missing.

`.env` is gitignored and must never be committed.

### 2. Deploy TLS

End-to-end TLS covers both local Docker and distributed GCP deployments.
- Four cloud Orthanc nodes enabling DIMSE-TLS with native `SslEnabled` in Orthanc config
- Orchestrator FastAPI enabling SSL with `--ssl-keyfile / --ssl-certfile` of Uvicorn
- All httpx clients (Daemon, Forwarder) using SSL_CERT_FILE / REQUESTS_CA_BUNDLE env variables

Note the following stays plain HTTP intentionally:
- Redis — bound to `127.0.0.1` inside the Orchestrator container only

Generate certificates using the following script:

```bash
bash scripts/gen_certs.sh
```

This output certificates below:

```
certs/
├── ca.key                          # CA private key — never commit or share
├── ca.pem                          # CA public cert — distribute to any client that needs to verify servers
├── orchestrator/
│   ├── server.crt
│   └── server.key
├── orthanc-us/combined.pem         # cert + key in one file (Orthanc's required format)
├── orthanc-eu/combined.pem
├── orthanc-asia/combined.pem
├── orthanc-af/combined.pem
├── edge-agent/combined.pem         # Edge Orthanc uses the same combined format
└── diomede-client/
    ├── client.crt                  # clientAuth certificate — used by the simulator
    └── client.key
```

`certs/` is gitignored.  Re-run `gen_certs.sh` on every fresh clone.

You can view generated certificates with `openssl` (`x.509` for public key certificates and `rsa` for private key certificates):
```bash
# Root ca certificate
openssl x509 -in certs/ca.pem -text -noout
openssl rsa -in certs/ca.key -text -noout

# Public certificate
openssl x509 -in certs/diomede-client/client.crt -text -noout
openssl x509 -in certs/edge-agent/server.crt -text -noout
openssl x509 -in certs/orchestrator/server.crt -text -noout
openssl x509 -in certs/orthanc-us/server.crt -text -noout
openssl x509 -in certs/orthanc-eu/server.crt -text -noout
openssl x509 -in certs/orthanc-asia/server.crt -text -noout
openssl x509 -in certs/orthanc-af/server.crt -text -noout

# Private key (no password for being used on the server)
openssl rsa -in certs/diomede-client/client.key -text -noout
openssl rsa -in certs/edge-agent/server.key -text -noout
openssl rsa -in certs/orchestrator/server.key -text -noout
openssl rsa -in certs/orthanc-us/server.key -text -noout
openssl rsa -in certs/orthanc-eu/server.key -text -noout
openssl rsa -in certs/orthanc-asia/server.key -text -noout
openssl rsa -in certs/orthanc-af/server.key -text -noout

# Combined server certificate
openssl x509 -in certs/edge-agent/combined.pem -text -noout
openssl rsa -in certs/edge-agent/combined.pem
openssl x509 -in certs/orchestrator/combined.pem -text -noout
openssl rsa -in certs/orchestrator/combined.pem
openssl x509 -in certs/orthanc-us/combined.pem -text -noout
openssl rsa -in certs/orthanc-us/combined.pem
openssl x509 -in certs/orthanc-eu/combined.pem -text -noout
openssl rsa -in certs/orthanc-eu/combined.pem
openssl x509 -in certs/orthanc-asia/combined.pem -text -noout
openssl rsa -in certs/orthanc-asia/combined.pem
openssl x509 -in certs/orthanc-af/combined.pem -text -noout
openssl rsa -in certs/orthanc-af/combined.pem
```

### 3. Pull and start the 4 regional Orthanc nodes

The cloud PACS nodes use the pre-built `orthancteam/orthanc:26.4.2` image from
Docker Hub:
```bash
docker pull orthancteam/orthanc:26.4.2
```

To start only the 4 regional Orthanc nodes:
```bash
docker compose up -d orthanc-us orthanc-eu orthanc-asia orthanc-af

```

Before starting Orchestrator container, make sure nodes are healthy and reachable to
populate Redis:
```bash
docker compose ps
```

Since `orchestrator` and `edge-agent` have local Dockerfiles use `build`:
```bash
docker compose build orchestrator edge-agent && docker compose up -d orchestrator edge-agent

docker compose build orchestrator && docker compose up -d orchestrator
docker compose build edge-agent && docker compose up -d edge-agent
```

### 4. Inject Simulated WAN Latency

The script injects three WAN metrics per node (latency, jitter, and packet loss) modeled after real Alaska → GCP paths:

| Node | Latency | Jitter | Packet Loss | Rationale |
|---|---|---|---|---|
| `orthanc-us` | 85ms | 8ms | 0.08% | Alaska → US-East (Moncks Corner, South Carolina) |
| `orthanc-eu` | 165ms | 17ms | 0.12% | Alaska → EU-West (St. Ghislain, Belgium) |
| `orthanc-asia` | 115ms | 11ms | 0.08% | Alaska → Asia-Northeast (Tokyo, Japan) |
| `orthanc-af` | 300ms | 35ms | 0.75% | Alaska → Africa-South (Johannesburg, South Africa) |

> **Note:** `tc netem` delays *outbound* packets from the container (responses leaving that Orthanc node), so it captures the download half of the RTT from the Edge Agent's perspective.

Apply the rules (`NET_ADMIN` is already set in `docker-compose.yml`):

```bash
bash scripts/inject_latency.sh
```

Verify latency is applied with a timed REST call:

```bash
python3 - << 'EOF'
import urllib.request, ssl, base64, time

env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k] = v

auth = base64.b64encode(f"{env['ORTHANC_USER']}:{env['ORTHANC_PASSWORD']}".encode()).decode()
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# HTTPS measures ~3.5x the injected one-way delay: tc netem delays every
# outbound packet, and a full HTTPS request hits 3-4 of them (TCP SYN-ACK,
# TLS handshake, response).
for name, port, injected, expect in [('us', 8042, 85, 300), ('eu', 8043, 165, 600), ('asia', 8044, 115, 450), ('af', 8045, 300, 1000)]:
    req = urllib.request.Request(f'https://localhost:{port}/system')
    req.add_header('Authorization', f'Basic {auth}')
    t = time.time()
    urllib.request.urlopen(req, context=ctx)
    ms = (time.time() - t) * 1000
    print(f'orthanc-{name:<5} :{port}  {ms:6.0f}ms  (injected {injected}ms one-way, expect ~{expect}ms total)')
EOF
```

Inspect the active rules on each node:

```bash
docker exec orthanc-us tc qdisc show dev eth0
docker exec orthanc-eu tc qdisc show dev eth0
docker exec orthanc-asia tc qdisc show dev eth0
docker exec orthanc-af tc qdisc show dev eth0
```

To remove all latency rules:

```bash
bash scripts/inject_latency.sh --reset
```

> **Note:** These rules are not persistent — re-run `bash scripts/inject_latency.sh` after every `docker compose up` or container restart.

### 5. Send a test DICOM

Two simulator scripts are provided, each representing a different ingestion path.

#### 5a. Native DICOM (DIMSE-TLS) — `send_dicom_native`

Sends directly to a cloud node over a DIMSE-TLS association on port 4242.
Use this to test the DICOM protocol stack end-to-end.

```bash
python -m src.simulator.send_dicom_native \
  --host 127.0.0.1 \
  --port 4242 \
  --called-aet Orthanc_US
```

On success:

```
C-STORE success → Orthanc_US at 127.0.0.1:4242
```

#### 5b. REST simulator — `send_dicom_rest`

Posts raw DICOM bytes directly to an Orthanc node via `POST /instances` over HTTPS.
Credentials are read from `ORTHANC_USER` / `ORTHANC_PASSWORD` in `.env`.

```bash
python -m src.simulator.send_dicom_rest \
  --base-url https://127.0.0.1:8042
```

On success:

```
REST send success → https://127.0.0.1:8042 (HTTP 200)
```

#### 6. Access FastAPI endpoints in Orchestrator

All endpoints require the `X-API-Key` header matching `ORCHESTRATOR_API_KEY` from your `.env`.
Use `-k` to skip certificate verification against the self-signed cert:

```bash
# Get the best node for routing
curl -k -H "X-API-Key: your-api-key-here" "https://localhost:8000/get-best-node?agent_id=edge-agent"

# List all registered nodes and their current telemetry
curl -k -H "X-API-Key: your-api-key-here" "https://localhost:8000/nodes"

# Post a manual heartbeat for a node
 curl -k \
  -H "X-API-Key: your-api-key-here" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "edge-agent", "rtt_dict": {"us-east1": 10000, "eu-west1": 10000, "asia-northeast1": 10000, "af-south1": 1000}}' \
  "https://localhost:8000/heartbeat"
```

Stop Docker container hosting the best node, then run the command above, wait for 30 seconds,
another node should be the best node. This confirms the failover scenario.

#### 7. Probe RTT for all nodes

To measure and print the current round-trip time from your machine to each Orthanc node:

```bash
.venv/bin/python - << 'EOF'
import asyncio, time, httpx, ssl, json

ctx = ssl.create_default_context(cafile="certs/ca.pem")
auth = ("orthanc", "CHANGE_IN_PRODUCTION")
nodes = {
    "us-east1":        "https://localhost:8042",
    "eu-west1":        "https://localhost:8043",
    "asia-northeast1": "https://localhost:8044",
    "af-south1":       "https://localhost:8045",
}

async def probe():
    results = {}
    async with httpx.AsyncClient(verify=ctx) as client:
        for node_id, base in nodes.items():
            t0 = time.monotonic()
            await client.get(f"{base}/system", auth=auth, timeout=5)
            rtt_ms = (time.monotonic() - t0) * 1000
            results[node_id] = round(rtt_ms, 1)
    print(json.dumps(results, indent=2))

asyncio.run(probe())
EOF
```

Expected output (values will be low since all nodes are local):

```json
{
  "us-east1": 3.2,
  "eu-west1": 4.1,
  "asia-northeast1": 3.8,
  "af-south1": 5.0
}
```

---

## Troubleshooting

### Docker Containers

Start by running `docker compose ps` to list all 6 containers and their current state.

```bash
docker compose ps
```

The `STATUS` column shows `Up (health: starting)`, `Up (healthy)`, `Up (unhealthy)`, or `Exited`.
- `Up (health: starting)` means the service URL is not responding yet; wait a few seconds and re-check.
- An `Exited` status means the process crashed; check logs immediately.
- `Up (unhealthy)` means the process is running but the healthcheck is failing.

Look for the following cases:

- **Missing containers** — a service that does not appear in the list failed to start before Docker could track it; check logs with `docker compose logs <service>`.
- **Unhealthy containers** — the process is running but the health check is failing; check logs with `docker inspect <service>`

#### Get logs when a container fails to start

```bash
# All output from one service (most useful after a startup failure)
docker compose logs <service>

# Follow in real time (Ctrl-C to stop)
docker compose logs -f <service>

# Last 50 lines only
docker compose logs --tail=50 <service>

# All services at once
docker compose logs
```

Any of the six service names works: `orthanc-us`, `orthanc-eu`, `orthanc-asia`, `orthanc-af`, `orchestrator`, `edge-agent`.

#### Check container status and health

> **Note:** `docker compose logs` only captures output from the main service process (Orthanc or Uvicorn). To see the output of individual health-check queries including the exact HTTP requests and return codes, use `docker inspect`:
>
> ```bash
> docker inspect --format='{{json .State.Health}}' <service> | python3 -m json.tool
> ```
>
> The `Log` array in the output lists the last five health-check attempts with their exit codes and stdout/stderr.

Some services only become healthy after their dependencies are healthy (e.g. `edge-agent` waits for `orchestrator`). If the dependency is unhealthy, fix it first, then restart the dependent service.

```bash
docker compose restart <service>
```

Some common failures and their fixes are:

| Symptom | Likely cause | Fix |
|---|---|---|
| `orchestrator` exits immediately | `ORCHESTRATOR_API_KEY` missing from `.env` | Add it to `.env` |
| `orchestrator` unhealthy | Redis or uvicorn not ready within healthcheck window | `docker compose logs orchestrator` to confirm, then `docker compose restart orchestrator` |
| `edge-agent` unhealthy | `orchestrator` not healthy yet (`depends_on` blocks it) | Wait for orchestrator to become healthy first |
| Regional node unhealthy | Template substitution failed (bad variable replacement) or Orthanc config error | Check logs: `docker compose logs orthanc-<region>` |

---
