#!/usr/bin/env bash
#
# scripts/simulate_partition.sh
#
# Simulates a network partition for a designated container by disconnecting or
# reconnecting it from the Diomede Docker bridge network.
#
# Usage:
#   ./scripts/simulate_partition.sh isolate <container_name>
#   ./scripts/simulate_partition.sh heal <container_name>
#   ./scripts/simulate_partition.sh status <container_name>
#

set -euo pipefail

NETWORK="${DOCKER_NETWORK:-diomede-net}"
ACTION="${1:-help}"
CONTAINER="${2:-orthanc-us}"

case "${ACTION}" in
    isolate)
        echo "[*] Simulating Network Partition: Disconnecting ${CONTAINER} from ${NETWORK}..."
        if docker network inspect "${NETWORK}" | grep -q "\"Name\": \"${CONTAINER}\""; then
            docker network disconnect "${NETWORK}" "${CONTAINER}"
            echo "[✓] Container ${CONTAINER} successfully partitioned from ${NETWORK}."
        else
            echo "[!] Container ${CONTAINER} is not currently connected to ${NETWORK}."
        fi
        ;;

    heal)
        echo "[*] Healing Network Partition: Reconnecting ${CONTAINER} to ${NETWORK}..."
        if docker network inspect "${NETWORK}" | grep -q "\"Name\": \"${CONTAINER}\""; then
            echo "[!] Container ${CONTAINER} is already connected to ${NETWORK}."
        else
            docker network connect "${NETWORK}" "${CONTAINER}"
            echo "[✓] Container ${CONTAINER} successfully reconnected to ${NETWORK}."
        fi
        ;;

    status)
        echo "[*] Network Status for ${CONTAINER} on ${NETWORK}:"
        if docker network inspect "${NETWORK}" | grep -q "\"Name\": \"${CONTAINER}\""; then
            echo "  Status: CONNECTED (Healthy)"
        else
            echo "  Status: DISCONNECTED (Partitioned)"
        fi
        ;;

    *)
        echo "Usage: $0 {isolate|heal|status} [container_name]"
        echo "Example: $0 isolate orthanc-us"
        exit 1
        ;;
esac
