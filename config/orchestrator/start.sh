#!/bin/bash
# start.sh – Orchestrator container entrypoint
set -e

echo "[start.sh] Starting Orchestrator..."

echo "[start.sh] Starting Redis server..."
redis-server --daemonize yes

echo "[start.sh] Waiting for Redis to be ready..."
until redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.1; done

echo "[start.sh] Starting Telemetry Daemon..."
# python3 /app/daemon.py &
# DAEMON_PID=$!

echo "[start.sh] Starting Uvicorn for FastAPI endpoints..."
exec uvicorn main:app --host 0.0.0.0 --port 8000
