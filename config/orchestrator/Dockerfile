# Based on the Python 3.11 slim image, and installs the necessary dependencies for the Orchestrator to function.
FROM python:3.11-slim

# Install redis-tools for redis-cli
RUN apt-get update && apt-get install -y redis-server redis-tools && rm -rf /var/lib/apt/lists/*

# FASTAPI, Redis, and Telemetry Daemon all run in this container
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

# 8000 = Orchestrator FastAPI service
EXPOSE 6379 8000

# Override the default Orthanc entrypoint so we can launch both processes.
ENTRYPOINT ["./start.sh"]
