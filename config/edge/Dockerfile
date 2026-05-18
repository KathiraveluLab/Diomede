# FROM jodogne/orthanc:latest
FROM orthancteam/orthanc:26.4.2

# Install Python 3.11 + pip on top of the Orthanc (Ubuntu-based) image.
# Both Orthanc and the Forwarder Daemon run inside this single container,
# mirroring the production edge VM layout where they share the same host.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-venv \
       python3-pip \
       wget \
       gettext-base \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY config/edge/requirements.txt .

# Create the virtual environment
RUN python3 -m venv /opt/venv

# Ensure the app uses the venv's python/pip automatically
ENV PATH="/opt/venv/bin:$PATH"

RUN pip3 install --no-cache-dir -r requirements.txt

COPY config/edge/start.sh .
RUN chmod +x start.sh

# 8042 = Orthanc REST  |  4242 = Orthanc DICOM (legacy scanner C-STORE)
EXPOSE 8042 4242

# Override the default Orthanc entrypoint so we can launch both processes.
ENTRYPOINT ["./start.sh"]
