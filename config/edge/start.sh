#!/bin/bash
# start.sh – Edge Agent container entrypoint
# Substitutes credentials into the Orthanc config template, then launches
# Orthanc (DICOM receiver) and the Forwarder Daemon side-by-side.

set -e

echo "[start.sh] Generating Orthanc config from template..."
python3 -c "
import os
t = open('/etc/orthanc/orthanc.template.json').read()
t = t.replace('\${orthanc_user}', os.environ['ORTHANC_USER'])
t = t.replace('\${orthanc_password}', os.environ['ORTHANC_PASSWORD'])
open('/tmp/orthanc.json', 'w').write(t)
"

echo "[start.sh] Starting Orthanc..."
Orthanc /tmp/orthanc.json &
ORTHANC_PID=$!

echo "[start.sh] Starting Forwarder Daemon..."
python -m src.edge.forwarder &
FORWARDER_PID=$!


wait -n $ORTHANC_PID $FORWARDER_PID
EXIT_CODE=$?

echo "[start.sh] A process exited with code $EXIT_CODE – shutting down."
kill $ORTHANC_PID $FORWARDER_PID 2>/dev/null || true
exit $EXIT_CODE
