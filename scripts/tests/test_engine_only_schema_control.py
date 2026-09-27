#  Project:      dfe-docker
#  File:         tests/test_engine_only_schema_control.py
#  Purpose:      Fail the build when a schema or topic bootstrapper comes back to
#                this repo. dfe-engine is the only thing that creates either.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The guard for engine-only schema control.

dfe-engine applies every ClickHouse object and every bootstrap Kafka topic at its
own startup, from the pinned dfe-schemas manifest, and reports healthy only once
that pass converged. No compose service creates a table or a topic any more.

That is a property of the repo rather than of a review: an init container that
creates a table starts cleanly, exits 0 and passes every compose validation,
because compose has no opinion about DDL in a command string. So it is swept for.

Four assertions, each the reappearance of something this change deleted:

1. No ClickHouse DDL and no topic-creation step in the compose files, scripts,
   config or ops trees.
2. No bootstrapper service: no `dfe-schema-init`, no `kafka-init-*`, nothing
   running the `dfe-schema` entry point, and no KAFKA_INIT_TOPICS.
3. Every service that reads a DFE table or topic gates on
   `dfe-engine: service_healthy`, which is what replaced them.
4. The same of the services scripts/instances.py GENERATES, which the committed
   file does not hold and check_compose.py never resolves: CI has no instance
   index, so the fragment is empty there and every name in the generator's own
   table goes unchecked. That is not hypothetical -- `INSTANCE_DEPENDS_ON` kept
   `kafka-init-apache` and `kafka-init-redpanda` through this change, and
   `required: false` does not excuse them: compose answers a depends_on naming a
   service it cannot find with "invalid compose project" and starts nothing.

`dlq-init` is deliberately untouched: it is a chown on a volume Docker creates
root-owned, not schema.

The compose file is read by a small block reader rather than PyYAML, because
`make check-tests` resolves a bare pytest with no third-party packages.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import instances

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

# The trees that deploy or operate the stack.
SWEPT = (
    "docker-compose.yml",
    "docker-compose.override.yml",
    "scripts",
    "config",
    "ops",
)

SKIP_SUFFIXES = (".png", ".svg", ".lock", ".pyc")

# Creating or destroying a ClickHouse object. `ALTER TABLE ... DELETE` is
# ClickHouse's mutation form of a row delete, so ALTER is matched only where it
# changes the schema.
CLICKHOUSE_DDL = re.compile(
    r"\b(?:CREATE|ATTACH)\s+(?:OR\s+REPLACE\s+)?"
    r"(?:TABLE|DATABASE|VIEW|MATERIALIZED\s+VIEW|DICTIONARY|USER|ROLE|ROW\s+POLICY)\b"
    r"|\bDROP\s+(?:TABLE|DATABASE|VIEW|DICTIONARY|USER|ROLE)\b"
    r"|\bALTER\s+TABLE\b[^\n]*\b(?:ADD|DROP|MODIFY|RENAME)\s+(?:COLUMN|TTL)\b",
    re.IGNORECASE,
)

TOPIC_CREATE = re.compile(
    r"kafka-topics\.sh[^\n]*--create|rpk\s+topic\s+create",
    re.IGNORECASE,
)

# A bootstrapper by name, wherever the name can reach a container. `dlq-init` and
# the `dfe-schemas` manifest are not matched -- neither creates anything here.
BOOTSTRAPPER_NAME = re.compile(r"dfe-schema-init|kafka-init")

# The e2e executor, which makes and drops its own throwaway topic and database
# per run against a live stack to prove the pipeline, and this file, which quotes
# what it forbids.
ALLOWED = {
    "scripts/test_e2e.py",
    "scripts/tests/test_engine_only_schema_control.py",
}

# Services whose start order the engine's schema pass governs: each reads a table
# or a topic dfe-schemas declares and dfe-engine makes.
GATED_SERVICES = (
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-e2e-vector-filebeat",
    "dfe-transform-e2e-vrl-filebeat",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "otel-collector",
)

# The two broker services, one per Kafka backend, each enabled by its own
# compose profile -- so a wait on both is a wait on whichever this tier runs.
BROKERS = ("kafka-redpanda", "kafka-apache")

_SERVICE_RE = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9._-]*):\s*$")
_DEPENDS_RE = re.compile(r"^      ([A-Za-z0-9][A-Za-z0-9._-]*):\s*$")
_CONDITION_RE = re.compile(r"^        condition:\s*(\S+)\s*$")


def _blocks(text: str) -> dict[str, list[str]]:
    """Each service's own lines, keyed by name, from the `services:` mapping."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    in_services = False
    for line in text.splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" "):
            break
        match = _SERVICE_RE.match(line)
        if match:
            current = match.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return blocks


def _generated_blocks() -> dict[str, list[str]]:
    """The same, for the services scripts/instances.py writes at run time.

    One instance of every app the generator knows, so the fragment is rendered
    rather than read: the committed file holds none of this, and CI resolves none
    of it either, because an instance index only exists once dfe-engine has run.
    """
    sample = {service: ["sample"] for service in sorted(instances.SERVICE_CONFIG_FILE)}
    return _blocks(instances.fragment(sample))


def _depends_on(block: list[str]) -> dict[str, str]:
    """One service's `depends_on` as name -> condition."""
    out: dict[str, str] = {}
    name: str | None = None
    inside = False
    for line in block:
        if line.startswith("    depends_on:"):
            inside = True
            continue
        if inside and line.startswith("    ") and not line.startswith("     "):
            break
        if not inside:
            continue
        match = _DEPENDS_RE.match(line)
        if match:
            name = match.group(1)
            out[name] = ""
            continue
        condition = _CONDITION_RE.match(line)
        if condition and name is not None:
            out[name] = condition.group(1)
    return out


def _swept_files() -> list[Path]:
    out = []
    for entry in SWEPT:
        path = REPO_ROOT / entry
        if path.is_file():
            out.append(path)
            continue
        out += [
            p
            for p in sorted(path.rglob("*"))
            if p.is_file() and p.suffix not in SKIP_SUFFIXES
        ]
    return out


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    hits = []
    for path in _swept_files():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ALLOWED:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(body.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:120]}")
    return hits


@pytest.fixture(scope="module")
def services() -> dict[str, list[str]]:
    blocks = _blocks(COMPOSE.read_text(encoding="utf-8"))
    # Loud rather than vacuous: a reader that found nothing would pass every
    # assertion below against an empty mapping.
    assert len(blocks) > 10, f"the compose reader found only {sorted(blocks)}"
    return blocks


def test_nothing_issues_clickhouse_ddl() -> None:
    hits = _offenders(CLICKHOUSE_DDL)
    assert hits == [], "ClickHouse DDL is dfe-engine's alone:\n  " + "\n  ".join(hits)


def test_nothing_creates_a_kafka_topic() -> None:
    hits = _offenders(TOPIC_CREATE)
    assert hits == [], "topic creation is dfe-engine's alone:\n  " + "\n  ".join(hits)


def test_no_bootstrapper_service_is_declared(services: dict[str, list[str]]) -> None:
    back = {n for n in services if "schema-init" in n or n.startswith("kafka-init")}
    assert back == set(), f"a bootstrapper came back: {sorted(back)}"


def test_no_service_runs_the_schema_entry_point(
    services: dict[str, list[str]],
) -> None:
    for name, block in services.items():
        entrypoints = [ln for ln in block if ln.strip().startswith("entrypoint:")]
        for line in entrypoints:
            assert "dfe-schema" not in line, f"{name} runs the dfe-schema entry point"


def test_the_topic_derivation_is_gone() -> None:
    body = (REPO_ROOT / "scripts" / "resolve_profile.py").read_text(encoding="utf-8")
    assert "KAFKA_INIT_TOPICS" not in body, (
        "resolve_profile.py still derives a topic list"
    )
    assert "KAFKA_INIT_TOPICS" not in COMPOSE.read_text(encoding="utf-8")


def test_the_dlq_chown_survives(services: dict[str, list[str]]) -> None:
    """A chown on a root-owned volume, not schema -- it stays."""
    block = "\n".join(services["dlq-init"])
    assert "chown" in block, f"dlq-init no longer chowns the spool:\n{block}"


@pytest.mark.parametrize("service", GATED_SERVICES)
def test_every_reader_waits_on_the_engine(
    services: dict[str, list[str]], service: str
) -> None:
    depends = _depends_on(services[service])
    assert "dfe-engine" in depends, (
        f"{service} starts without waiting on the schema authority"
    )
    assert depends["dfe-engine"] == "service_healthy", (
        f"{service} waits on dfe-engine on the wrong condition: {depends['dfe-engine']}"
    )


def test_no_generated_service_names_a_bootstrapper() -> None:
    """The generator's own table is rendered service text, so it is swept too."""
    hits = []
    for name, block in _generated_blocks().items():
        hits += [
            f"{name} -> {dependency}"
            for dependency in _depends_on(block)
            if BOOTSTRAPPER_NAME.search(dependency)
        ]
    assert hits == [], (
        "a bootstrapper came back through the generator:\n  " + "\n  ".join(hits)
    )


def test_every_generated_dependency_is_a_declared_service(
    services: dict[str, list[str]],
) -> None:
    """Compose refuses the whole project over a depends_on it cannot resolve.

    `required: false` does not soften that -- it decides whether an absent
    CONTAINER blocks the wait, not whether an undeclared SERVICE parses, so a
    generated service naming one starts nothing at all.
    """
    undefined = []
    for name, block in _generated_blocks().items():
        undefined += [
            f"{name} -> {dependency}"
            for dependency in _depends_on(block)
            if dependency not in services
        ]
    assert undefined == [], (
        "a generated service depends on something this repo declares no service "
        "for, which fails the whole project:\n  " + "\n  ".join(undefined)
    )


def test_every_generated_service_waits_on_the_engine() -> None:
    for name, block in _generated_blocks().items():
        depends = _depends_on(block)
        assert depends.get("dfe-engine") == "service_healthy", (
            f"{name} starts without waiting on the schema authority: {depends}"
        )


@pytest.mark.parametrize("broker", BROKERS)
def test_the_engine_waits_on_the_broker_it_creates_topics_on(
    services: dict[str, list[str]], broker: str
) -> None:
    """The engine is now the only thing that talks to the broker at boot.

    It is also the only edge that STARTS one: a broker is in no profile's
    started set, so it comes up as somebody's dependency or not at all. The
    retired kafka-init one-shots used to carry that edge, and every reader
    named one; when they went, the single tier came up with no broker.
    """
    depends = _depends_on(services["dfe-engine"])
    assert depends.get(broker) == "service_healthy", (
        f"dfe-engine does not wait on {broker}, so nothing starts it: {depends}"
    )
