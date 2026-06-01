#!/usr/bin/env bash
# scripts/gen_certs.sh  –  Generate TLS certificates for Diomede
#
# Creates a 30-year self-signed root CA and use it to sign certificate requests of
# 2-year server certificates for all four 4 regional Orthanc nodes, the Orchestrator, and
# the edge agent Orthanc using the same hostnames as the Docker Compose service names, as
# well as a client certificate for the simulator.
#
# Usage:
#   bash scripts/gen_certs.sh
#
# Output layout (all private keys are mode 600):
#   certs/
#   ├── ca.key                       # CA private key — NEVER commit or share
#   ├── ca.pem                       # CA public cert — safe to distribute
#   ├── orchestrator/
#   │   ├── server.key
#   │   └── server.crt
#   ├── orthanc-us/combined.pem      # cert + key concatenated (Orthanc format)
#   ├── orthanc-eu/combined.pem
#   ├── orthanc-asia/combined.pem
#   └── orthanc-af/combined.pem
#   └── diomede-client/
#       ├── client.crt               # clientAuth certificate — used by the simulator
#       └── client.key
# The certs/ directory is gitignored. Re-run this script on every fresh clone.

set -euo pipefail

DAYS_CA=10950      # 30 years for the CA
DAYS_SERVER=730   # 2 years for server certs (renewable from the same CA)
OUT="certs"
KEY_BITS=4096

# --------------------------------------------------------------------------- #
# Helper: sign one server cert
# Each cert's SAN includes the Docker service name and localhost so it works
# both inside Docker (service name resolution) and for local curl testing.
# --------------------------------------------------------------------------- #
sign_cert() {
  local name="$1"   # Docker service name, e.g. orthanc-us
  local dir="$OUT/$name"
  mkdir -p "$dir"

  local san="DNS:${name},DNS:localhost,IP:127.0.0.1"

  openssl genrsa -out "$dir/server.key" "$KEY_BITS" 2>/dev/null
  chmod 600 "$dir/server.key"

  openssl req -new \
    -key "$dir/server.key" \
    -subj "/CN=${name}/O=Diomede" \
    -out "$dir/server.csr" 2>/dev/null

  # Use a temp file instead of process substitution so set -euo pipefail works correctly
  local ext
  ext=$(mktemp)
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\n' "$san" > "$ext"

  openssl x509 -req \
    -in "$dir/server.csr" \
    -CA "$OUT/ca.pem" -CAkey "$OUT/ca.key" -CAcreateserial \
    -days "$DAYS_SERVER" -sha256 \
    -extfile "$ext" \
    -out "$dir/server.crt" 2>/dev/null

  rm -f "$ext" "$dir/server.csr"

  # Orthanc requires cert + key concatenated into a single PEM file
  cat "$dir/server.crt" "$dir/server.key" > "$dir/combined.pem"
  chmod 600 "$dir/combined.pem"
  cp "$OUT/ca.pem" "$dir/ca.pem"

  echo "  $name  (SAN: $san)"
}

# --------------------------------------------------------------------------- #
# Helper: sign one client cert (extendedKeyUsage=clientAuth)
# Used by DICOM clients (simulator scripts) to authenticate
# themselves to nodes that enforce DicomTlsRemoteCertificateRequired: true.
# --------------------------------------------------------------------------- #
sign_client_cert() {
  local name="$1"   # logical name, e.g. diomede-client
  local dir="$OUT/$name"
  mkdir -p "$dir"

  openssl genrsa -out "$dir/client.key" "$KEY_BITS" 2>/dev/null
  chmod 600 "$dir/client.key"

  openssl req -new \
    -key "$dir/client.key" \
    -subj "/CN=${name}/O=Diomede" \
    -out "$dir/client.csr" 2>/dev/null

  local ext
  ext=$(mktemp)
  printf 'extendedKeyUsage=clientAuth\n' > "$ext"

  openssl x509 -req \
    -in "$dir/client.csr" \
    -CA "$OUT/ca.pem" -CAkey "$OUT/ca.key" -CAcreateserial \
    -days "$DAYS_SERVER" -sha256 \
    -extfile "$ext" \
    -out "$dir/client.crt" 2>/dev/null

  rm -f "$ext" "$dir/client.csr"
  echo "  $name  (clientAuth)"
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
echo "Generating TLS certificates for Diomede..."
echo ""

mkdir -p "$OUT"
# Remove existing certificates directory to ensure a clean regeneration
rm -rf "$OUT"/*
# Root CA (30 years) ─────────────────────────────────────────────────────────
echo "[1/8] Root CA (${DAYS_CA} days)"
openssl genrsa -out "$OUT/ca.key" "$KEY_BITS" 2>/dev/null
chmod 600 "$OUT/ca.key"
# -addext avoids process substitution so set -euo pipefail works correctly
openssl req -x509 -new -nodes \
  -key "$OUT/ca.key" \
  -sha256 -days "$DAYS_CA" \
  -subj "/CN=Diomede-CA/O=Diomede" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -out "$OUT/ca.pem" 2>/dev/null
echo "  CA → $OUT/ca.pem"
echo ""

# Server certs ────────────────────────────────────────────────────────────────
sign_cert "orchestrator" && echo "[2/8] orchestrator  done"
sign_cert "orthanc-us"   && echo "[3/8] orthanc-us    done"
sign_cert "orthanc-eu"   && echo "[4/8] orthanc-eu    done"
sign_cert "orthanc-asia" && echo "[5/8] orthanc-asia  done"
sign_cert "orthanc-af"   && echo "[6/8] orthanc-af    done"
sign_cert "edge-agent"   && echo "[7/8] edge-agent    done"

# Client cert (clientAuth EKU) — used by simulator scripts to prove
# identity during DICOM TLS mutual authentication.
sign_client_cert "diomede-client" && echo "[8/8] diomede-client  done"

# Summary ─────────────────────────────────────────────────────────────────────
echo ""
echo "Done.  Certificate tree:"
find "$OUT" -type f | sort | sed 's/^/  /'
echo ""
echo "Keep certs/ca.key and all server.key / combined.pem files secret."
echo "Distribute certs/ca.pem to any client that needs to verify servers."
