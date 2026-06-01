#!/usr/bin/env bash
# Project:   dfe-docker
# File:      tests/unit/test-sync-component-defaults.sh
# Purpose:   Unit tests for the dockerless `render` path of sync-component-defaults.sh
# Language:  Bash
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
#
# Assertions are single-quoted strings evaluated by check() via `eval`, so the
# expansions are intentionally deferred and `out` is used inside them.
# shellcheck disable=SC2016,SC2034
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/sync-component-defaults.sh"
FIXTURE="$REPO_ROOT/tests/unit/fixtures/sample-deployment-contract.json"
fail=0
check() { if eval "$2"; then echo "ok: $1"; else echo "FAIL: $1"; fail=1; fi; }

out="$("$SCRIPT" render "$FIXTURE" "ghcr.io/hyperi-io/dfe-transform-vrl:v1.2.3")"

check "has AUTO-GENERATED header"   'grep -q "^# AUTO-GENERATED" <<<"$out"'
check "header names source image"   'grep -q "dfe-transform-vrl:v1.2.3" <<<"$out"'
check "contains transforms.dir"     'yq -e ".transforms.dir == \"/etc/dfe-transform-vrl/transforms\"" <<<"$out" >/dev/null'
check "keys are sorted (metrics before pipeline before transforms)" \
      '[ "$(yq -e "keys | .[]" <<<"$out" | head -1)" = "metrics" ]'
check "deterministic across two runs" \
      '[ "$out" = "$("$SCRIPT" render "$FIXTURE" "ghcr.io/hyperi-io/dfe-transform-vrl:v1.2.3")" ]'

# missing .default_config must fail loudly, not emit `null`
if echo '{"app_name":"x"}' > /tmp/no-default.json && \
   "$SCRIPT" render /tmp/no-default.json "img:v0" >/dev/null 2>&1; then
  echo "FAIL: render should error when .default_config is absent"; fail=1
else
  echo "ok: render errors on missing .default_config"
fi

exit $fail
