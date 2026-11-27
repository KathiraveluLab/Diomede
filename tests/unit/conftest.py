import os

# Set required env vars before any test module imports service code that
# validates them at module level (e.g. src.edge.forwarder, src.edge.orthanc_source,
# src.orchestrator.daemon, src.orchestrator.main -- all use require_env(), which
# raises RuntimeError at import time if a key is missing).
os.environ.setdefault("ORCHESTRATOR_API_KEY", "test-key")
os.environ.setdefault("AGENT_ID", "test-agent")
os.environ.setdefault("ORCHESTRATOR_BASE", "http://orchestrator:8000")

# Empty counts as unset, and load_dotenv won't replace it with a CLOUD_NODES from a local .env.
os.environ["CLOUD_NODES"] = ""

os.environ.setdefault("NODE_US_USER", "orthanc")
os.environ.setdefault("NODE_US_PASS", "orthanc")
os.environ.setdefault("NODE_EU_USER", "orthanc")
os.environ.setdefault("NODE_EU_PASS", "orthanc")
os.environ.setdefault("NODE_ASIA_USER", "orthanc")
os.environ.setdefault("NODE_ASIA_PASS", "orthanc")
os.environ.setdefault("NODE_AF_USER", "orthanc")
os.environ.setdefault("NODE_AF_PASS", "orthanc")

os.environ.setdefault("EDGE_BASE", "http://localhost:8042")
os.environ.setdefault("EDGE_USER", "orthanc")
os.environ.setdefault("EDGE_PASS", "orthanc")

os.environ.setdefault("FORWARDER_POLL_INTERVAL_S", "5")
os.environ.setdefault("PROBE_INTERVAL_S", "3600")

os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("DAEMON_POLL_INTERVAL_S", "10")
os.environ.setdefault("REDIS_TTL_S", "30")
os.environ.setdefault("HTTP_TIMEOUT_S", "5")

# src/simulator/send_dicom_native.py, send_dicom_rest.py: these load_dotenv()
# from a real .env at import time, so tests relying only on sys.argv patching
# (not an explicit patch.dict) would otherwise depend on a real .env existing --
# works by accident locally, breaks in CI where no .env file is created.
os.environ.setdefault("ORTHANC_USER", "orthanc")
os.environ.setdefault("ORTHANC_PASSWORD", "CHANGE_IN_PRODUCTION")
os.environ.setdefault("ORTHANC_DICOM_HOST", "localhost")
os.environ.setdefault("ORTHANC_DICOM_PORT", "4242")
os.environ.setdefault("ORTHANC_CALLED_AET", "Orthanc_US")
os.environ.setdefault("ORTHANC_CALLING_AET", "Simulator")
os.environ.setdefault("ORTHANC_BASE_URL", "https://localhost:8042")
