#!/bin/bash
# start.sh – Orchestrator container entrypoint
set -e

echo "[start.sh] Starting Orchestrator..."

echo "[start.sh] Starting Redis server..."
# AOF in /data (a named volume in docker-compose.yml) keeps RTTs and study pins
# across orchestrator restarts. uvicorn is PID 1, so Redis stops with the container.
mkdir -p /data
redis-server --daemonize yes --appendonly yes --dir /data

echo "[start.sh] Waiting for Redis to be ready..."
until redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.1; done

echo "[start.sh] Starting Telemetry Daemon..."
python3 -m src.orchestrator.daemon &
DAEMON_PID=$!

echo "[start.sh] Starting Uvicorn for FastAPI endpoints..."
exec uvicorn src.orchestrator.main:app --host 0.0.0.0 --port 8000 \
    --ssl-keyfile  /certs/server.key \
    --ssl-certfile /certs/server.crt
