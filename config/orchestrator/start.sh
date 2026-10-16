#!/bin/bash
# start.sh – Orchestrator container entrypoint
# Redis and the Telemetry Daemon run as their own services (see docker-compose.yml).
set -e

echo "[start.sh] Starting Uvicorn for FastAPI endpoints..."
exec uvicorn src.orchestrator.main:app --host 0.0.0.0 --port 8000 \
    --ssl-keyfile  /certs/server.key \
    --ssl-certfile /certs/server.crt
