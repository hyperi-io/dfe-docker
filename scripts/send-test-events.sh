#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/send-test-events.sh
# Purpose:   Send test events to dfe-receiver and verify they land in ClickHouse
# Language:  Bash
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

set -euo pipefail

RECEIVER_URL="${RECEIVER_URL:-http://localhost:8080}"
INGEST_PATH="/ingest"
URL="${RECEIVER_URL}${INGEST_PATH}"

echo "=== DFE Test Event Sender ==="
echo "Target: ${URL}"
echo ""

# Auth event — should route to auth_events table
echo "Sending auth event..."
curl -s -w "  HTTP %{http_code}\n" -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'",
    "event_category": "auth",
    "event_type": "login",
    "user_id": "user-001",
    "username": "kaz@hyperi.io",
    "ip_address": "10.66.0.100",
    "user_agent": "curl/test",
    "success": 1,
    "metadata": "{\"source\": \"test-script\"}"
  }'

# API event — should route to api_events table
echo "Sending API event..."
curl -s -w "  HTTP %{http_code}\n" -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'",
    "event_category": "api",
    "endpoint": "/api/v1/events",
    "method": "POST",
    "status_code": 200,
    "response_time_ms": 42,
    "request_id": "req-test-001",
    "user_id": "user-001",
    "ip_address": "10.66.0.100",
    "metadata": "{\"source\": \"test-script\"}"
  }'

# Generic event (no category) — should route to default table
echo "Sending generic event (unrouted)..."
curl -s -w "  HTTP %{http_code}\n" -X POST "${URL}" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "'"$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"'",
    "message": "Generic test event with no category",
    "source": "test-script",
    "metadata": "{\"note\": \"should land in default table\"}"
  }'

echo ""
echo "=== Events sent ==="
echo ""
echo "Waiting for loader to flush to ClickHouse..."
sleep 5

# Run verification if script exists
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "${SCRIPT_DIR}/verify-clickhouse.sh" ]]; then
  echo ""
  exec "${SCRIPT_DIR}/verify-clickhouse.sh"
else
  echo "Run scripts/verify-clickhouse.sh to check ClickHouse."
fi
