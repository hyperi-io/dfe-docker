#!/usr/bin/env bash
# Project:   dfe-docker
# File:      scripts/sync-component-defaults.sh
# Purpose:   Generate config/<comp>/default/defaults_v<version>.yaml from a DFE
#            component image's own deployment-contract .default_config
# Language:  Bash
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Usage:
#   ./scripts/sync-component-defaults.sh render <deployment-contract.json> <image_ref>
#       Dockerless core: read .default_config from an already-extracted
#       deployment-contract.json and emit deterministic, sorted defaults YAML
#       (with an AUTO-GENERATED header) on stdout.
#
#   ./scripts/sync-component-defaults.sh run <name> <image> <repo> <binary> <repo_config> <stage>
#       Docker-driven path: detect the newest release, pull the image, run
#       generate-artefacts to obtain its deployment-contract.json, render it
#       into the staging tree, self-validate, and emit a status JSON.

set -euo pipefail

render() {
  local contract="$1" image_ref="$2"
  printf '# AUTO-GENERATED — do not edit by hand.\n'
  printf '# Source: %s (generate-artefacts -> deployment-contract.json .default_config)\n' "$image_ref"
  printf '# Workflow: .github/workflows/sync-component-defaults.yml\n'
  # -e: fail if .default_config is null/absent. sort_keys(..): deterministic order.
  yq -p=json -o=yaml -e '.default_config | sort_keys(..)' "$contract"
}

# Emit a single-line status JSON.
status() { yq -o=json -I=0 -n ".name=\"$1\" | .version=\"$2\" | .state=\"$3\" | .validation=\"$4\""; }

# Write status into the staging root (for the artifact) and echo it (for logs).
write_status() { local stage="$1"; shift; status "$@" | tee "$stage/status-$1.json"; }

run() {
  local name="$1" image="$2" repo="$3" binary="$4" repo_config="$5" stage="$6"
  local ref version target latest stage_dir art_dir tmp validation

  version="$(gh release view --repo "$repo" --json tagName -q .tagName 2>/dev/null || true)"
  if [ -z "$version" ]; then write_status "$stage" "$name" "" "skip-no-release" "n/a"; return 0; fi

  ref="${image}:${version}"
  target="$repo_config/$name/default/defaults_${version}.yaml"
  latest="$repo_config/$name/default/defaults_latest.yaml"
  if [ -f "$target" ] && grep -q "$ref" "$latest" 2>/dev/null; then
    write_status "$stage" "$name" "$version" "skip-current" "n/a"; return 0
  fi

  art_dir="$(mktemp -d)"
  # mktemp -d is mode 0700 (host user only); the image runs as a non-root user,
  # so make the bind-mounted output dir writable by any container UID.
  chmod 0777 "$art_dir"
  docker pull -q "$ref" >&2
  docker run --rm -v "$art_dir:/out" --entrypoint "$binary" "$ref" \
    generate-artefacts --output-dir /out >&2

  stage_dir="$stage/config/$name/default"; mkdir -p "$stage_dir"
  tmp="$(mktemp)"
  render "$art_dir/deployment-contract.json" "$ref" > "$tmp"
  mv "$tmp" "$stage_dir/defaults_${version}.yaml"
  rm -rf "$art_dir"                                  # raw artefacts consumed
  # mktemp left the file mode 0600; make it world-readable so the container's
  # non-root user can read it back during config-check (mounted read-only).
  chmod 0644 "$stage_dir/defaults_${version}.yaml"
  cp "$stage_dir/defaults_${version}.yaml" "$stage_dir/defaults_latest.yaml"

  # Self-validate: `--config` is a global flag, so it precedes the subcommand.
  validation="pass"
  if ! docker run --rm -v "$stage_dir/defaults_${version}.yaml:/etc/dfe/cfg.yaml:ro" \
       --entrypoint "$binary" "$ref" --config /etc/dfe/cfg.yaml config-check >&2; then
    validation="fail"
  fi
  write_status "$stage" "$name" "$version" "generated" "$validation"
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    render) render "$@" ;;
    run)    run "$@" ;;
    *) echo "usage: $0 {render|run} ..." >&2; exit 2 ;;
  esac
}

main "$@"
