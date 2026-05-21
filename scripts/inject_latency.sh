#!/bin/bash
# inject_latency.sh (Bash 3.2+ Compatible)
#
# Simulates realistic WAN conditions from an Alaska edge node to four GCP regions.
# Latency/jitter model: netem delay with 25% correlation.
# Packet loss model: netem loss with 25% correlation.
#
# Usage:
#   ./inject_latency.sh           # apply rules
#   ./inject_latency.sh --reset   # remove all rules
#   ./inject_latency.sh --show    # print current tc rules for each node
set -euo pipefail

ACTION=${1:-""}

# Format: "container_name|latency jitter|loss%"
# Latency values reflect RTT from Alaska to each GCP region:
#   us     – Alaska → US-East (Moncks Corner, South Carolina, ~85 ms)
#   eu     – Alaska → Seattle → transatlantic → St. Ghislain, Belgium (~165 ms)
#   asia   – Alaska → trans-Pacific cable → Tokyo (~115 ms)
#   af     – Alaska → multi-hop → Johannesburg (~300 ms, most variable)
NODES=(
  "orthanc-us|85ms 8ms|0.08%"
  "orthanc-eu|165ms 17ms|0.12%"
  "orthanc-asia|115ms 11ms|0.08%"
  "orthanc-af|300ms 35ms|0.75%"
)

for ENTRY in "${NODES[@]}"; do
  CONTAINER="${ENTRY%%|*}"
  REST="${ENTRY#*|}"
  DELAY_FULL="${REST%%|*}"
  LOSS="${REST##*|}"

  if ! docker exec "$CONTAINER" sh -c 'command -v tc' > /dev/null 2>&1; then
    echo "Installing iproute2 in $CONTAINER..."
    docker exec -e DEBIAN_FRONTEND=noninteractive -u root "$CONTAINER" apt-get update -qq > /dev/null 2>&1
    docker exec -e DEBIAN_FRONTEND=noninteractive -u root "$CONTAINER" apt-get install -y -qq iproute2 > /dev/null 2>&1
  fi

  if [ "$ACTION" = "--reset" ]; then
    echo "Removing tc rules from $CONTAINER..."
    docker exec "$CONTAINER" tc qdisc del dev eth0 root 2>/dev/null || true

  elif [ "$ACTION" = "--show" ]; then
    echo "[$CONTAINER]"
    docker exec "$CONTAINER" tc qdisc show dev eth0

  else
    DELAY_MS="${DELAY_FULL%% *}"
    JITTER_MS="${DELAY_FULL##* }"

    echo "Injecting delay=${DELAY_MS} jitter=${JITTER_MS} loss=${LOSS} into ${CONTAINER}..."

    # Remove existing rules (idempotent)
    docker exec "$CONTAINER" tc qdisc del dev eth0 root 2>/dev/null || true

    # 25% correlation on both delay and loss clusters consecutive drops,
    # matching real WAN behaviour better than independent random loss.
    docker exec "$CONTAINER" \
      tc qdisc add dev eth0 root netem \
        delay "${DELAY_MS}" "${JITTER_MS}" 25% \
        loss "${LOSS}" 25%

    echo "  [ok] ${CONTAINER}: delay=${DELAY_MS} +/-${JITTER_MS} loss=${LOSS}"
  fi
done

echo ""
case "$ACTION" in
  --reset) echo "All latency rules removed." ;;
  --show)  ;;
  *)       echo "Latency injection complete." ;;
esac
