#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/test-e2e.sh
# Purpose:   Config-driven e2e test runner for the DFE Docker stack
# Language:  Bash
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Usage:
#   ./scripts/test-e2e.sh                          # Run all tests from config
#   ./scripts/test-e2e.sh kafka-full               # Run specific test by name
#   ./scripts/test-e2e.sh kafka-full grpc-full     # Run multiple named tests
#   TEST_CONFIG=tests/e2e/e2e-tests.yaml ./scripts/test-e2e.sh  # Custom config
#   LOG_LEVEL=debug ./scripts/test-e2e.sh          # Verbose service logs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly PROJECT_DIR
: "${TEST_CONFIG:=${PROJECT_DIR}/tests/e2e/e2e-tests.yaml}"
: "${RECEIVER_HEALTH_URL:=http://localhost:${DFE_RECEIVER_PROMETHEUS_PORT:-9090}/metrics}"
: "${RECEIVER_INGEST_URL:=http://localhost:${DFE_RECEIVER_HTTP_PORT:-8080}}"
: "${CLICKHOUSE_URL:=http://localhost:${CLICKHOUSE_HTTP_PORT:-8123}}"
: "${KAFKA_BROKER:=localhost:${KAFKA_EXTERNAL_PORT:-19092}}"
: "${LOG_LEVEL:=info}"
LOG_LEVEL="$(echo "${LOG_LEVEL}" | tr '[:upper:]' '[:lower:]')"

passed=0
failed=0
skipped=0
test_run_id="e2e-$(date -u +%Y%m%d%H%M%S)"

log() {
    echo "${1}" >&2
}

log_debug() {
    if [[ "${LOG_LEVEL}" == "debug" ]]; then
        echo "${1}" >&2
    fi
}

die() {
    log "ERROR: ${1}"
    exit "${2:-1}"
}

require_command() {
    command -v "${1}" > /dev/null 2>&1 || die "${1} not found — install it first"
}

cleanup() {
    rm -f "${PROJECT_DIR}/.tmp/sampled-events.jsonl"
    rm -f "${PROJECT_DIR}/.tmp/docker-compose.e2e.yaml"
}

if [[ -t 1 ]]; then
    _green=$'\033[32m' _red=$'\033[31m' _yellow=$'\033[33m' _reset=$'\033[0m'
else
    _green="" _red="" _yellow="" _reset=""
fi

pass() {
    echo "${_green}PASS${_reset}: ${1}"
    passed=$((passed + 1))
}

fail() {
    echo "${_red}FAIL${_reset}: ${1}"
    failed=$((failed + 1))
}

skip_test() {
    echo "${_yellow}SKIP${_reset}: ${1}"
    skipped=$((skipped + 1))
    echo ""
}

ch_query() {
    curl -sf "${CLICKHOUSE_URL}" --data-binary "${1}" 2>/dev/null
}

# Read a test field from the YAML config, falling back to defaults section.
# Usage: cfg_get <test_index> <field>
cfg_get() {
    local idx="${1}"
    local field="${2}"
    local val
    val=$(yq -r ".tests[${idx}].${field} | select(. != null)" "${TEST_CONFIG}")
    if [[ -z "${val}" ]]; then
        val=$(yq -r ".defaults.${field} | select(. != null)" "${TEST_CONFIG}")
    fi
    [[ -n "${val}" ]] && echo "${val}" || echo "null"
}

# Read a list field as newline-separated values.
# Usage: cfg_list <test_index> <field>
cfg_list() {
    local idx="${1}"
    local field="${2}"
    yq -r ".tests[${idx}].${field} // [] | .[]" "${TEST_CONFIG}"
}

wait_for_service() {
    local name="${1}"
    local url="${2}"
    local max_attempts="${3:-30}"
    local attempt=1

    log "Waiting for '${name}' at '${url}'..."
    while [[ ${attempt} -le ${max_attempts} ]]; do
        if curl -sf "${url}" > /dev/null 2>&1; then
            log_debug "${name} ready after ${attempt} attempts"
            return 0
        fi
        sleep 2
        attempt=$((attempt + 1))
    done
    log "ERROR: ${name} not ready after ${max_attempts} attempts"
    return 1
}

# Map a compose profile to its receiver and loader service names.
services_for_profile() {
    echo "dfe-receiver dfe-loader"
}

# Generate a compose override that mounts the test-specified configs.
generate_compose_override() {
    local profile="${1}"
    local receiver_config="${2}"
    local loader_config="${3}"
    local override_file="${PROJECT_DIR}/.tmp/docker-compose.e2e.yaml"

    mkdir -p "${PROJECT_DIR}/.tmp"

    local services
    services=$(services_for_profile "${profile}")
    local receiver_svc="${services%% *}"
    local loader_svc="${services##* }"

    # Export vars for yq strenv() — yq uses env vars, not --arg like jq
    export YQ_RSVC="${receiver_svc}"
    export YQ_LSVC="${loader_svc}"
    export YQ_RCFG="./${receiver_config}:/etc/dfe-receiver/config.yaml:ro"
    export YQ_LCFG="./${loader_config}:/etc/dfe/loader.yaml:ro"

    # shellcheck disable=SC2016 # yq expressions, not shell variables
    yq -n \
        '.services[strenv(YQ_RSVC)].volumes = [strenv(YQ_RCFG)] | .services[strenv(YQ_LSVC)].volumes = [strenv(YQ_LCFG)]' \
        > "${override_file}"

    log_debug "Generated compose override: ${override_file}"
    echo "${override_file}"
}

stack_up() {
    local mode="${1}"
    local profile="${2}"
    local receiver_config="${3}"
    local loader_config="${4}"

    log "Starting stack..."
    log_debug "  - Mode: ${mode}"
    log_debug "  - Profile: ${profile}"
    log_debug "  - Receiver Config: ${receiver_config}"
    log_debug "  - Loader Config: ${loader_config}"

    cd "${PROJECT_DIR}"

    local profiles=(--profile "${profile}")

    local override_file
    override_file=$(generate_compose_override "${profile}" "${receiver_config}" "${loader_config}")

    local compose_files=(-f docker-compose.yml)
    case "${mode}" in
        dev)
            # Include the dev override for local builds if it exists
            if [[ -f "docker-compose.override.yml" ]]; then
                compose_files+=(-f docker-compose.override.yml)
            fi
            compose_files+=(-f "${override_file}")
            if [[ "${LOG_LEVEL}" == "debug" ]]; then
                docker compose "${compose_files[@]}" "${profiles[@]}" up --build -d
            else
                local compose_log="${PROJECT_DIR}/.tmp/compose-up.log"
                if ! docker compose "${compose_files[@]}" "${profiles[@]}" up --build -d --quiet-pull > "${compose_log}" 2>&1; then
                    log "ERROR: docker compose up failed:"
                    cat "${compose_log}" >&2
                    return 1
                fi
            fi
            ;;
        ci)
            compose_files+=(-f "${override_file}")
            if [[ "${LOG_LEVEL}" == "debug" ]]; then
                docker compose "${compose_files[@]}" "${profiles[@]}" up -d
            else
                local compose_log="${PROJECT_DIR}/.tmp/compose-up.log"
                if ! docker compose "${compose_files[@]}" "${profiles[@]}" up -d --quiet-pull > "${compose_log}" 2>&1; then
                    log "ERROR: docker compose up failed:"
                    cat "${compose_log}" >&2
                    return 1
                fi
            fi
            ;;
        *)
            die "Unknown mode: ${mode} (expected 'dev' or 'ci')"
            ;;
    esac
}

compose_down_all() {
    cd "${PROJECT_DIR}"
    local profile_flags=()
    local p
    while IFS= read -r p; do
        [[ -n "${p}" ]] && profile_flags+=(--profile "${p}")
    done < <(yq -r '[.services[].profiles[]?] | unique | .[]' docker-compose.yml 2>/dev/null)
    docker compose "${profile_flags[@]}" down --remove-orphans > /dev/null 2>&1 || true
}

stack_down() {
    log_debug "Stopping stack..."
    compose_down_all
}

dump_logs() {
    if [[ "${LOG_LEVEL}" == "debug" ]]; then
        log "--- Container logs ---"
        cd "${PROJECT_DIR}"
        docker compose logs --tail=50 2>/dev/null || true
        log "--- End logs ---"
    fi
}

wait_for_stack() {
    local profile="${1}"

    wait_for_service "ClickHouse" "${CLICKHOUSE_URL}/ping" 30 || return 1
    wait_for_service "dfe-receiver" "${RECEIVER_HEALTH_URL}" 30 || return 1

    log_debug "Waiting for loader to connect to ClickHouse..."
    sleep 3

    if [[ "${profile}" == *kafka* ]]; then
        log_debug "Kafka profile: extra settle time for consumer group"
        sleep 5
    fi

    return 0
}

# Truncate dfe.default so each test starts clean.
clean_table() {
    log_debug "Cleaning dfe.default..."
    ch_query "TRUNCATE TABLE IF EXISTS dfe.default" || true
    sleep 1
}

# Send sampled events from the data file. Sets total_sent for verify_table.
send_events() {
    local test_name="${1}"
    local marker="${2}"
    local data_file="${3}"
    local sample_size="${4}"
    local url="${RECEIVER_INGEST_URL}/ingest"

    local resolved_data
    resolved_data="${PROJECT_DIR}/${data_file}"
    [[ -f "${resolved_data}" ]] || die "Data file not found: ${resolved_data}"

    local pool_size
    pool_size=$(wc -l < "${resolved_data}")

    local sampled_file
    sampled_file="${PROJECT_DIR}/.tmp/sampled-events.jsonl"
    mkdir -p "${PROJECT_DIR}/.tmp"

    if [[ -z "${sample_size}" ]]; then
        log "Sending all ${pool_size} events from data file..."
        cp "${resolved_data}" "${sampled_file}"
    else
        local count="${sample_size}"
        if [[ ${count} -gt ${pool_size} ]]; then
            count=${pool_size}
        fi
        log "Sampling ${count} events from pool of ${pool_size}..."
        shuf -n "${count}" "${resolved_data}" > "${sampled_file}"
    fi
    log_debug "  - Marker: ${marker}"

    total_sent=0
    local send_errors=0

    while IFS= read -r line; do
        local enriched
        enriched=$(jq -c \
            --arg marker "${marker}" \
            '. + {"metadata": ("{\"marker\": \"" + $marker + "\"}")}' \
            <<< "${line}")

        local http_code
        http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${url}" \
            -H "Content-Type: application/json" \
            -d "${enriched}")

        if [[ "${http_code}" -lt 200 ]] || [[ "${http_code}" -ge 300 ]]; then
            log_debug "Non-2xx response (${http_code}) for: ${enriched}"
            send_errors=$((send_errors + 1))
        fi

        total_sent=$((total_sent + 1))
    done < "${sampled_file}"

    log "Sent ${total_sent} events (errors = ${send_errors})"

    if [[ ${send_errors} -eq 0 ]]; then
        pass "[${test_name}] All ${total_sent} ingest requests returned 2xx"
    else
        fail "[${test_name}] ${send_errors}/${total_sent} ingest requests returned non-2xx"
    fi
}

# Verify events landed in dfe.default.
verify_table() {
    local test_name="${1}"
    local marker="${2}"
    local expected="${total_sent}"

    log "Verifying events in ClickHouse 'dfe.default' (marker: '${marker}')..."

    local actual="0"
    local attempt=1
    while [[ ${attempt} -le 3 ]]; do
        actual=$(ch_query "SELECT count() FROM dfe.default WHERE metadata LIKE '%${marker}%'" || echo "0")
        actual=$(echo "${actual}" | tr -d '[:space:]')

        if [[ "${actual}" -ge "${expected}" ]] 2>/dev/null; then
            log "    Attempt ${attempt}/3: got ${actual}/${expected}"
            break
        fi

        if [[ ${attempt} -lt 3 ]]; then
            log "    Attempt ${attempt}/3: got ${actual}/${expected}, retrying in 10s..."
            sleep 10
        else
            log "    Attempt ${attempt}/3: got ${actual}/${expected}"
        fi
        attempt=$((attempt + 1))
    done

    if [[ "${actual}" -ge "${expected}" ]] 2>/dev/null; then
        pass "[${test_name}] 'dfe.default': ${actual}/${expected}"
    else
        fail "[${test_name}] 'dfe.default': expected ${expected}, got ${actual}"
    fi
}

# Verify expected Kafka topics exist (kafka profiles only).
verify_topics() {
    local test_name="${1}"
    local expected_topics="${2}"

    log "Verifying Kafka topics..."

    local existing_topics
    existing_topics=$(docker compose exec -T kafka \
        /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list 2>/dev/null || echo "")

    local topic
    while IFS= read -r topic; do
        [[ -z "${topic}" ]] && continue
        if echo "${existing_topics}" | grep -qxF "${topic}"; then
            pass "[${test_name}] Kafka topic exists: '${topic}'"
        else
            fail "[${test_name}] Kafka topic missing: '${topic}'"
        fi
    done <<< "${expected_topics}"
}

# Run a single test definition from the YAML config.
run_test() {
    local idx="${1}"

    local name
    name=$(cfg_get "${idx}" "name")
    local mode
    mode=$(cfg_get "${idx}" "mode")
    local profile
    profile=$(cfg_get "${idx}" "profile")
    local sample_size
    sample_size=$(cfg_get "${idx}" "sample_size")
    [[ "${sample_size}" != "null" ]] || sample_size=""
    local data_file
    data_file=$(cfg_get "${idx}" "data_file")
    local receiver_config
    receiver_config=$(cfg_get "${idx}" "receiver_config")
    local loader_config
    loader_config=$(cfg_get "${idx}" "loader_config")
    local teardown
    teardown=$(cfg_get "${idx}" "teardown")
    [[ "${teardown}" != "null" ]] || teardown="true"
    local marker="${test_run_id}-${name}"

    local expected_topics
    expected_topics=$(cfg_list "${idx}" "expected_topics")

    [[ "${receiver_config}" != "null" ]] || die "Test '${name}' missing receiver_config"
    [[ "${loader_config}" != "null" ]] || die "Test '${name}' missing loader_config"
    [[ -f "${PROJECT_DIR}/${receiver_config}" ]] || die "receiver_config not found: ${receiver_config}"
    [[ -f "${PROJECT_DIR}/${loader_config}" ]] || die "loader_config not found: ${loader_config}"

    echo ""
    echo "------------------------------------------------------------"
    echo "E2E Test: ${name}"
    echo "  - Mode: ${mode}"
    echo "  - Profile: ${profile}"
    echo "  - Data File: ${data_file}"
    [[ -z "${sample_size}" ]] || echo "  - Sample Size: ${sample_size}"
    echo "  - Receiver Config: ${receiver_config}"
    echo "  - Loader Config: ${loader_config}"
    [[ "${teardown}" == "true" ]] || echo "  - Teardown: false"
    echo "------------------------------------------------------------"

    if ! stack_up "${mode}" "${profile}" "${receiver_config}" "${loader_config}"; then
        fail "[${name}] Stack failed to start"
        [[ "${teardown}" != "true" ]] || stack_down
        return
    fi

    if ! wait_for_stack "${profile}"; then
        fail "[${name}] Stack did not become healthy"
        dump_logs
        [[ "${teardown}" != "true" ]] || stack_down
        return
    fi

    clean_table

    send_events "${name}" "${marker}" "${data_file}" "${sample_size}"
    echo ""

    verify_table "${name}" "${marker}"

    if [[ "${profile}" == *kafka* ]] && [[ -n "${expected_topics}" ]]; then
        echo ""
        verify_topics "${name}" "${expected_topics}"
    fi

    dump_logs
    if [[ "${teardown}" == "true" ]]; then
        stack_down
    else
        log "Teardown disabled — stack left running for '${name}'"
    fi
    echo "------------------------------------------------------------"
}

main() {
    echo "============= DFE Docker End-to-End Test Suite ============="
    echo ""
    echo "Config:     ${TEST_CONFIG}"
    echo "Run ID:     ${test_run_id}"
    echo "Log level:  ${LOG_LEVEL}"
    echo "Start Time: $(date +%Y-%m-%dT%H:%M:%S%:z)"
    echo ""
    echo "============================================================"

    require_command curl
    require_command jq
    require_command yq
    require_command shuf

    [[ -f "${TEST_CONFIG}" ]] || die "Test config not found: ${TEST_CONFIG}"

    trap cleanup EXIT

    # Silently tear down any existing stack before starting
    compose_down_all

    local test_count
    test_count=$(yq -r '.tests | length' "${TEST_CONFIG}")

    if [[ ${#} -gt 0 ]]; then
        # Run only named tests from CLI args
        local arg
        for arg in "${@}"; do
            local found=false
            local i=0
            while [[ ${i} -lt ${test_count} ]]; do
                local name
                name=$(yq -r ".tests[${i}].name" "${TEST_CONFIG}")
                if [[ "${name}" == "${arg}" ]]; then
                    run_test "${i}"
                    found=true
                    break
                fi
                i=$((i + 1))
            done
            if [[ "${found}" == "false" ]]; then
                skip_test "Unknown test name: ${arg}"
            fi
        done
    else
        # Run all tests
        local i=0
        while [[ ${i} -lt ${test_count} ]]; do
            run_test "${i}"
            i=$((i + 1))
        done
    fi

    echo ""
    echo "========================== Results ========================="
    echo ""
    echo "Passed:     ${passed}"
    echo "Failed:     ${failed}"
    echo "Skipped:    ${skipped}"
    echo "Total:      $((passed + failed + skipped))"
    echo "End Time:   $(date +%Y-%m-%dT%H:%M:%S%:z)"
    echo ""
    echo "============================================================"

    if [[ ${failed} -gt 0 ]]; then
        exit 1
    fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
