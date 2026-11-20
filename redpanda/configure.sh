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

main() {
    # Redpanda turns both on by default and counts them as Enterprise features, which lapse with its trial.
    rpk_admin cluster config set partition_autobalancing_mode node_add
    rpk_admin cluster config set core_balancing_continuous false

    local in_use
    in_use="$(features_in_use)"
    [[ "${in_use}" == "[]" ]] || die "Enterprise features still in use: ${in_use}. Turn each off here."
    log "no Enterprise feature in use"
}

main "$@"
