#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/verify-clickhouse.sh
# Purpose:   Query ClickHouse to verify test events landed correctly
# Language:  Bash
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

set -euo pipefail

CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
CH_URL="http://${CH_HOST}:${CH_PORT}"

query() {
  curl -s "${CH_URL}" --data-binary "$1"
}

echo "=== ClickHouse Verification ==="
echo "Host: ${CH_URL}"
echo ""

# Check connectivity
if ! curl -sf "${CH_URL}/ping" > /dev/null 2>&1; then
  echo "ERROR: Cannot reach ClickHouse at ${CH_URL}"
  exit 1
fi

# List tables in dfe database
echo "--- Tables in dfe database ---"
query "SELECT name, engine FROM system.tables WHERE database = 'dfe' ORDER BY name FORMAT PrettyCompact"
echo ""

# Row counts per table
echo "--- Row counts ---"
for table in common auth_events api_events admin_events error_events dlq_events; do
  count=$(query "SELECT count() FROM dfe.${table}" 2>/dev/null || echo "N/A")
  printf "  %-20s %s\n" "dfe.${table}" "${count}"
done
echo ""

# Recent events from each table (last 5)
for table in common auth_events api_events admin_events; do
  count=$(query "SELECT count() FROM dfe.${table}" 2>/dev/null || echo "0")
  if [[ "${count}" -gt 0 ]] 2>/dev/null; then
    echo "--- Recent events: dfe.${table} (last 5) ---"
    query "SELECT * FROM dfe.${table} ORDER BY timestamp DESC LIMIT 5 FORMAT PrettyCompact"
    echo ""
  fi
done

echo "=== Verification complete ==="
