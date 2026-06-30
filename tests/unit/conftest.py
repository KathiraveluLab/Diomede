import os

# Set required env vars before any test module imports service code that
# validates them at module level (e.g. src.edge.forwarder, src.orchestrator.main).
os.environ.setdefault("ORCHESTRATOR_API_KEY", "test-key")
os.environ.setdefault("AGENT_ID", "test-agent")
