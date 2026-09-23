#!/usr/bin/env bash
#
# Project:   dfe-docker
# File:      redpanda/start.sh
# Purpose:   Start Redpanda with a --memory that fits its container on this host
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Bash because the broker image carries no Python.

set -euo pipefail

readonly MIB=1048576
# Below this the broker is refused rather than started short of memory.
readonly FLOOR_MIB=512
# The image's own entrypoint, which hands `redpanda start ...` to rpk.
readonly IMAGE_ENTRYPOINT=/entrypoint.sh

log() { echo "redpanda/start.sh: $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# REDPANDA_MEMORY in MiB: <n>G, <n>M or plain bytes, the forms seastar reads.
requested_mib() {
    local value="${1}"
    local number="${value%[GgMm]}"
    [[ "${number}" =~ ^[0-9]+$ ]] || die "REDPANDA_MEMORY=${value} is not <n>G, <n>M or a byte count"
    case "${value}" in
        *[Gg]) echo $(( number * 1024 )) ;;
        *[Mm]) echo "${number}" ;;
        *) echo $(( number / MIB )) ;;
    esac
}

# The container's memory limit in MiB, or nothing when it has none.
limit_mib() {
    local file raw
    for file in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes; do
        [[ -r "${file}" ]] || continue
        raw="$(cat "${file}")"
        if [[ "${raw}" =~ ^[0-9]+$ ]]; then
            echo $(( raw / MIB ))
        fi
        return 0
    done
}

# Seastar takes the host's vm.min_free_kbytes off the container limit before it
# checks --memory. Rounded up, so the fit never lands above what seastar sees.
reserve_mib() {
    local kib
    kib="$(cat /proc/sys/vm/min_free_kbytes)"
    echo $(( (kib + 1023) / 1024 ))
}

main() {
    local requested limit reserve fit memory
    requested="$(requested_mib "${REDPANDA_MEMORY:-1G}")"
    limit="$(limit_mib)"
    if [[ -z "${limit}" ]]; then
        log "no container memory limit -- --memory=${requested}M as REDPANDA_MEMORY sets"
        exec "${IMAGE_ENTRYPOINT}" "$@" --memory="${requested}M"
    fi

    reserve="$(reserve_mib)"
    fit=$(( limit - reserve ))
    if (( fit >= requested )); then
        memory="${requested}"
    elif (( fit >= FLOOR_MIB )); then
        memory="${fit}"
        log "WARN: --memory=${memory}M, not the ${requested}M REDPANDA_MEMORY asks for:" \
            "the ${limit}M container limit less this host's ${reserve}M kernel reserve" \
            "(vm.min_free_kbytes) leaves ${fit}M. DFE_BROKER_MEMORY=$(( requested + reserve ))M" \
            "or more restores the full amount"
    else
        die "the ${limit}M container limit less this host's ${reserve}M kernel reserve" \
            "(vm.min_free_kbytes) leaves ${fit}M, under the ${FLOOR_MIB}M floor. Set" \
            "DFE_BROKER_MEMORY to at least $(( FLOOR_MIB + reserve ))M, or" \
            "$(( requested + reserve ))M for the full ${requested}M"
    fi

    log "--memory=${memory}M (container limit ${limit}M, host kernel reserve ${reserve}M)"
    exec "${IMAGE_ENTRYPOINT}" "$@" --memory="${memory}M"
}

main "$@"
