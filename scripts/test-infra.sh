#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/test-infra.sh
# Purpose:   Smoke tests for Kafka + ClickHouse infrastructure containers
# Language:  Bash
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Usage:
#   ./scripts/test-infra.sh          # Test against running infra profile
#   KAFKA_HOST=myhost ./scripts/...  # Override defaults

set -uo pipefail

KAFKA_BOOTSTRAP="${KAFKA_BOOTSTRAP:-localhost:19092}"
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
CH_URL="http://${CH_HOST}:${CH_PORT}"
TEST_TOPIC="dfe-infra-test"

passed=0
failed=0

pass() {
  echo "  PASS: $1"
  passed=$((passed + 1))
}

fail() {
  echo "  FAIL: $1"
  failed=$((failed + 1))
}

echo "=== DFE Infrastructure Smoke Tests ==="
echo "Kafka:      ${KAFKA_BOOTSTRAP}"
echo "ClickHouse: ${CH_URL}"
echo ""

# -------------------------------------------------------------------------
# Kafka tests
# -------------------------------------------------------------------------
echo "--- Kafka ---"

# Test 1: Kafka broker reachable
KAFKA_HOST="${KAFKA_BOOTSTRAP%%:*}"
KAFKA_PORT="${KAFKA_BOOTSTRAP##*:}"
if timeout 5 bash -c "echo > /dev/tcp/${KAFKA_HOST}/${KAFKA_PORT}" 2>/dev/null; then
  pass "Kafka broker reachable at ${KAFKA_BOOTSTRAP}"
else
  fail "Kafka broker not reachable at ${KAFKA_BOOTSTRAP}"
fi

# Find kafka CLI tools (apache/kafka image puts them in /opt/kafka/bin/)
KAFKA_BIN=""
if docker exec dfe-kafka test -f /opt/kafka/bin/kafka-topics.sh 2>/dev/null; then
  KAFKA_BIN="/opt/kafka/bin"
fi

if [[ -n "${KAFKA_BIN}" ]]; then
  # Test 2: Create a test topic
  if docker exec dfe-kafka "${KAFKA_BIN}/kafka-topics.sh" \
    --bootstrap-server localhost:9092 \
    --create --topic "${TEST_TOPIC}" \
    --partitions 1 --replication-factor 1 \
    --if-not-exists 2>/dev/null; then
    pass "Topic '${TEST_TOPIC}' created"
  else
    fail "Could not create topic '${TEST_TOPIC}'"
  fi

  # Test 3: Produce a message
  if echo '{"test":"infra-smoke"}' | docker exec -i dfe-kafka \
    "${KAFKA_BIN}/kafka-console-producer.sh" \
    --bootstrap-server localhost:9092 \
    --topic "${TEST_TOPIC}" 2>/dev/null; then
    pass "Message produced to '${TEST_TOPIC}'"
  else
    fail "Could not produce message to '${TEST_TOPIC}'"
  fi

  # Test 4: Consume the message back
  consumed=$(docker exec dfe-kafka "${KAFKA_BIN}/kafka-console-consumer.sh" \
    --bootstrap-server localhost:9092 \
    --topic "${TEST_TOPIC}" \
    --from-beginning --max-messages 1 --timeout-ms 10000 2>/dev/null || true)
  if echo "${consumed}" | grep -q "infra-smoke"; then
    pass "Message consumed from '${TEST_TOPIC}'"
  else
    fail "Could not consume message from '${TEST_TOPIC}'"
  fi

  # Test 5: List topics (should include test topic)
  topics=$(docker exec dfe-kafka "${KAFKA_BIN}/kafka-topics.sh" \
    --bootstrap-server localhost:9092 --list 2>/dev/null || true)
  if echo "${topics}" | grep -q "${TEST_TOPIC}"; then
    pass "Topic '${TEST_TOPIC}' visible in topic list"
  else
    fail "Topic '${TEST_TOPIC}' not found in topic list"
  fi

  # Cleanup: delete test topic
  docker exec dfe-kafka "${KAFKA_BIN}/kafka-topics.sh" \
    --bootstrap-server localhost:9092 \
    --delete --topic "${TEST_TOPIC}" 2>/dev/null || true
else
  fail "Kafka CLI tools not found in container (expected /opt/kafka/bin/)"
fi

echo ""

# -------------------------------------------------------------------------
# ClickHouse tests
# -------------------------------------------------------------------------
echo "--- ClickHouse ---"

ch_query() {
  curl -sf "${CH_URL}" --data-binary "$1"
}

# Test 6: ClickHouse ping
if curl -sf "${CH_URL}/ping" > /dev/null 2>&1; then
  pass "ClickHouse reachable at ${CH_URL}"
else
  fail "ClickHouse not reachable at ${CH_URL}"
fi

# Test 7: ClickHouse can execute queries
result=$(ch_query "SELECT 1" || true)
if [[ "${result}" == "1" ]]; then
  pass "ClickHouse query execution works"
else
  fail "ClickHouse query execution failed"
fi

# Test 8: dfe database exists
db_exists=$(ch_query "SELECT count() FROM system.databases WHERE name = 'dfe'" || echo "0")
if [[ "${db_exists}" == "1" ]]; then
  pass "Database 'dfe' exists"
else
  fail "Database 'dfe' not found"
fi

# Test 9: Expected tables exist
expected_tables=("default" "auth_events" "api_events" "admin_events" "error_events" "dlq_events")
for table in "${expected_tables[@]}"; do
  table_exists=$(ch_query "SELECT count() FROM system.tables WHERE database = 'dfe' AND name = '${table}'" || echo "0")
  if [[ "${table_exists}" == "1" ]]; then
    pass "Table 'dfe.${table}' exists"
  else
    fail "Table 'dfe.${table}' not found"
  fi
done

# Test 10: Insert and query a test row
ch_query "INSERT INTO dfe.default (timestamp, metadata) VALUES (now64(3), '{\"test\":\"infra-smoke\"}')" || true
count=$(ch_query "SELECT count() FROM dfe.default WHERE metadata LIKE '%infra-smoke%'" || echo "0")
if [[ "${count}" -ge 1 ]] 2>/dev/null; then
  pass "Insert + query round-trip works (dfe.default)"
else
  fail "Insert + query round-trip failed (dfe.default)"
fi

# Cleanup test row
ch_query "ALTER TABLE dfe.default DELETE WHERE metadata LIKE '%infra-smoke%'" 2>/dev/null || true

echo ""

# -------------------------------------------------------------------------
# Summary
# -------------------------------------------------------------------------
total=$((passed + failed))
echo "=== Results: ${passed}/${total} passed, ${failed} failed ==="

if [[ ${failed} -gt 0 ]]; then
  exit 1
fi
