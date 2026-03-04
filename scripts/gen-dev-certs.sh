#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/gen-dev-certs.sh
# Purpose:   Generate self-signed dev certs for dfe-receiver gRPC TLS (:6000)
# Language:  Bash
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${SCRIPT_DIR}/../certs/dev"

mkdir -p "${CERT_DIR}"

openssl req \
    -newkey ec \
    -pkeyopt ec_paramgen_curve:P-384 \
    -nodes \
    -x509 \
    -keyout "${CERT_DIR}/receiver.key" \
    -out "${CERT_DIR}/receiver.crt" \
    -days 365 \
    -subj "/CN=dfe-receiver/O=DFE Dev/C=AU" \
    -addext "subjectAltName=DNS:dfe-receiver,DNS:localhost,IP:127.0.0.1"

chmod 600 "${CERT_DIR}/receiver.key"

echo "Certs written to ${CERT_DIR}/"
echo "  receiver.crt  — certificate (mount read-only)"
echo "  receiver.key  — private key (chmod 600)"
echo ""
echo "To enable TLS on gRPC inbound (:6000), uncomment the cert volume mounts"
echo "in docker-compose.override.yml and set grpc.tls.enabled: true in the config."
