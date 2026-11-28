#!/usr/bin/env bash
# Reproducible end-to-end benchmark runner for Diomede.
# Executes the full evaluation suite against the 10-container WAN Docker Compose stack
# and saves raw JSON/CSV artifacts into OUTPUT_DIR (default: ./benchmark_results).
#
# Prerequisites:
#   - Docker Engine + Docker Compose v2 + Linux kernel sch_netem support
#   - Python venv with: pip install -e ".[orchestrator,edge,scripts,load,bench,test]"
#   - .env created from .env.example
#
# Usage:
#   bash scripts/run_benchmarks.sh [output_dir]
set -euo pipefail

OUT="${1:-benchmark_results}"
PY="${PYTHON:-python3}"
LOCUST="${LOCUST:-locust}"
TCIA_DIR="${TCIA_DIR:-/tmp/tcia_lidc_ct}"
mkdir -p "$OUT"

# Keep container-internal REQUESTS_CA_BUNDLE=/certs/ca.pem isolated from host overrides.
unset REQUESTS_CA_BUNDLE || true

# 1. Generate dual-EKU certificates for both edge agents and all cloud nodes
EDGE_AGENTS="agent-001 agent-002" bash scripts/gen_certs.sh

# 2. Launch 10-container stack (4 regional Orthanc + Redis + daemon + orchestrator + 2 edges + HAProxy)
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --build --wait --wait-timeout 240
bash scripts/inject_latency.sh
docker compose -f docker-compose.yml -f docker-compose.bench.yml restart agent-001 agent-002
sleep 15

# 3. Routing correctness (35 deterministic load permutations)
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.routing_correctness \
  | tee "$OUT/routing_correctness.txt"
sleep 12
bash scripts/inject_latency.sh

# 4. Four-way baseline comparison (Static, Round-Robin, HAProxy 2.9, Diomede REST/STOW-RS/DIMSE+SC)
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.baseline_comparison \
  --studies 40 --slices-per-study 10 --slice-kb 256 \
  --simulate-outage --label diomede_rest \
  --output-json "$OUT/bench1_rest.json"

sed -i 's/^FORWARDER_TRANSPORT=.*/FORWARDER_TRANSPORT=stow-rs/' .env
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --no-deps --force-recreate agent-001
sleep 15
bash scripts/inject_latency.sh
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.baseline_comparison \
  --studies 40 --slices-per-study 10 --slice-kb 256 \
  --simulate-outage --diomede-only --label diomede_stow_rs \
  --output-json "$OUT/bench1_stow_rs.json"

sed -i 's/^FORWARDER_TRANSPORT=.*/FORWARDER_TRANSPORT=dimse/' .env
sed -i 's/^FORWARDER_STORAGE_COMMITMENT=.*/FORWARDER_STORAGE_COMMITMENT=true/' .env
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --no-deps --force-recreate agent-001
sleep 15
bash scripts/inject_latency.sh
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.baseline_comparison \
  --studies 40 --slices-per-study 10 --slice-kb 256 \
  --simulate-outage --diomede-only --label diomede_dimse_sc \
  --output-json "$OUT/bench1_dimse_sc.json"

sed -i 's/^FORWARDER_TRANSPORT=.*/FORWARDER_TRANSPORT=rest/' .env
docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d --no-deps --force-recreate agent-001
sleep 12

# 5. Multi-factor weight sensitivity analysis
bash scripts/inject_latency.sh
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.sensitivity_analysis \
  --slices-per-study 8 --slice-kb 256 --repeats 5 \
  --output-csv "$OUT/bench2_sensitivity.csv" | tee "$OUT/bench2_sensitivity.txt"

# 6. WAN degradation (tc netem) and 2% storage exhaustion (60 MB Reject quota)
bash scripts/inject_latency.sh
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.degradation_and_storage \
  --mode netem --output-json "$OUT/bench3_netem.json" | tee "$OUT/bench3_netem.txt"

docker rm -f orthanc-us telemetry-daemon >/dev/null 2>&1 || true
docker compose -f docker-compose.yml -f docker-compose.bench.yml -f docker-compose.storage-test.yml \
  up -d --no-deps --wait orthanc-us telemetry-daemon
sleep 15
bash scripts/inject_latency.sh
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.degradation_and_storage \
  --mode storage --slice-kb 512 --max-studies 160 \
  --output-json "$OUT/bench3_storage.json" | tee "$OUT/bench3_storage.txt"

docker rm -f orthanc-us telemetry-daemon >/dev/null 2>&1 || true
docker compose -f docker-compose.yml -f docker-compose.bench.yml \
  up -d --no-deps --wait orthanc-us telemetry-daemon
sleep 15
bash scripts/inject_latency.sh

# 7. Sustained 100-client control-plane latency via Locust (180 s steady + 30 s ramp-down)
cp certs/ca.pem /tmp/diomede_ca.pem
LOAD_CA_CERT=/tmp/diomede_ca.pem "$LOCUST" --config tests/load/locust.conf RoutingUser \
  --processes 4 --users 100 --run-time 3m \
  --csv="$OUT/bench4_locust" --html="$OUT/bench4_locust.html" | tee "$OUT/bench4_locust.txt" || true

# 8. Real de-identified TCIA Chest CT (LIDC-IDRI, 133 slices + GSPS) + SPOF + synthetic burst
"$PY" scripts/fetch_tcia_sample.py --output-dir "$TCIA_DIR" --add-gsps | tee "$OUT/tcia_sample.json"
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.clinical_and_spof \
  --dicom-dir "$TCIA_DIR" \
  --output-json "$OUT/bench6_tcia_clinical_spof.json" | tee "$OUT/bench6_tcia_clinical_spof.txt"

REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.success_rate \
  --burst-count 1000 --file-size-kb 50 --timeout-s 240 | tee "$OUT/success_rate_baseline.txt"
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.success_rate \
  --burst-count 1000 --file-size-kb 50 --simulate-outage --outage-duration-s 15 --timeout-s 240 \
  | tee "$OUT/success_rate_outage.txt"

# 9. Randomized-phase failover & recovery (N=15 trials x 4 regions x 2 modes = 120 trials)
REQUESTS_CA_BUNDLE=certs/ca.pem "$PY" -m src.benchmark.failover_randomized \
  --trials 15 --output-json "$OUT/bench5_failover_randomized.json" \
  | tee "$OUT/bench5_failover_randomized.txt"
bash scripts/inject_latency.sh

echo "Benchmark suite complete. Artifacts saved in $OUT"
