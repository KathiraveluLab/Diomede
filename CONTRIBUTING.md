# Contributing to Diomede – Dynamic DICOM Endpoints

Thank you for contributing! This project is developed as part of **Google Summer of Code 2026** with the University of Alaska Anchorage / KathiraveluLab.

---

## Documentation

- Document any user-facing change in the relevant component `README` or `setup.md`.
- API changes to the FastAPI route `summary=` strings and Pydantic models will update
  the live Swagger UI at `http://localhost:8000/docs`. After any endpoint or model change,
  regenerate the static API files at `docs/api.html` and `docs/openapi.json`.

---

## Code Style

This project uses **ruff** for linting/formatting and **mypy** for type checking. mypy runs in strict mode for all production code (`src/orchestrator/`, `src/edge/`); the simulator scripts (e.g., `send_test_dicom.py`) are exempt. Run both before pushing:

```bash
ruff check --fix src/ tests/
ruff format src/ tests/
for dir in src/orchestrator src/edge src/simulator; do (cd "$dir" && mypy .); done
```


Use `logging` throughout, never `print()`. Pick the right level:
- `log.debug()` for transient diagnostic output
- `log.info()` for normal operational events (routing decisions, probe results)
- `log.warning()` for recoverable problems (node unreachable, probe failed)
- `log.error()` for data loss scenarios (failed forwarding, unknown node ID)

Never include PHI in log output. Log instance IDs, node IDs, and scores only.
See the [PHI Policy](#phi-policy) section for full rules.

Install pre-commit hooks to catch issues automatically:

```bash
pip install pre-commit
pre-commit install
```

---

## Setting Up the Dev Environment

```bash
git clone https://github.com/KathiraveluLab/Diomede.git
cd Diomede

# Copy and configure credentials
cp .env.example .env
# edit .env — set ORTHANC_PASSWORD to something other than the default

# Install all extras for local development
pip install -e ".[orchestrator,edge,scripts,test]"

# Start the full Docker Compose simulation stack
docker compose up -d --build
```

---

## Running Tests

```bash
# Unit tests (no Docker needed)
pytest tests/unit/ -v

# Integration tests (requires running stack)
pytest tests/integration/ -v

# Load test with locust
locust --config tests/load/locust.conf
```

---

## Commit Messages

Write meaningful commit messages that explain *why*, not just *what*:

```
# Good
fix: exclude nodes with disk_free_mb == 0 from healthy set

# Not as useful
fix: update daemon.py
```

---

## Pull Requests

- Fork the upstream repository and develop in a feature branch on your fork. Open PRs targeting `main` on the upstream repository.
- Keep PRs minimal: avoid unrelated whitespace changes, unused imports, or reformatting of unchanged files. Run `git diff` before committing.
- Each PR should include or update the relevant unit tests. CI enforces ≥ 80% coverage on `src/`.
- Integration tests are triggered automatically on PRs to `main`.

---

## Reporting Issues & Questions

- Bugs and enhancement requests: [GitHub Issues](https://github.com/KathiraveluLab/Diomede/issues)
- Design questions and GSoC discussions: [GitHub Discussions](https://github.com/KathiraveluLab/Diomede/discussions)

---

## PHI Policy

PHI (Protected Health Information) includes patient name, date of birth,
accession number in the groups `(0010,xxxx)` and `(0008,xxxx)`.
The rules below apply to all code, logs, and committed files.

**Committed files**
- Never commit real DICOM files that contain PHI.
- All files under `data/synthetic/` are algorithmically generated with no
  patient data.
- Real DICOM samples (e.g., `data/samples/`) must be fully anonymized
  before inclusion. If unsure, do not commit the file.

**Logs**
- Never log PHI. Log only routing metadata: Orthanc instance IDs, node IDs,
  scores, queue sizes, and disk metrics.
- If a DICOM tag value must appear in a log message for debugging, replace it
  with a truncated hash (e.g., `sha1(value)[:8]`).

**API responses**
- Orchestrator endpoints (`/nodes`, `/get-best-node`, `/heartbeat`) return
  infrastructure metrics only including node IDs, scores, queue depth, disk space,
  and RTT (Round Trip Time). They must never proxy or surface DICOM tag values.
- If a new endpoint needs to reference a study or series, use the Orthanc
  instance ID (an opaque SHA-1 hash) instead of the raw DICOM UIDs or patient
  identifiers.
