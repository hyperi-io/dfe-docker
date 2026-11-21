#!/usr/bin/env bash
#
# Project:   dfe-docker
# File:      redpanda/configure.sh
# Purpose:   Set the broker's cluster properties and refuse any Enterprise feature
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Bash because the broker image carries no Python.

set -euo pipefail

# Cluster properties live in the controller, not in `redpanda start` flags, so they are set on every start.
readonly ADMIN="${REDPANDA_ADMIN_HOSTS:-kafka-redpanda:9644}"
readonly FEATURES_RE='"enterprise_features_in_use":(\[[^]]*\])'
# The broker shares its disk with ClickHouse. compose sets both (docs/broker.md).
readonly MIN_FREE_BYTES="${REDPANDA_MIN_FREE_BYTES:?compose sets REDPANDA_MIN_FREE_BYTES}"
readonly RETENTION_BYTES="${REDPANDA_RETENTION_BYTES:?compose sets REDPANDA_RETENTION_BYTES}"

log() { echo "redpanda/configure.sh: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

rpk_admin() { rpk "$@" -X "admin.hosts=${ADMIN}"; }

# The text form drops the features line when the list is empty, so read the JSON.
features_in_use() {
    local json
    json="$(rpk_admin cluster license info --format json)"
    [[ "${json}" =~ ${FEATURES_RE} ]] || die "no enterprise_features_in_use in: ${json}"
    echo "${BASH_REMATCH[1]}"
}

# Redpanda turns both on by default and counts them as Enterprise features, which lapse with its trial.
turn_features_off() {
    rpk_admin cluster config set partition_autobalancing_mode node_add
    rpk_admin cluster config set core_balancing_continuous false
}

# Beta and untested against a paid licence: applies REDPANDA_LICENSE and requires no violation.
apply_licence() {
    local file json rc=0
    file="$(mktemp)"
    printf '%s' "${REDPANDA_LICENSE}" > "${file}"
    rpk_admin cluster license set --path "${file}" || rc=$?
    rm -f "${file}"
    if (( rc != 0 )); then
        turn_features_off
        die "rpk refused REDPANDA_LICENSE (exit ${rc}), so both trial Enterprise features are off"
    fi
    json="$(rpk_admin cluster license info --format json)"
    if [[ "${json}" != *'"license_violation":false'* ]]; then
        turn_features_off
        die "licence applied but Redpanda reports a violation, so both trial Enterprise features are off: ${json}"
    fi
    log "Enterprise licence applied (beta)"
}

main() {
    # Below the floor the broker refuses writes rather than fill the disk. The cap bounds one partition.
    rpk_admin cluster config set storage_min_free_bytes "${MIN_FREE_BYTES}"
    rpk_admin cluster config set retention_bytes "${RETENTION_BYTES}"
    if [[ -n "${REDPANDA_LICENSE:-}" ]]; then
        apply_licence
        return
    fi
    turn_features_off

    local in_use
    in_use="$(features_in_use)"
    [[ "${in_use}" == "[]" ]] || die "Enterprise features still in use: ${in_use}. Turn each off here."
    log "no Enterprise feature in use"
}

main "$@"
