# Based on the Python 3.11 slim image, and installs the necessary dependencies for the Orchestrator to function.
FROM python:3.11-slim

# Install redis-tools for redis-cli
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends redis-server redis-tools && rm -rf /var/lib/apt/lists/*

# FastAPI, Redis, and Telemetry Daemon all run in this container
WORKDIR /app

COPY src/orchestrator/ .
COPY config/orchestrator/requirements.txt .

# Create the virtual environment
RUN python3 -m venv /opt/venv

# Ensure the app uses the venv's python/pip automatically
ENV PATH="/opt/venv/bin:$PATH"

RUN pip3 install --no-cache-dir -r requirements.txt

COPY config/orchestrator/start.sh .
RUN chmod +x start.sh

# 6379 for Redis, 8000 for FastAPI
EXPOSE 8000

# Launch the Orchestrator processes.
ENTRYPOINT ["./start.sh"]
