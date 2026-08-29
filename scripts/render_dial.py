#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         scripts/render_dial.py
#  Purpose:      Render the deployment dial (docker-vm slice) into .env
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Render the deployment dial into dfe-docker's .env.

The deployment dial (``deployment.yaml``) is the single SSoT a deployment turns:
one file the whole automation reads, authored by hand today and populated by the
QA GUI wizard later. This renders the docker-vm SLICE of that dial into the flat
.env docker compose consumes, so a redeploy is a dial edit plus
``make dial && make login && make stack && make ci``.

The canonical superset schema + example live in dfe-infra (the DeployContext
owner). dfe-docker consumes the docker-vm subset so a lone clone deploys
standalone. ``make init`` still mints the generated secrets; this MERGES the
dial-controlled keys over that .env, leaving the minted secrets and every other
setting untouched.

Dependency-free (no PyYAML) - the scripts run under a plain ``python3`` on the
devex VMs, and the dial's docker-vm slice is scalar/nested-map only. Secrets are
never touched here: GHCR credentials come from OpenBao via hyperi-infra's thin
caller, not from this renderer.
"""

from __future__ import annotations

import re

from _common import (
    DEPLOYMENT_DIAL,
    DEPLOYMENT_DIAL_TEMPLATE,
    DOTENV_FILE,
    _parse_yaml_subset,
    _print,
    _rel_path,
)

# (dial path) -> .env key. Order matters: a later entry overrides an earlier one
# for the same key, so `docker.profile` wins over the top-level `profile`.
_DOCKER_DIAL_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("registry",), "IMAGE_REGISTRY"),
    (("version", "pin"), "DFE_STACK_VERSION"),
    (("profile",), "DFE_PROFILE"),
    (("docker", "profile"), "DFE_PROFILE"),
    (("docker", "kafka_backend"), "KAFKA_BACKEND"),
    (("docker", "kafbat_enabled"), "KAFBAT_ENABLED"),
    (("docker", "core_enabled"), "DFE_CORE_ENABLED"),
    (("docker", "hyperdx_enabled"), "DFE_HYPERDX_ENABLED"),
    (("docker", "otel_enabled"), "DFE_OTEL_ENABLED"),
    (("docker", "otel_exporter_endpoint"), "DFE_OTEL_EXPORTER_ENDPOINT"),
    (("docker", "post_enabled"), "DFE_POST_ENABLED"),
    (("docker", "bind_host"), "DFE_BIND_HOST"),
    (("docker", "ingress_bind_host"), "DFE_INGRESS_BIND_HOST"),
    (("docker", "bind_scope"), "DFE_BIND_SCOPE"),
    (("docker", "infra_uis_external"), "DFE_INFRA_UIS_EXTERNAL"),
    (("docker", "ui_external"), "DFE_UI_EXTERNAL"),
    (("docker", "engine_api_external"), "DFE_ENGINE_API_EXTERNAL"),
    (("docker", "kafbat_ui_external"), "DFE_KAFBAT_UI_EXTERNAL"),
    (("docker", "hyperdx_ui_external"), "DFE_HYPERDX_UI_EXTERNAL"),
    (("docker", "auth_enabled"), "DFE_AUTH_ENABLED"),
    (("docker", "oidc_issuer_url"), "DFE_OIDC_ISSUER_URL"),
    (("docker", "oidc_client_id"), "DFE_OIDC_CLIENT_ID"),
    (("docker", "oidc_allowed_groups"), "DFE_OIDC_ALLOWED_GROUPS"),
    (("docker", "oauth2_proxy_external_origin"), "DFE_OAUTH2_PROXY_EXTERNAL_ORIGIN"),
    (("endpoints", "clickhouse_host"), "CLICKHOUSE_HOST"),
)

# A dotenv assignment, live (`KEY=`) or single-hash commented (`# KEY=`). Mirrors
# init.py's convention: `#` is a commented setting, `##` is prose.
_SETTING_RE = re.compile(r"^[ \t]*(?:#[ \t]?)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=")


def _scalar(dial: dict[str, object], path: tuple[str, ...]) -> str | None:
    """Return the non-empty scalar at ``path`` in the parsed dial, else None."""
    node: object = dial
    for step in path:
        if not (isinstance(node, dict)):
            return None
        node = node.get(step)
    return node.strip() if isinstance(node, str) and node.strip() else None


def _env_updates(*, dial: dict[str, object]) -> dict[str, str]:
    """Map the dial's docker-vm fields to the .env keys they set (empty skipped)."""
    updates: dict[str, str] = {}
    for path, env_key in _DOCKER_DIAL_MAP:
        value = _scalar(dial, path)
        if value is not None:
            updates[env_key] = value
    return updates


def _merge_env(*, updates: dict[str, str]) -> None:
    """Overwrite each mapped key in .env with the dial value, appending any new one.

    Every OTHER line - the make-init secrets, comments, untouched settings -
    survives verbatim. A mapped key's commented placeholder or stale value is
    replaced in place; a duplicate assignment of the same key is dropped.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in DOTENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SETTING_RE.match(line)
        key = match.group("key") if match else None
        if key in updates:
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
            continue
        out.append(line)

    if remaining:
        out.append("")
        out.append("## Set by `make dial` from deployment.yaml - do not edit by hand.")
        out.extend(f"{key}={value}" for key, value in remaining.items())

    with DOTENV_FILE.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")


def main() -> int:
    if not (DEPLOYMENT_DIAL.is_file()):
        raise SystemExit(
            f"render_dial: no deployment dial at {_rel_path(path=DEPLOYMENT_DIAL)} - "
            f"copy {_rel_path(path=DEPLOYMENT_DIAL_TEMPLATE)} to "
            f"{_rel_path(path=DEPLOYMENT_DIAL)} and populate it"
        )
    if not (DOTENV_FILE.is_file()):
        raise SystemExit(
            "render_dial: no .env - run `make init` first (the dial merges over it)"
        )

    dial = _parse_yaml_subset(
        text=DEPLOYMENT_DIAL.read_text(encoding="utf-8", errors="replace")
    )
    substrate = _scalar(dial, ("substrate",))
    if substrate != "docker-vm":
        raise SystemExit(
            f"render_dial: this renderer handles substrate 'docker-vm', the dial "
            f"says {substrate!r} - the k8s substrate renders via dfe-infra"
        )

    updates = _env_updates(dial=dial)
    if not (updates):
        _print(msg="dial set no docker-vm keys - .env unchanged")
        return 0

    _merge_env(updates=updates)
    _print(
        msg=f"merged {len(updates)} dial key(s) into {_rel_path(path=DOTENV_FILE)}: "
        + ", ".join(sorted(updates))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
