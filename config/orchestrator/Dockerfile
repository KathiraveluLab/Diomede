# Based on the Python 3.11 slim image, and installs the necessary dependencies for the Orchestrator to function.
FROM python:3.11-slim

# The same image also runs the Telemetry Daemon as a separate service; Redis has its own container
WORKDIR /app

COPY src/orchestrator/ src/orchestrator/
COPY src/utils/ src/utils/
COPY config/orchestrator/requirements.txt .

# Create the virtual environment
RUN python3 -m venv /opt/venv

# Ensure the app uses the venv's python/pip automatically
ENV PATH="/opt/venv/bin:$PATH"

RUN pip3 install --no-cache-dir -r requirements.txt

COPY config/orchestrator/start.sh .
RUN chmod +x start.sh

# 8000 for FastAPI
EXPOSE 8000

# Launch the Orchestrator API.
ENTRYPOINT ["./start.sh"]
