#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/test-vector.sh
# Purpose:   Feed test events into dfe-receiver via HTTP and gRPC (Vector protocol)
# Language:  Bash
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Usage:
#   ./scripts/test-vector.sh http    # HTTP ingest to :8080
#   ./scripts/test-vector.sh grpc    # gRPC ingest to :6000 (Vector protocol)
#   ./scripts/test-vector.sh both    # Both simultaneously (default)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-both}"
RECEIVER_HOST="${RECEIVER_HOST:-localhost}"
RECEIVER_HTTP_PORT="${RECEIVER_HTTP_PORT:-8080}"
RECEIVER_GRPC_PORT="${RECEIVER_GRPC_PORT:-6000}"

resolve_vector() {
    local cached="${SCRIPT_DIR}/../.tmp/vector/bin/vector"
    local fetch="${SCRIPT_DIR}/fetch-vector.sh"
    if [[ -x "${cached}" ]]; then
        echo "${cached}"
        return
    fi
    if [[ -f "${fetch}" ]]; then
        local path
        path="$(bash "${fetch}" 2>/dev/null | tail -1)"
        if [[ -x "${path}" ]]; then
            echo "${path}"
            return
        fi
    fi
    if command -v vector &>/dev/null; then
        echo "vector"
        return
    fi
    echo "vector not found — run: ./scripts/fetch-vector.sh" >&2
    exit 1
}

VECTOR="$(resolve_vector)"
TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_WORK}"' EXIT

run_http() {
    local cfg="${TMPDIR_WORK}/vector-http.yaml"
    cat > "${cfg}" << EOF
sources:
  generate:
    type: demo_logs
    format: json
    interval: 0.1
    count: 20

sinks:
  receiver_http:
    type: http
    inputs: [generate]
    uri: "http://${RECEIVER_HOST}:${RECEIVER_HTTP_PORT}/events"
    method: post
    encoding:
      codec: json
    batch:
      max_events: 10
      timeout_secs: 1
EOF
    echo "Sending 20 events via HTTP -> ${RECEIVER_HOST}:${RECEIVER_HTTP_PORT}/events"
    "${VECTOR}" --config "${cfg}" --quiet
    echo "HTTP test complete"
}

run_grpc() {
    local cfg="${TMPDIR_WORK}/vector-grpc.yaml"
    cat > "${cfg}" << EOF
sources:
  generate:
    type: demo_logs
    format: json
    interval: 0.1
    count: 20

sinks:
  receiver_grpc:
    type: grpc
    inputs: [generate]
    endpoint: "${RECEIVER_HOST}:${RECEIVER_GRPC_PORT}"
    encoding:
      codec: native_json
EOF
    echo "Sending 20 events via gRPC (Vector protocol) -> ${RECEIVER_HOST}:${RECEIVER_GRPC_PORT}"
    "${VECTOR}" --config "${cfg}" --quiet
    echo "gRPC test complete"
}

case "${MODE}" in
    http)
        run_http
        ;;
    grpc)
        run_grpc
        ;;
    both)
        run_http
        run_grpc
        ;;
    *)
        echo "Usage: $0 [http|grpc|both]" >&2
        exit 1
        ;;
esac

echo ""
echo "Check results: make verify"
