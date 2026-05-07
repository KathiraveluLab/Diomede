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

### 3. Run unit tests

No Docker needed. All tests use mocks:

```bash
python -m pytest tests/unit/ -v -m unit --cov=src --cov-fail-under=80
```

### 4. Run linting and type checks

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

### 2. Pull and start the 4 regional Orthanc nodes

The cloud PACS nodes use the pre-built `orthancteam/orthanc:latest` image from
Docker Hub:

```bash
docker pull orthancteam/orthanc:latest
```
