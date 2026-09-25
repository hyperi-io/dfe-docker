#!/usr/bin/env python3

#  Project:   dfe-docker
#  File:      scripts/test-e2e.py
#  Purpose:   Config-driven e2e test runner for the DFE Docker stack
#  Language:  Python
#
#  License:   BUSL-1.1
#  Copyright: (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    ./scripts/test-e2e.py                                       # Run all tests from config
#    ./scripts/test-e2e.py kafka-full                            # Run specific test by name
#    ./scripts/test-e2e.py kafka-full grpc-full                  # Run multiple named tests
#    ./scripts/test-e2e.py --outages                             # Run the outage tests instead
#    TEST_CONFIG=tests/e2e/e2e-tests.yaml ./scripts/test-e2e.py  # Custom config
#    LOG_LEVEL=debug ./scripts/test-e2e.py                       # Verbose service logs

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - environment guard
    # PyYAML is the harness's only third-party dependency and it is NOT in the
    # stdlib, so a plain `make test-e2e` on a fresh machine dies with a bare
    # ModuleNotFoundError that says nothing about how to proceed. Every other
    # missing prerequisite here (docker) gets an actionable message; this one
    # should too.
    sys.exit(
        "test_e2e: PyYAML is required and not installed.\n"
        "  Install it:      pip install pyyaml\n"
        "  Or run via uv:   uv run --with pyyaml python3 scripts/test_e2e.py"
    )

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree, which
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import _outage
from _common import FALSY, _config_argument, _load_dotenv, _use_mounted_configs
from _pipeline import MARKER_EXPRESSIONS  # noqa: F401 - re-exported for callers
from _pipeline import ch_marker_count as _ch_marker_count
from _pipeline import clickhouse_url, env_or, expand_env, otel_fresh_counts, poll_until


# ==============================================================================
# Global Variables
# ==============================================================================

# ------------------------------------------------------------------------------
# Project Related
# - Paths and identifiers for the project and test run
# ------------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
RUN_ID = f"e2e-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
TMP_DIR = PROJECT_DIR / ".tmp"

# ------------------------------------------------------------------------------
# Config Related
# - Supported config values
# ------------------------------------------------------------------------------
MODES = ["dev", "ci"]
DEFAULTS = {
    "data_file": "tests/e2e/data/events.jsonl",
    "mode": "ci",
    "persistent_services": ["clickhouse"],
}

# ------------------------------------------------------------------------------
# Service Layout
# - Source of truth for service_profiles.yaml resolution
#   - SERVICE_PROFILES_FILE: path to service_profiles.yaml at the repo root
#   - SERVICE_CONFIG_MOUNTS: where each DFE service expects its config in-container
#   - SERVICE_TO_COMPOSE_PROFILE: docker-compose profile name for each DFE service
# ------------------------------------------------------------------------------
SERVICE_PROFILES_FILE = Path(__file__).resolve().parent.parent / "service_profiles.yaml"

SERVICE_CONFIG_MOUNTS = {
    "dfe-archiver": "/etc/dfe/archiver.yaml",
    "dfe-fetcher": "/etc/dfe/fetcher.yaml",
    "dfe-loader": "/etc/dfe/loader.yaml",
    "dfe-receiver": "/etc/dfe-receiver/config.yaml",
    "dfe-transform-elastic": "/etc/dfe-transform-elastic/config.yaml",
    "dfe-transform-elastic-cisco-ios": "/etc/dfe-transform-elastic/config.yaml",
    "dfe-transform-vector": "/etc/dfe-transform-vector/config.yaml",
    "dfe-transform-vector-filebeat": "/etc/dfe-transform-vector/config.yaml",
    "dfe-transform-vrl": "/etc/dfe-transform-vrl/config.yaml",
    "dfe-transform-vrl-filebeat": "/etc/dfe-transform-vrl/config.yaml",
}

KNOWN_DFE_SERVICES = set(SERVICE_CONFIG_MOUNTS.keys())

# Extra compose files chained onto every `up`, e.g. one renaming the containers
# so this stack can run beside another on the same daemon.
EXTRA_COMPOSE_FILES_ENV_VAR = "DFE_E2E_COMPOSE_FILES"

# ------------------------------------------------------------------------------
# Schema Authority
# - dfe-engine is brought up as INFRA, health-gated before the DFE services,
#   because the loader pre-warms the schemas it registers. The harness sends with
#   _source = the target table, so rows land in the engine-provisioned main
#   table. docs/developing.md#end-to-end-suite----one-stack-per-test
# ------------------------------------------------------------------------------
SCHEMA_AUTHORITY_SERVICE = "dfe-engine"
SCHEMA_AUTHORITY_PROFILE = "core"
# The rest of the `core` profile: the user-facing surface a complete-stack test
# asserts against. dfe-engine is started separately, earlier, as the authority.
CORE_SURFACE_SERVICES = ["dfe-ui", "dfe-proxy"]
OTEL_PROFILE = "otel"
OTEL_SERVICE = "otel-collector"
OTEL_ENDPOINT_ENV_VAR = "DFE_OTEL_EXPORTER_ENDPOINT"
OTEL_BUNDLED_ENDPOINT = "http://otel-collector:4317"
OTEL_ENGINE_BACKEND_ENV_VAR = "DFE_ENGINE_METRICS_BACKEND"
OTEL_ENGINE_BACKEND = "opentelemetry"
OTEL_FRESH_WINDOW_SECONDS = 300
OTEL_TIMEOUT_SECONDS = 120.0
OTEL_INTERVAL_SECONDS = 5.0
# dfe-archiver writes a batch only once it is big or old enough, so the archive
# check waits this long before it calls the archive missing.
ARCHIVE_TIMEOUT_SECONDS = 180
ARCHIVE_INTERVAL_SECONDS = 10
TARGET_DB = "dfe"
TARGET_TABLE = "main"

# Marker lookup lives in _pipeline, shared with the power-on self test.


# ------------------------------------------------------------------------------
# Load Dotenv
# - Shared with init/stack/resolve_profile via _common. This file used to carry a
#   third private copy of the same parser, and three copies of "how do we read
#   .env" is three chances to disagree with docker compose about it.
# - Every rendered-config variable is then blanked. `make` exports the engine's
#   render paths, and compose inherits them unless each service is sent back to
#   the config this suite mounts for it.
# ------------------------------------------------------------------------------
_load_dotenv()
_use_mounted_configs(environ=os.environ)

# ------------------------------------------------------------------------------
# Endpoint Related
# - Configurable URLs for services, with appropriate defaults
# - The Makefile exports these keys even when .env leaves them unset, so an
#   empty value must fall back like an absent one (env_or / clickhouse_url).
# ------------------------------------------------------------------------------
CLICKHOUSE_URL = clickhouse_url()
CLICKHOUSE_USERNAME = env_or("CLICKHOUSE_USERNAME", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
# Readiness, not liveness -- the harness needs "usable", not "the process exists".
# Every service uses /readyz, matching the compose healthchecks. The
# /health/live|ready|startup aliases are gone from the scalo-py in the pinned
# dfe-engine and 404, which reads as an engine that never comes ready.
DFE_LOADER_HEALTH_URL = env_or(
    "DFE_LOADER_HEALTH_URL",
    f"http://localhost:{env_or('DFE_LOADER_PROMETHEUS_PORT', '9091')}/readyz",
)
DFE_RECEIVER_HEALTH_URL = env_or(
    "DFE_RECEIVER_HEALTH_URL",
    f"http://localhost:{env_or('DFE_RECEIVER_PROMETHEUS_PORT', '9090')}/readyz",
)
DFE_ARCHIVER_HEALTH_URL = env_or(
    "DFE_ARCHIVER_HEALTH_URL",
    f"http://localhost:{env_or('DFE_ARCHIVER_PROMETHEUS_PORT', '9093')}/readyz",
)
DFE_FETCHER_HEALTH_URL = env_or(
    "DFE_FETCHER_HEALTH_URL",
    f"http://localhost:{env_or('DFE_FETCHER_PROMETHEUS_PORT', '9094')}/readyz",
)
DFE_ENGINE_HEALTH_URL = env_or(
    "DFE_ENGINE_HEALTH_URL",
    f"http://localhost:{env_or('DFE_ENGINE_PORT', '8003')}/readyz",
)
DFE_FETCHER_INGEST_URL = env_or(
    "DFE_FETCHER_INGEST_URL",
    f"http://localhost:{env_or('DFE_FETCHER_INGEST_PORT', '8082')}/ingest",
)
DFE_RECEIVER_INGEST_URL = env_or(
    "DFE_RECEIVER_INGEST_URL",
    f"http://localhost:{env_or('DFE_RECEIVER_HTTP_PORT', '8080')}/ingest",
)
TEST_CONFIG = Path(
    os.environ.get("TEST_CONFIG", str(PROJECT_DIR / "tests" / "e2e" / "e2e-tests.yaml"))
)

# ------------------------------------------------------------------------------
# Kafka Backend
# - Mirrors scripts/resolve_profile.py: KAFKA_BACKEND selects BOTH the compose
#   profile and the broker service for kafka-transport tests. Profile name and
#   service name are identical (service 'kafka-redpanda' lives in profile
#   'kafka-redpanda'; likewise 'kafka-apache'). The in-network alias is always
#   'kafka:9092', so downstream services and `--bootstrap-server` address that,
#   but `docker compose exec` needs the real SERVICE name -> KAFKA_SERVICE.
#   Unknown backend falls back to redpanda (the stack default).
# ------------------------------------------------------------------------------
KAFKA_BACKEND = os.environ.get("KAFKA_BACKEND", "redpanda").strip().lower()
KAFKA_BACKEND_PROFILES = {
    "redpanda": "kafka-redpanda",
    "apache": "kafka-apache",
}
KAFKA_BACKEND_PROFILE = KAFKA_BACKEND_PROFILES.get(KAFKA_BACKEND, "kafka-redpanda")
KAFKA_SERVICE = KAFKA_BACKEND_PROFILE
KAFKA_UI_PROFILE = "kafka-ui"
KAFKA_BOOTSTRAP = "kafka:9092"

# ------------------------------------------------------------------------------
# Outage Tests
# - A test with an `outage:` block stops one service under steady load and
#   starts it again. docs/developing.md#outage-tests----a-service-stopped-under-load
# ------------------------------------------------------------------------------
OUTAGE_DEFAULT_SECONDS = 60
OUTAGE_WORKERS = 4
OUTAGE_INTERVAL_SECONDS = 0.5
# Longer than the outage, so a request the receiver holds gets its real answer.
OUTAGE_REQUEST_TIMEOUT = 120
OUTAGE_READY_TIMEOUT = 180
OUTAGE_LEAD_SECONDS = 20
OUTAGE_TAIL_SECONDS = 30
OUTAGE_RECOVERY_TIMEOUT = 180
OUTAGE_SETTLE_TIMEOUT = 300
OUTAGE_POLL_SECONDS = 5

# ------------------------------------------------------------------------------
# Logging Related
# - Custom log levels for test results (skip/pass/fail) and coloured output
# ------------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
LOGGER = logging.getLogger("e2e")
SKIP_LEVEL = 24
PASS_LEVEL = 25
FAIL_LEVEL = 26


# ==============================================================================
# Class Initialisation
# ==============================================================================


# ------------------------------------------------------------------------------
# Test Context
# - For tracking test results and shared state across test phases
# ------------------------------------------------------------------------------
@dataclass
class TestContext:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total_sent: int = 0


# ------------------------------------------------------------------------------
# Test Case
# - Represents a single test case with required parameters and defaults
#   - profile: service_profiles.yaml profile name (e.g. kafka-receiver)
#   - compose_profiles: resolved compose profiles including infra (clickhouse, kafka backend profile, kafka-ui)
#   - services: resolved {dfe-service: project-relative config path}
#   - outage: the plan of a test that stops, kills or pauses a service under load
# ------------------------------------------------------------------------------
@dataclass
class TestCase:
    name: str
    profile: str
    compose_profiles: list[str]
    services: dict[str, str]
    data_file: str
    database: str
    table: str
    marker: str
    persistent_services: list[str] = field(default_factory=list)
    expected_topics: list[str] = field(default_factory=list)
    extra_services: list[str] = field(default_factory=list)
    expected_http: list[dict] = field(default_factory=list)
    outage: _outage.OutagePlan | None = None


# ------------------------------------------------------------------------------
# Coloured Formatter
# - Custom logging formatter to add colours based on log level
# ------------------------------------------------------------------------------
class ColouredFormatter(logging.Formatter):
    COLOURS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "",
        SKIP_LEVEL: "\033[1;33m",
        PASS_LEVEL: "\033[1;32m",
        FAIL_LEVEL: "\033[1;31m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record):
        message = super().format(record)
        level = record.levelname
        if sys.stderr.isatty():
            colour = self.COLOURS.get(record.levelno, "")
            return f"{colour}[{level}]{self.RESET} {message}"
        return f"[{level}] {message}"


# ==============================================================================
# Test Markers
# - Helper functions to mark test results and log with appropriate levels
# ==============================================================================


# ------------------------------------------------------------------------------
# Skip Marker
# - Log a skipped test and increment skip count
# ------------------------------------------------------------------------------
def mark_skip(ctx, label):
    LOGGER.log(SKIP_LEVEL, label)
    ctx.skipped += 1


# ------------------------------------------------------------------------------
# Pass Marker
# - Log a passed test and increment pass count
# ------------------------------------------------------------------------------
def mark_pass(ctx, label):
    LOGGER.log(PASS_LEVEL, label)
    ctx.passed += 1


# ------------------------------------------------------------------------------
# Fail Marker
# - Log a failed test and increment fail count
# ------------------------------------------------------------------------------
def mark_fail(ctx, label):
    LOGGER.log(FAIL_LEVEL, label)
    ctx.failed += 1


# ==============================================================================
# Connection Functions
# - Helpers for making HTTP requests and querying ClickHouse
# ==============================================================================


# ------------------------------------------------------------------------------
# HTTP Get
# - Helper to perform a HTTP GET request and return the response body
# ------------------------------------------------------------------------------
def http_get(url, timeout=5):
    request = Request(url, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode()


# ------------------------------------------------------------------------------
# HTTP Post
# - Helper to perform a HTTP POST request with set body and content type
# ------------------------------------------------------------------------------
def http_post(url, body, content_type="application/json", timeout=10):
    data = body.encode() if (isinstance(body, str)) else body
    request = Request(url, data=data, method="POST")
    request.add_header("Content-Type", content_type)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status
    except URLError as e:
        if hasattr(e, "code"):
            return e.code
        raise


# ------------------------------------------------------------------------------
# ClickHouse Query
# - Execute a SQL query against ClickHouse via HTTP and return the result
# ------------------------------------------------------------------------------
def ch_query(sql):
    LOGGER.debug(f"Executing ClickHouse query: `{sql}`")
    data = sql.encode()
    request = Request(CLICKHOUSE_URL, data=data, method="POST")
    request.add_header("X-ClickHouse-User", CLICKHOUSE_USERNAME)
    request.add_header("X-ClickHouse-Key", CLICKHOUSE_PASSWORD)
    try:
        with urlopen(request, timeout=10) as response:
            return response.read().decode().strip()
    except Exception as e:
        LOGGER.debug(f"ClickHouse query failed. Error message: {e}")
        return ""


# ==============================================================================
# Config Functions
# - Helpers for loading test configuration and extracting fields with defaults
# ==============================================================================


# ------------------------------------------------------------------------------
# Load Config
# - Load test configuration from a YAML file
# ------------------------------------------------------------------------------
def load_config(path):
    with open(path, encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


# ------------------------------------------------------------------------------
# Get Config
# - Resolves a config value: test_config -> global_config -> DEFAULTS
#   Each tier is searched independently - no merging or combining
# ------------------------------------------------------------------------------
def get_config(
    path, test_config, global_config, default_value=None, required=False, is_list=False
):
    keys = path.split(".") if ("." in path) else [path]
    for config in [test_config, global_config, DEFAULTS]:
        value = config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                value = None
                break
        if value is not None:
            if is_list:
                return value if (isinstance(value, list)) else []
            return value
    if default_value is not None:
        return default_value
    if required:
        error(f"Missing required config field '{path}'")
    return [] if (is_list) else None


# ------------------------------------------------------------------------------
# Generate Temporary Loader Config
# - Generates a temporary loader config file with the tests database and table
# ------------------------------------------------------------------------------
def generate_tmp_loader_config(loader_config, database, table):
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    with open(PROJECT_DIR / loader_config, encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    config.setdefault("clickhouse", {})["database"] = database
    config.setdefault("routing", {})["default_db"] = database
    config.setdefault("routing", {})["default_table"] = table

    generated_path = TMP_DIR / f"loader-{RUN_ID}-{table}.yaml"
    with open(generated_path, "w", encoding="utf-8", newline="\n") as generated_file:
        yaml.safe_dump(config, generated_file, default_flow_style=False)

    LOGGER.debug(f"Generated loader config: '{generated_path}'")
    return str(generated_path.relative_to(PROJECT_DIR))


# ==============================================================================
# Services / Compose Resolution
# - Resolve service_profiles.yaml profiles into compose profiles + DFE service configs
# ==============================================================================

# ------------------------------------------------------------------------------
# Load Services YAML
# - Caches the parsed service_profiles.yaml on first read
# ------------------------------------------------------------------------------
_SERVICES_YAML_CACHE = None


def load_services_yaml():
    global _SERVICES_YAML_CACHE
    if _SERVICES_YAML_CACHE is None:
        if not (SERVICE_PROFILES_FILE.exists()):
            error(f"service_profiles.yaml not found at '{SERVICE_PROFILES_FILE}'")
        with open(SERVICE_PROFILES_FILE, encoding="utf-8") as services_file:
            _SERVICES_YAML_CACHE = yaml.safe_load(services_file)
    return _SERVICES_YAML_CACHE


# ------------------------------------------------------------------------------
# Resolve Services Profile
# - Resolve a service_profiles.yaml profile into (compose_profiles, services_dict)
#   - compose_profiles always includes 'clickhouse'; kafka backend profile + 'kafka-ui' when transport=kafka
#   - services_dict maps {dfe-service: project-relative config path}
# ------------------------------------------------------------------------------
def resolve_services_profile(profile_name):
    data = load_services_yaml()
    profiles = data.get("profiles") or {}
    if profile_name not in profiles:
        available = ", ".join(sorted(profiles.keys()))
        error(
            f"Profile '{profile_name}' not found in 'service_profiles.yaml'. Available: {available}"
        )

    profile = profiles[profile_name]
    transport = profile.get("transport", "")
    if transport not in ("kafka", "grpc"):
        error(
            f"Profile '{profile_name}': transport must be 'kafka' or 'grpc', got '{transport}'"
        )

    raw_services = profile.get("services") or {}
    if not (raw_services):
        error(f"Profile '{profile_name}': no services defined")

    compose_profiles = ["clickhouse", SCHEMA_AUTHORITY_PROFILE]
    if transport == "kafka":
        compose_profiles += [KAFKA_BACKEND_PROFILE, KAFKA_UI_PROFILE]

    # Footprint keys from service_profiles.yaml, defaults matching
    # scripts/resolve_profile.py. dfe-engine already starts as the schema
    # authority; `core` adds the UI and the proxy, `otel` the collector, so a
    # complete-stack profile is tested whole. `clickhouse` and `hyperdx` are
    # ignored: the suite owns the warehouse it asserts against and carries no
    # HyperDX assertions.
    extra_services = []
    if str(profile.get("core", "true")).strip().lower() not in FALSY:
        extra_services += CORE_SURFACE_SERVICES
    if (
        transport == "kafka"
        and str(profile.get("kafbat", "true")).strip().lower() not in FALSY
    ):
        extra_services.append("kafka-ui")
    if str(profile.get("otel", "false")).strip().lower() not in FALSY:
        compose_profiles.append(OTEL_PROFILE)
        extra_services.append(OTEL_SERVICE)
        # The harness drives compose directly, so it sets what resolve_profile.py
        # would have exported through make.
        os.environ.setdefault(OTEL_ENDPOINT_ENV_VAR, OTEL_BUNDLED_ENDPOINT)
        os.environ.setdefault(OTEL_ENGINE_BACKEND_ENV_VAR, OTEL_ENGINE_BACKEND)

    services = {}
    for svc_name, svc_conf in raw_services.items():
        if svc_name not in KNOWN_DFE_SERVICES:
            error(f"Profile '{profile_name}': unknown service '{svc_name}'")
        if not (isinstance(svc_conf, dict)) or "config_path" not in svc_conf:
            error(
                f"Profile '{profile_name}': service '{svc_name}' missing 'config_path'"
            )
        services[svc_name] = f"config/{svc_conf['config_path']}"

    return compose_profiles, services, extra_services


# ==============================================================================
# Docker Compose Parsing/Setup
# - Docker-compose-wide helpers used for teardown and log dumping
# ==============================================================================


# ------------------------------------------------------------------------------
# Parse Compose Profiles
# - Reads docker-compose.yml and builds a mapping of profiles to services
# ------------------------------------------------------------------------------
def parse_compose_profiles():
    compose_path = PROJECT_DIR / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as compose_file:
        compose_data = yaml.safe_load(compose_file)

    profile_map = {}
    for service_name, service_data in compose_data.get("services", {}).items():
        for profile in service_data.get("profiles", []):
            profile_map.setdefault(profile, []).append(service_name)
    return profile_map


# ------------------------------------------------------------------------------
# Parse Compose Services
# - Returns the list of every service defined in docker-compose.yml
# ------------------------------------------------------------------------------
def parse_compose_services():
    compose_path = PROJECT_DIR / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as compose_file:
        compose_data = yaml.safe_load(compose_file)
    return list(compose_data.get("services", {}).keys())


# ------------------------------------------------------------------------------
# Generate Compose Override
# - Creates a temp docker-compose override file to mount test-specific configs
#   services: dict {dfe-service-name: project-relative config path}
# ------------------------------------------------------------------------------
def generate_compose_override(services):
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    override_file = TMP_DIR / f"docker-compose.{RUN_ID}.e2e.yaml"

    content = "services:\n"
    for svc_name in sorted(services):
        mount = SERVICE_CONFIG_MOUNTS[svc_name]
        content += (
            f"  {svc_name}:\n    volumes:\n      - ./{services[svc_name]}:{mount}:ro\n"
        )

    override_file.write_text(content, encoding="utf-8", newline="\n")
    LOGGER.debug(f"Generated compose override: '{override_file}'")
    return str(override_file)


# ==============================================================================
# Docker Stack Lifecycle
# - Starting and stopping the stack, waiting for services to be healthy
# ==============================================================================


# ------------------------------------------------------------------------------
# Filter Build Output
# - Filters Docker build output to show only relevant lines
# ------------------------------------------------------------------------------
def filter_build_output(text):
    if not (text):
        return ""
    output_lines = []

    for line in text.splitlines():
        stripped_line = line.strip()
        if not (stripped_line):
            continue

        # Step Headers: "#N [service stage N/M] CMD"
        if (
            stripped_line.startswith("#")
            and " [" in stripped_line
            and "]" in stripped_line
        ):
            output_lines.append(line)
        # Completion: "DONE", "CACHED", "ERROR"
        elif (
            ("DONE" in stripped_line)
            or ("CACHED" in stripped_line)
            or ("ERROR" in stripped_line)
        ):
            output_lines.append(line)
        # curl/wget Failures
        elif ("curl:" in stripped_line) or ("wget:" in stripped_line):
            output_lines.append(line)
        # Exit Codes from Failed Commands
        elif (stripped_line.startswith("ERROR:")) or (
            "process" in stripped_line
            and "did not complete successfully" in stripped_line
        ):
            output_lines.append(line)
    return "\n".join(output_lines)


# ------------------------------------------------------------------------------
# Build Images
# - Builds DFE service images once on startup
#   dfe_services: iterable of DFE service names (dfe-loader, dfe-receiver, ...)
# ------------------------------------------------------------------------------
def build_images(mode, dfe_services):
    """Build the dev images, or explain why there is nothing to build.

    This used to run `docker compose build <services>`, which built NOTHING: no
    service in either compose file declares a `build:` key, and compose answers a
    build request for image-only services with `No services to build` and exit 0.
    So it logged "Building images for ..." and moved on, and in dev mode the run
    then used whatever stale `<service>:local` images happened to exist. A silent
    no-op that reads like a safety net is worse than no safety net.

    The real builder is scripts/build_dev_images.py -- each Rust component
    compiled from local source, then packaged by the component's own Dockerfile.
    """
    dfe_services = sorted(dfe_services)
    if not (dfe_services):
        LOGGER.info("No DFE services to build")
        return

    if mode == "ci":
        # Registry images. `stack_up` pulls them as part of `up`; there is no
        # local build step, and pretending otherwise is what this replaced.
        LOGGER.info(
            f"Mode 'ci': using registry images for {len(dfe_services)} service(s)"
        )
        return

    if len(dfe_services) > 1:
        services_str = f"'{"', '".join(dfe_services[:-1])}' and '{dfe_services[-1]}'"
    else:
        services_str = f"'{dfe_services[0]}'"
    LOGGER.info(f"Building images from local source for {services_str}...")

    build_result = run_cmd(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "build_dev_images.py"),
            *dfe_services,
        ],
        capture=not (LOG_LEVEL == "DEBUG"),
    )
    if build_result.returncode != 0:
        filtered = filter_build_output(build_result.stderr or build_result.stdout or "")
        error(
            f"Docker image build failed: {filtered if (filtered) else (build_result.stderr or build_result.stdout)}"
        )


# ------------------------------------------------------------------------------
# Run Command
# - Runs a subprocess command with logging and error handling
# ------------------------------------------------------------------------------
def run_cmd(args, capture=False, cwd=None):
    """Run a command and hand back the CompletedProcess; callers judge the result.

    There is no `check` parameter. There used to be, and both of its branches
    returned the same thing -- so every call site passed `check=False` to opt out
    of something that never happened. A parameter that does nothing is worse than
    no parameter: it reads as a safety net.

    `encoding`/`errors` are explicit because `text=True` alone decodes with the
    LOCALE encoding, so one non-UTF-8 byte in captured container output would
    raise UnicodeDecodeError and take down a test run instead of degrading.
    """
    LOGGER.debug(f"Executing command: `$ {' '.join(args)}`...")
    kwargs = {"cwd": cwd or PROJECT_DIR}
    if capture:
        kwargs["capture_output"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
        kwargs["text"] = True
    return subprocess.run(args, **kwargs)


# ------------------------------------------------------------------------------
# Stack Up
# - Starts the Docker stack with the appropriate compose files and profile
# ------------------------------------------------------------------------------
def stack_up(mode, test, services):
    LOGGER.info("Starting stack...")
    LOGGER.debug(f"Compose profiles for test '{test.name}':")
    for profile in test.compose_profiles:
        LOGGER.debug(f"  - {profile}")

    override_file = generate_compose_override(services)

    compose_files = ["-f", "docker-compose.yml"]
    if mode not in MODES:
        error(f"Unknown mode: '{mode}'. Supported modes: '{MODES}'")
    elif mode == "dev" and (PROJECT_DIR / "docker-compose.override.yml").exists():
        compose_files += ["-f", "docker-compose.override.yml"]

    for extra in os.environ.get(EXTRA_COMPOSE_FILES_ENV_VAR, "").split(os.pathsep):
        if extra.strip():
            compose_files += ["-f", extra.strip()]
    compose_files += ["-f", override_file]

    # Schema authority: dfe-engine provisions dfe.main and registers the schemas
    # the loader pre-warms on startup. Bring it up with ClickHouse and gate on its
    # health BEFORE the DFE services, so the loader caches the schema instead of
    # holding every message pending-schema and dead-lettering it.
    LOGGER.info("Preparing schema authority (ClickHouse + dfe-engine)...")
    authority_up = (
        ["docker", "compose"]
        + compose_files
        + [
            "--profile",
            "clickhouse",
            "--profile",
            SCHEMA_AUTHORITY_PROFILE,
            "up",
            "-d",
            "clickhouse",
            SCHEMA_AUTHORITY_SERVICE,
        ]
    )
    authority_result = run_cmd(authority_up, capture=not (LOG_LEVEL == "DEBUG"))
    if authority_result.returncode != 0:
        filtered = filter_build_output(
            authority_result.stderr or authority_result.stdout or ""
        )
        LOGGER.error(
            f"Schema authority failed to start: {filtered if (filtered) else (authority_result.stderr or authority_result.stdout)}"
        )
        return False
    if not (wait_for_service("dfe-engine", DFE_ENGINE_HEALTH_URL, max_attempts=45)):
        return False

    # Kafka profiles: bring up infra first, create topics, then start the full profile.
    # The loader fails on startup if no matching topics exist on the broker.
    has_kafka = KAFKA_BACKEND_PROFILE in test.compose_profiles
    if has_kafka and test.expected_topics:
        infra_cmd = (
            ["docker", "compose"]
            + compose_files
            + [
                "--profile",
                "clickhouse",
                "--profile",
                KAFKA_BACKEND_PROFILE,
                "--profile",
                KAFKA_UI_PROFILE,
            ]
        )
        infra_up = infra_cmd + ["up", "-d"]

        LOGGER.info("Preparing 'Kafka' service...")
        infra_result = run_cmd(infra_up, capture=not (LOG_LEVEL == "DEBUG"))
        if infra_result.returncode != 0:
            filtered = filter_build_output(
                infra_result.stderr or infra_result.stdout or ""
            )
            LOGGER.error(
                f"'Kafka' infrastructure failed to start: {filtered if (filtered) else (infra_result.stderr or infra_result.stdout)}"
            )
            return False

        # Named services, never the whole profile: `--wait` counts an EXITED
        # container as a failure even on exit 0, and topic-init is a one-shot.
        # docs/developing.md#end-to-end-suite----one-stack-per-test
        LOGGER.debug("Waiting for 'Kafka' to be healthy...")
        wait_cmd = (
            ["docker", "compose"]
            + compose_files
            + [
                "--profile",
                "clickhouse",
                "--profile",
                KAFKA_BACKEND_PROFILE,
                "up",
                "--wait",
                "--wait-timeout",
                "60",
                "-d",
                "clickhouse",
                KAFKA_SERVICE,
            ]
        )
        wait_result = run_cmd(wait_cmd, capture=not (LOG_LEVEL == "DEBUG"))
        if wait_result.returncode != 0:
            detail = (wait_result.stderr or wait_result.stdout or "").strip()
            LOGGER.error(
                f"'Kafka' broker not healthy within timeout{f': {detail}' if detail else ''}"
            )
            return False
        LOGGER.debug("'Kafka' is healthy")

        clean_topics(test.expected_topics)
        if not (create_topics(test.expected_topics)):
            return False

    profile_flags = []
    for profile in test.compose_profiles:
        profile_flags += ["--profile", profile]
    base_cmd = ["docker", "compose"] + compose_files + profile_flags
    up_args = base_cmd + ["up", "-d"] + sorted(set(services) | set(test.extra_services))

    up_result = run_cmd(up_args, capture=not (LOG_LEVEL == "DEBUG"))
    if up_result.returncode != 0:
        filtered = filter_build_output(up_result.stderr or up_result.stdout or "")
        LOGGER.error(
            f"Docker compose up failed: {filtered if (filtered) else (up_result.stderr or up_result.stdout)}"
        )
        return False
    return True


# ------------------------------------------------------------------------------
# Stack Is Up
# - Checks if the stack is up
# ------------------------------------------------------------------------------
def stack_is_up():
    base_cmd = ["docker", "compose", "ps"]
    result = run_cmd(base_cmd, capture=not (LOG_LEVEL == "DEBUG"))
    if result.stdout is None:
        return result.returncode == 0
    return result.returncode == 0 and "Up" in result.stdout


# ------------------------------------------------------------------------------
# Stack Down
# - Stops the Docker stack
# ------------------------------------------------------------------------------
def stack_down(keep_services=None):
    base_cmd = ["docker", "compose"]

    profiles = parse_compose_profiles()
    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]

    if keep_services:
        all_services = set(parse_compose_services())
        stop_services = [
            service for service in all_services if (service not in keep_services)
        ]

        if not (stop_services):
            LOGGER.info(
                "All running services are in persistent_services, skipping teardown"
            )
            return

        if len(keep_services) > 1:
            keep_services_str = (
                f"{', '.join(keep_services[:-1])} and {keep_services[-1]}"
            )
        else:
            keep_services_str = keep_services[0]
        LOGGER.info(f"Stopping services (keeping {keep_services_str})...")
        stop_args = base_cmd + profile_flags + ["stop"] + stop_services
        run_cmd(stop_args, capture=not (LOG_LEVEL == "DEBUG"))
        rm_args = base_cmd + profile_flags + ["rm", "-f"] + stop_services
        run_cmd(rm_args, capture=not (LOG_LEVEL == "DEBUG"))
    else:
        LOGGER.info("Stopping stack...")
        down_args = base_cmd + profile_flags + ["down", "--remove-orphans"]
        run_cmd(down_args, capture=not (LOG_LEVEL == "DEBUG"))


# ------------------------------------------------------------------------------
# Dump Logs
# - Dumps recent logs from all containers in the stack for debugging purposes
# ------------------------------------------------------------------------------
def dump_logs():
    base_cmd = ["docker", "compose"]

    profiles = parse_compose_profiles()
    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]

    logs_args = base_cmd + profile_flags + ["logs", "--tail=50"]

    if LOG_LEVEL == "DEBUG":
        print("--------------- CONTAINER LOGS (50 TAIL LOGS) --------------")
        run_cmd(logs_args, capture=(not (LOG_LEVEL == "DEBUG")))
        print("------------------------- END LOGS -------------------------")


# ------------------------------------------------------------------------------
# Container Id
# - The container one service runs as in THIS compose project, stopped or not,
#   or empty when the project has none
# ------------------------------------------------------------------------------
def container_id(service):
    result = run_cmd(
        ["docker", "compose", "--profile", "*", "ps", "--all", "--quiet", service],
        capture=True,
    )
    ids = (result.stdout or "").split()
    return ids[0] if (result.returncode == 0 and ids) else ""


# ------------------------------------------------------------------------------
# Report Unready Service
# - Dumps what a timed-out service was actually doing, before teardown removes it
#
#   A bare timeout cannot distinguish the three states it might have been in: no
#   container, a container whose port is published but not yet listening, or a
#   process that is running and not answering. They call for different fixes, and
#   the container is gone by the time anyone reads the failure.
#
#   The container is looked up through compose, never by name: names are
#   daemon-wide, so a bare one reaches whichever stack on the host owns it.
# ------------------------------------------------------------------------------
def report_unready(name):
    container = container_id(name)
    if not (container):
        LOGGER.error(f"    '{name}' container: not found in this compose project")
        return
    inspect = run_cmd(
        [
            "docker",
            "inspect",
            "-f",
            "status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}",
            container,
        ],
        capture=True,
    )
    state = (inspect.stdout or inspect.stderr or "").strip()
    LOGGER.error(f"    '{name}' container: {state or 'not found'}")
    if inspect.returncode != 0:
        return

    health = run_cmd(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .State.Health.Log}}{{.ExitCode}} {{.Output}}{{end}}",
            container,
        ],
        capture=True,
    )
    if (health.stdout or "").strip():
        LOGGER.error(f"    '{name}' healthcheck log: {health.stdout.strip()[:400]}")

    logs = run_cmd(["docker", "logs", "--tail", "20", container], capture=True)
    tail = ((logs.stdout or "") + (logs.stderr or "")).strip()
    if tail:
        LOGGER.error(f"    '{name}' last lines:\n{tail[-2000:]}")


# ------------------------------------------------------------------------------
# Wait For Service
# - Waits for a service to become healthy by polling its endpoint
# ------------------------------------------------------------------------------
def wait_for_service(name, url, max_attempts=30):
    LOGGER.info(f"Waiting for '{name}' at '{url}'...")
    time.sleep(2)
    for attempt in range(1, max_attempts + 1):
        try:
            http_get(url, timeout=3)
            LOGGER.info(
                f"'{name}' ready after {attempt} attempt{'s' if (attempt > 1) else ''}"
            )
            return True
        except Exception:
            LOGGER.debug(
                f"    Attempt {attempt}/{max_attempts}: '{name}' not ready, retrying in 2s..."
            )
            time.sleep(2)
    LOGGER.error(
        f"'{name}' not ready after {max_attempts} attempt{'s' if (attempt > 1) else ''}"
    )
    if name.startswith("dfe-"):
        report_unready(name)
    return False


# ------------------------------------------------------------------------------
# Wait For Stack
# - Waits for all required services in the stack to become healthy
# ------------------------------------------------------------------------------
def wait_for_stack(services):
    """Wait for ClickHouse (always) and each DFE service in `services` to be healthy."""
    health_map = {
        "dfe-archiver": ("dfe-archiver", DFE_ARCHIVER_HEALTH_URL),
        "dfe-fetcher": ("dfe-fetcher", DFE_FETCHER_HEALTH_URL),
        "dfe-loader": ("dfe-loader", DFE_LOADER_HEALTH_URL),
        "dfe-receiver": ("dfe-receiver", DFE_RECEIVER_HEALTH_URL),
    }

    required_services = {"ClickHouse": f"{CLICKHOUSE_URL}/ping"}
    for svc_name in services:
        if svc_name in health_map:
            name, url = health_map[svc_name]
            required_services[name] = url

    for name, url in required_services.items():
        if not (wait_for_service(name, url)):
            return False
    return True


# ------------------------------------------------------------------------------
# Verify Config Mounts
# - Each DFE service's process runs `--config` on the file this test mounts for
#   it, read off `docker inspect`. The mount alone proves nothing: an inherited
#   DFE_*_CONFIG_FILE runs the service on another file beside it.
# ------------------------------------------------------------------------------
def verify_config_mounts(ctx, test):
    verified = True
    for service in sorted(test.services):
        want = SERVICE_CONFIG_MOUNTS[service]
        container = container_id(service)
        result = (
            run_cmd(["docker", "inspect", container], capture=True)
            if (container)
            else None
        )
        try:
            documents = json.loads(result.stdout or "[]") if (result) else []
        except ValueError:
            documents = []
        if not (documents):
            mark_fail(ctx, f"[{test.name}] '{service}' has no container to inspect")
            verified = False
            continue
        got = _config_argument(args=documents[0].get("Args") or [])
        if got == want:
            mark_pass(ctx, f"[{test.name}] '{service}' runs --config {got}")
        else:
            mark_fail(
                ctx,
                f"[{test.name}] '{service}' runs --config {got or 'nothing'}, not "
                f"the file this test mounts at {want}",
            )
            verified = False
    return verified


# ==============================================================================
# Data Helpers
# - Functions for preparing test data, sending events, and verifying results
# ==============================================================================


# ---------------------------------------------------------------------------
# Send Events
# - Reads events from a file and sends to the receiver
# ---------------------------------------------------------------------------
def send_events(ctx, test_name, marker, data_file_name, database, table, ingest_url):
    data_file_path = PROJECT_DIR / data_file_name
    if not (data_file_path.exists()):
        error(f"Data file '{data_file_path}' could not be found", test_name)

    events = data_file_path.read_text(encoding="utf-8").splitlines()
    num_events = len(events)

    LOGGER.info(f"Sending {num_events} events to '{ingest_url}'...")
    LOGGER.debug(f"Marker: '{marker}'")

    ctx.total_sent = 0
    send_errors = 0

    for line in events:
        event = json.loads(line)
        event["_source"] = table
        event["_tags"] = {"marker": marker, "test_name": test_name}
        enriched = json.dumps(event, separators=(",", ":"))

        try:
            status = http_post(ingest_url, enriched)
        except Exception:
            status = 0

        if status < 200 or status >= 300:
            LOGGER.debug(f"Response code '{status}' received for '{enriched}'")
            send_errors += 1

        ctx.total_sent += 1

    LOGGER.info(f"Sent {ctx.total_sent} events (errors = {send_errors})")

    if send_errors == 0:
        mark_pass(
            ctx,
            f"[{test_name}] All {ctx.total_sent} ingest requests successful (200 response codes)",
        )
    else:
        mark_fail(
            ctx,
            f"[{test_name}] {send_errors}/{ctx.total_sent} ingest requests failed (non-200 response codes)",
        )


# ---------------------------------------------------------------------------
# ClickHouse Row Count
# - Returns the current row count of a table (0 if it cannot be read)
# ---------------------------------------------------------------------------
def ch_count(database, table):
    raw = ch_query(f"SELECT count() FROM {database}.{table}")
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# ClickHouse Marker Count
# - Counts rows carrying THIS run's marker.
#
#   The landing table is shared and persistent (clickhouse is in
#   persistent_services), so a bare count-delta cannot tell our rows from a
#   concurrent writer's, a retry's, or a previous run's. Every event is already
#   stamped with a unique marker in send_events; this is what reads it back.
#
#   The marker lands in the _tags map. Which column that becomes is the engine's
#   business, not ours, so this asks ClickHouse where it is rather than hardcoding
#   a schema this repo does not own - and returns None (not 0) when the column
#   cannot be found, so "I could not check" stays distinguishable from "nothing
#   arrived".
# ---------------------------------------------------------------------------
def ch_marker_count(database, table, marker):
    # Shared with the power-on self test (scripts/post.py) via _pipeline. The
    # subtlety it encapsulates -- that a fallback expression which RUNS is not
    # one that WORKS, so a clean 0 can mean "cannot read markers" rather than
    # "nothing arrived" -- is exactly the kind of thing that must not exist in
    # two places and drift.
    return _ch_marker_count(database, table, marker, debug=LOGGER.debug)


# ---------------------------------------------------------------------------
# Verify Table
# - Confirms `expected` new rows landed in the engine-provisioned landing table
#
#   Two assertions, deliberately not one. The count delta proves the right VOLUME
#   arrived; the marker proves the rows are OURS. A delta alone passes when
#   somebody else's rows make up the numbers, which on a shared persistent table
#   is not hypothetical.
# ---------------------------------------------------------------------------
def verify_table(
    ctx, test_name, database, table, baseline, expected, marker, max_attempts=12
):
    target = baseline + expected
    LOGGER.info(
        f"Verifying {expected} new rows in ClickHouse table '{database}.{table}' (baseline {baseline})..."
    )
    time.sleep(3)

    actual = baseline
    matched = None
    for attempt in range(1, max_attempts + 1):
        actual = ch_count(database, table)
        matched = ch_marker_count(database, table, marker)
        got = actual - baseline
        if actual >= target and (matched is None or matched >= expected):
            LOGGER.info(f"    Attempt {attempt}/{max_attempts}: got +{got}/{expected}")
            break
        LOGGER.info(
            f"    Attempt {attempt}/{max_attempts}: got +{got}/{expected} (marker matches: {matched}), retrying in 5s..."
        )
        time.sleep(5)

    got = actual - baseline
    if actual >= target:
        mark_pass(ctx, f"[{test_name}] '{database}.{table}': +{got}/{expected} rows")
    else:
        mark_fail(
            ctx, f"[{test_name}] '{database}.{table}': expected +{expected}, got +{got}"
        )

    # A missing marker column is reported, never silently passed. If it cannot be
    # located the delta check is all we have, and the run should say so rather
    # than imply a stronger guarantee than it verified.
    if matched is None:
        mark_skip(
            ctx,
            f"[{test_name}] marker column not found in '{database}.{table}' - only the row-count delta was verified",
        )
    elif matched >= expected:
        mark_pass(
            ctx,
            f"[{test_name}] '{database}.{table}': {matched}/{expected} rows carry this run's marker",
        )
    else:
        mark_fail(
            ctx,
            f"[{test_name}] '{database}.{table}': expected {expected} rows with marker '{marker}', got {matched}",
        )


# ---------------------------------------------------------------------------
# Kafka Topic Commands
# - Backend-aware topic ops. The broker service name AND the CLI both differ by
#   backend: redpanda ships `rpk`, Apache Kafka ships kafka-topics.sh. Each is
#   execed inside the running broker service (KAFKA_SERVICE) and addresses the
#   in-network alias 'kafka:9092'. Mirrors scripts/resolve_profile.py.
# ---------------------------------------------------------------------------
def _topic_exec_base():
    return ["docker", "compose", "exec", "-T", KAFKA_SERVICE]


def _topic_create_cmd(topic):
    if KAFKA_BACKEND == "apache":
        return _topic_exec_base() + [
            "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--create",
            "--if-not-exists",
            "--topic",
            topic,
            "--partitions",
            "1",
            "--replication-factor",
            "1",
        ]
    return _topic_exec_base() + [
        "rpk",
        "topic",
        "create",
        topic,
        "--partitions",
        "1",
        "--replicas",
        "1",
        "-X",
        f"brokers={KAFKA_BOOTSTRAP}",
    ]


def _topic_delete_cmd(topic):
    if KAFKA_BACKEND == "apache":
        return _topic_exec_base() + [
            "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--delete",
            "--if-exists",
            "--topic",
            topic,
        ]
    return _topic_exec_base() + [
        "rpk",
        "topic",
        "delete",
        topic,
        "-X",
        f"brokers={KAFKA_BOOTSTRAP}",
    ]


def _topic_list_cmd():
    if KAFKA_BACKEND == "apache":
        return _topic_exec_base() + [
            "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--list",
        ]
    return _topic_exec_base() + [
        "rpk",
        "topic",
        "list",
        "-X",
        f"brokers={KAFKA_BOOTSTRAP}",
    ]


def _parse_topic_list(stdout):
    # kafka-topics.sh --list prints one topic per line; rpk topic list prints a
    # table with a 'NAME PARTITIONS REPLICAS' header, so take the first column
    # and drop that header row.
    topics = []
    for line in (stdout or "").splitlines():
        parts = line.split()
        if not (parts):
            continue
        if parts[0] == "NAME":
            continue
        topics.append(parts[0])
    return topics


# ---------------------------------------------------------------------------
# Suppressing Topics
# - Expands a topic list with the siblings that would SUPPRESS it on the broker
#
#   A `<base>_load` topic removes `<base>_land` from the loader's subscription,
#   and broker volumes outlive containers, so the sibling is deleted per run and
#   never re-created. docs/developing.md#end-to-end-suite----one-stack-per-test
# ---------------------------------------------------------------------------
KAFKA_SUPPRESSION_PAIRS = (("_land", "_load"),)


def suppressing_topics(expected_topics):
    extra = []
    for topic in expected_topics or []:
        if not (topic):
            continue
        for suppressed_suffix, preferred_suffix in KAFKA_SUPPRESSION_PAIRS:
            if topic.endswith(suppressed_suffix):
                base = topic[: -len(suppressed_suffix)]
                sibling = f"{base}{preferred_suffix}"
                if sibling not in expected_topics and sibling not in extra:
                    extra.append(sibling)
    return extra


# ---------------------------------------------------------------------------
# Create Topics
# - Pre-creates expected Kafka topics before dfe-loader starts consuming
# ---------------------------------------------------------------------------
def clean_topics(expected_topics):
    if not (expected_topics):
        return True

    delete_topics = list(expected_topics) + suppressing_topics(expected_topics)
    topics_label = f"{'s' if (len(delete_topics) > 1) else ''}"
    LOGGER.debug(f"Cleaning {len(delete_topics)} 'Kafka' topic{topics_label}...")
    for topic in delete_topics:
        if not (topic):
            continue
        LOGGER.debug(f"Deleting topic: '{topic}'...")
        result = run_cmd(_topic_delete_cmd(topic), capture=True)
        if result.returncode != 0:
            # A suppressing sibling usually does NOT exist, and "not found" is the
            # expected answer for it -- so this stays a debug line rather than a
            # warning that cries wolf on every clean run.
            LOGGER.debug(
                f"Could not delete topic '{topic}' (may not exist): {result.stderr or result.stdout}"
            )
    LOGGER.debug(f"{len(delete_topics)} topic{topics_label} cleaned")
    return True


# ---------------------------------------------------------------------------
# Create Topics
# - Pre-creates expected Kafka topics before dfe-loader starts consuming
# ---------------------------------------------------------------------------
def create_topics(expected_topics):
    if not (expected_topics):
        return True

    LOGGER.debug(
        f"Creating {len(expected_topics)} 'Kafka' topic{'s' if (len(expected_topics) > 1) else ''}..."
    )
    for topic in expected_topics:
        if not (topic):
            continue
        LOGGER.debug(f"Creating topic: '{topic}'...")
        result = run_cmd(_topic_create_cmd(topic), capture=True)
        if result.returncode != 0:
            LOGGER.error(
                f"Failed to create topic '{topic}': {result.stderr or result.stdout}"
            )
            return False
    LOGGER.debug(
        f"{len(expected_topics)} topic{'s' if (len(expected_topics) > 1) else ''} created"
    )
    return True


# ---------------------------------------------------------------------------
# Verify Topics
# - Checks if the expected Kafka topics exist in the Kafka container
# ---------------------------------------------------------------------------
def verify_topics(ctx, test_name, expected_topics):
    LOGGER.info("Verifying 'Kafka' topics...")

    result = run_cmd(_topic_list_cmd(), capture=True)
    topics = _parse_topic_list(result.stdout) if (result.returncode == 0) else []

    for topic in expected_topics:
        if not (topic):
            continue
        if topic in topics:
            mark_pass(ctx, f"[{test_name}] 'Kafka' topic '{topic}' exists")
        else:
            mark_fail(ctx, f"[{test_name}] 'Kafka' topic '{topic}' not found")


# ---------------------------------------------------------------------------
# Archive Targets
# - {archiver service: container directory} for every archiver this test's
#   records reach: it subscribes to the test's landing topic and archives to a
#   file:// destination
# ---------------------------------------------------------------------------
def archive_targets(test, services):
    landing = f"{test.table}_land"
    targets = {}
    for service, config_path in sorted(services.items()):
        if not (service.startswith("dfe-archiver")):
            continue
        with open(PROJECT_DIR / config_path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
        topics = (config.get("kafka") or {}).get("topics") or []
        destination = str((config.get("archive") or {}).get("destination", ""))
        if landing in topics and destination.startswith("file://"):
            targets[service] = destination[len("file://") :]
    return targets


# ---------------------------------------------------------------------------
# Container Files
# - {relative path: size} of every file under a directory in one service's
#   container, copied out through compose so the image needs no tools of its
#   own. Empty when the directory does not exist, None when it cannot be read.
# ---------------------------------------------------------------------------
def container_files(service, directory):
    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "copy"
        result = run_cmd(
            [
                "docker",
                "compose",
                "--profile",
                "*",
                "cp",
                f"{service}:{directory}",
                str(target),
            ],
            capture=True,
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout or ""
            LOGGER.debug(f"Could not copy '{directory}' out of '{service}': {detail}")
            return {} if ("Could not find the file" in detail) else None
        return {
            str(path.relative_to(target)): path.stat().st_size
            for path in target.rglob("*")
            if path.is_file()
        }


# ---------------------------------------------------------------------------
# Verify Archive
# - Each archiver this test's records reach wrote to its archive during the
#   test: a file that is new since the baseline, or one that grew
# ---------------------------------------------------------------------------
def verify_archive(ctx, test_name, baselines):
    for service, (directory, before) in sorted(baselines.items()):
        LOGGER.info(f"Verifying '{service}' archives to '{directory}'...")

        def _written(service=service, directory=directory, before=before):
            after = container_files(service, directory)
            if after is None:
                return None
            return _outage.grown(before, after)

        written = poll_until(
            _written,
            timeout=ARCHIVE_TIMEOUT_SECONDS,
            interval=ARCHIVE_INTERVAL_SECONDS,
            done=bool,
        )
        if written:
            mark_pass(
                ctx,
                f"[{test_name}] '{service}' wrote {len(written)} archive file(s) "
                f"under '{directory}' (first: '{written[0]}')",
            )
        elif written is None:
            mark_fail(
                ctx, f"[{test_name}] '{directory}' could not be read out of '{service}'"
            )
        else:
            mark_fail(
                ctx,
                f"[{test_name}] '{service}' wrote nothing under '{directory}' within "
                f"{ARCHIVE_TIMEOUT_SECONDS}s -- read its log for the write error",
            )


# ---------------------------------------------------------------------------
# Verify HTTP
# - Asserts the user-facing surface answers: the proxy origin, and what it routes
#   to. A complete-stack profile is only proven when the UI and the engine API
#   both answer on ONE origin, which is the whole reason dfe-proxy exists.
# ---------------------------------------------------------------------------
def verify_http(ctx, test_name, checks):
    LOGGER.info("Verifying HTTP surface...")

    for check in checks:
        url = expand_env((check or {}).get("url") or "")
        if not (url):
            continue
        want = int(check.get("status", 200))
        needle = check.get("contains", "")
        try:
            with urlopen(Request(url), timeout=15) as response:
                got = response.status
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            got, body = error.code, ""
        except (URLError, OSError) as error:
            mark_fail(ctx, f"[{test_name}] {url} unreachable: {error}")
            continue

        if got != want:
            mark_fail(ctx, f"[{test_name}] {url} returned {got}, wanted {want}")
        elif needle and needle not in body:
            mark_fail(ctx, f"[{test_name}] {url} is {got} but lacks {needle!r}")
        else:
            mark_pass(ctx, f"[{test_name}] {url} -> {got}")


# ---------------------------------------------------------------------------
# Verify Self-Monitoring
# - Asserts the stack's OWN telemetry reaches ClickHouse, the second of the two
#   pipelines a complete stack has to land. Freshness, not existence: the claim
#   is that it is streaming now.
# ---------------------------------------------------------------------------
def verify_self_monitoring(ctx, test_name):
    database = os.environ.get("DFE_OTEL_DATABASE", "dfe")
    LOGGER.info(f"Verifying self-telemetry in '{database}'...")

    def _fresh():
        return otel_fresh_counts(
            database, OTEL_FRESH_WINDOW_SECONDS, debug=LOGGER.debug
        )

    def _report(attempt, result):
        LOGGER.debug(f"    Attempt {attempt}: {result}")

    counts = poll_until(
        _fresh,
        timeout=OTEL_TIMEOUT_SECONDS,
        interval=OTEL_INTERVAL_SECONDS,
        done=lambda result: result is not None and any(result.values()),
        on_attempt=_report,
    )

    if counts is None:
        mark_fail(
            ctx,
            f"[{test_name}] no '{database}' table readable within "
            f"{OTEL_TIMEOUT_SECONDS:.0f}s - the collector never wrote its schema",
        )
        return
    landed = {table: count for table, count in counts.items() if count}
    if not (landed):
        mark_fail(
            ctx,
            f"[{test_name}] '{database}' has no rows newer than "
            f"{OTEL_FRESH_WINDOW_SECONDS}s - the services are not exporting",
        )
        return
    summary = ", ".join(f"{table}={count}" for table, count in sorted(landed.items()))
    mark_pass(
        ctx, f"[{test_name}] self-telemetry streaming into '{database}' ({summary})"
    )


# ==============================================================================
# Outage Tests
# - One service stopped under steady load, then started again: every other DFE
#   app may refuse work meanwhile, never exit, restart, drop a request, lose an
#   accepted record or stop consuming
# ==============================================================================


# ------------------------------------------------------------------------------
# Container State
# - `docker inspect` of one service's container in this project, or None
# ------------------------------------------------------------------------------
def container_state(service):
    container = container_id(service)
    if not (container):
        return None
    result = run_cmd(["docker", "inspect", container], capture=True)
    if result.returncode != 0:
        return None
    try:
        documents = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return _outage.container_state(documents[0]) if (documents) else None


# ------------------------------------------------------------------------------
# Wait Healthy
# - Polls a service's container until it runs and its healthcheck passes
# ------------------------------------------------------------------------------
def wait_healthy(service, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = container_state(service)
        if state and state.status == "running" and state.health in ("healthy", ""):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


# ------------------------------------------------------------------------------
# Outage Service
# - The compose service an outage names, where `kafka` means whichever broker
#   KAFKA_BACKEND selects
# ------------------------------------------------------------------------------
def outage_service(name):
    return KAFKA_SERVICE if (name == "kafka") else name


# ------------------------------------------------------------------------------
# Compose Lifecycle
# - Runs one lifecycle command (stop, start, kill, pause, unpause) on one service
#   of this project, whichever profile declares it
# ------------------------------------------------------------------------------
def compose_lifecycle(action, service, *flags):
    return run_cmd(
        ["docker", "compose", "--profile", "*", action, *flags, service],
        capture=True,
    )


# ------------------------------------------------------------------------------
# Lifecycle Step
# - One compose lifecycle command, a test failure naming it when it does not
#   succeed, and whether it did
# ------------------------------------------------------------------------------
def lifecycle_step(ctx, test, action, service, *flags):
    result = compose_lifecycle(action, service, *flags)
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    mark_fail(ctx, f"[{test.name}] could not {action} '{service}': {detail}")
    return False


# ------------------------------------------------------------------------------
# Outage Records
# - The records the load cycles through, per `_source`: the test's data file for
#   its own table unless `outage.sources` names data files of its own
# ------------------------------------------------------------------------------
def outage_records(test):
    declared = test.outage.sources or {test.table: test.data_file}
    events = {}
    for source, data_file in declared.items():
        path = PROJECT_DIR / data_file
        if not (path.exists()):
            error(f"Data file '{path}' could not be found", test.name)
        lines = path.read_text(encoding="utf-8").splitlines()
        events[source] = [json.loads(line) for line in lines if line.strip()]
    return _outage.interleave(events)


# ------------------------------------------------------------------------------
# Landed Records
# - {record: rows} for the load's records in the test's table, or None if
#   unreadable
# ------------------------------------------------------------------------------
def landed_records(test):
    return _outage.landed_counts(
        test.database, test.table, test.marker, debug=LOGGER.debug
    )


# ------------------------------------------------------------------------------
# Consumer Group Lag
# - {group: {topic: lag}} from the broker, or None when it cannot say
# ------------------------------------------------------------------------------
def consumer_group_lag():
    if KAFKA_BACKEND == "apache":
        command = _topic_exec_base() + [
            "/opt/kafka/bin/kafka-consumer-groups.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--describe",
            "--all-groups",
        ]
    else:
        command = _topic_exec_base() + [
            "rpk",
            "group",
            "describe",
            "--regex",
            ".*",
            "--format",
            "json",
            "-X",
            f"brokers={KAFKA_BOOTSTRAP}",
        ]
    result = run_cmd(command, capture=True)
    if result.returncode != 0:
        LOGGER.debug(
            f"Consumer group describe failed: {result.stderr or result.stdout}"
        )
        return None
    try:
        if KAFKA_BACKEND == "apache":
            return _outage.group_lag_table(result.stdout or "")
        return _outage.group_lag_json(result.stdout or "")
    except ValueError:
        return None


# ------------------------------------------------------------------------------
# Topic High Watermark
# - The records ever produced to one topic, summed over its partitions, or None
#   when the broker cannot say
# ------------------------------------------------------------------------------
def topic_high_watermark(topic):
    if KAFKA_BACKEND == "apache":
        command = _topic_exec_base() + [
            "/opt/kafka/bin/kafka-get-offsets.sh",
            "--bootstrap-server",
            KAFKA_BOOTSTRAP,
            "--topic",
            topic,
        ]
    else:
        command = _topic_exec_base() + [
            "rpk",
            "topic",
            "describe",
            topic,
            "-p",
            "-X",
            f"brokers={KAFKA_BOOTSTRAP}",
        ]
    result = run_cmd(command, capture=True)
    if result.returncode != 0:
        LOGGER.debug(f"Topic describe failed: {result.stderr or result.stdout}")
        return None
    if KAFKA_BACKEND == "apache":
        return _outage.high_watermark_offsets(result.stdout or "", topic)
    return _outage.high_watermark_text(result.stdout or "")


# ------------------------------------------------------------------------------
# Kafka Unreached
# - Why the load is not yet proven to run through the broker: a landing topic
#   with no record, or a subscriber with no commit. Polled before the broker is
#   stopped, so a path that bypasses Kafka fails as never having reached it
#   rather than later as a consumer left behind.
# ------------------------------------------------------------------------------
def kafka_unreached(test, services):
    expected = _outage.expected_consumers(
        (PROJECT_DIR / path for path in services.values()), test.expected_topics
    )
    landing = [topic for topic in test.expected_topics if topic.endswith("_land")]

    def _unreached():
        lag = consumer_group_lag()
        if lag is None:
            return ["the broker did not describe its consumer groups"]
        watermarks = {topic: topic_high_watermark(topic) for topic in landing}
        return _outage.kafka_unreached(watermarks, lag, expected)

    return poll_until(
        _unreached,
        timeout=OUTAGE_READY_TIMEOUT,
        interval=OUTAGE_POLL_SECONDS,
        done=lambda problems: problems == [],
        on_attempt=report_changes(
            lambda problems: "; ".join(problems) or "load reached Kafka"
        ),
    )


# ------------------------------------------------------------------------------
# Report Changes
# - A poll_until on_attempt callback that logs the rendered state only when it moves
# ------------------------------------------------------------------------------
def report_changes(render):
    previous = None

    def _report(attempt, result):
        nonlocal previous
        line = render(result)
        if line != previous:
            LOGGER.info(f"    Attempt {attempt}: {line}")
            previous = line

    return _report


# ------------------------------------------------------------------------------
# Disruption
# - What one outage did, and when: its start (the pause, else the signal), the
#   signal, the moment every service it touched was healthy again, and the
#   spool as it stood just before the signal
# ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Disruption:
    started_at: float
    signalled_at: float | None
    back_at: float
    spool_at_peak: dict | None = None


# ------------------------------------------------------------------------------
# Take Down
# - Sends the outage service its signal: SIGKILL at once, or a graceful stop.
#   Under a pause, SIGTERM goes out before the thaw so the drain meets the
#   paused service coming back. Returns (taken down, still frozen).
# ------------------------------------------------------------------------------
def take_down(ctx, test, backing, paused):
    if test.outage.signal == "KILL":
        return lifecycle_step(ctx, test, "kill", backing, "-s", "SIGKILL"), bool(paused)
    if not (paused):
        return lifecycle_step(ctx, test, "stop", backing), False
    signalled = lifecycle_step(ctx, test, "kill", backing, "-s", "SIGTERM")
    frozen = not (lifecycle_step(ctx, test, "unpause", paused))
    return signalled and lifecycle_step(ctx, test, "stop", backing), frozen


# ------------------------------------------------------------------------------
# Break And Restore
# - Pauses, signals, thaws and starts again, as the test's plan says, with the
#   thaw and the start always run. Returns the Disruption once every service it
#   touched is healthy again, else None.
# ------------------------------------------------------------------------------
def break_and_restore(ctx, test, load):
    plan = test.outage
    backing = outage_service(plan.service)
    paused = outage_service(plan.pause_service)
    started_at = time.time()
    signalled_at = None
    spool_at_peak = None
    froze = False
    frozen = False
    down = False
    restarted = False
    load.phase = "during"
    try:
        try:
            if paused:
                froze = frozen = lifecycle_step(ctx, test, "pause", paused)
                if froze:
                    LOGGER.info(
                        f"'{paused}' paused, holding it for {plan.pause_seconds}s under load..."
                    )
                    time.sleep(plan.pause_seconds)
            if froze or not (paused):
                if plan.spool_service:
                    spool_at_peak = container_files(plan.spool_service, plan.spool_path)
                if backing:
                    signalled_at = time.time()
                    down, frozen = take_down(
                        ctx, test, backing, paused if frozen else ""
                    )
        finally:
            if frozen:
                frozen = not (lifecycle_step(ctx, test, "unpause", paused))
        if down:
            if plan.signal == "TERM":
                state = container_state(backing)
                LOGGER.info(
                    f"'{backing}' exited with code {state.exit_code if state else 'unknown'} "
                    "after SIGTERM (137 means the stop timeout ran out and SIGKILL ended it)"
                )
            LOGGER.info(
                f"'{backing}' down ({plan.signal}); holding it down for {plan.seconds}s "
                "under load..."
            )
            time.sleep(plan.seconds)
    finally:
        if backing:
            restarted = lifecycle_step(ctx, test, "start", backing)
        load.phase = "after"

    paused_as_planned = not (paused) or (froze and not (frozen))
    downed_as_planned = not (backing) or (down and restarted)
    if not (paused_as_planned and downed_as_planned):
        return None
    for service in (name for name in (backing, paused) if name):
        if not (wait_healthy(service, OUTAGE_RECOVERY_TIMEOUT)):
            mark_fail(
                ctx,
                f"[{test.name}] '{service}' not healthy {OUTAGE_RECOVERY_TIMEOUT}s after "
                "it was brought back",
            )
            return None
    back_at = time.time()
    LOGGER.info(
        f"{' and '.join(f"'{name}'" for name in (backing, paused) if name)} healthy "
        f"again {back_at - started_at:.0f}s after the outage began"
    )
    return Disruption(
        started_at=started_at,
        signalled_at=signalled_at,
        back_at=back_at,
        spool_at_peak=spool_at_peak,
    )


# ------------------------------------------------------------------------------
# Verify Answers
# - Every request gets an HTTP answer: a refusal is backpressure and passes,
#   silence means nothing was there to refuse it. `down` is (signal, healthy
#   again) when the ingress itself was taken down. Silence from a request sent
#   inside it is excused, and from one in flight at the signal only after a
#   kill. A test that expects refusals needs one while the outage ran.
# ------------------------------------------------------------------------------
def verify_answers(ctx, test, sent, down):
    slowest = _outage.slowest_by_phase(sent)
    for phase, counts in _outage.outcomes_by_phase(sent).items():
        answers = ", ".join(f"{outcome} x{n}" for outcome, n in counts.most_common())
        LOGGER.info(
            f"[{test.name}] {phase}: {answers or 'nothing sent'}"
            f" (slowest {slowest.get(phase, 0.0):.1f}s)"
        )

    killed = test.outage.signal == "KILL"
    excused, silent = _outage.unanswered(sent, down, in_flight=killed)
    if excused:
        LOGGER.info(
            f"[{test.name}] {len(excused)} request(s) "
            f"{'open' if killed else 'sent'} while the ingress was down got no "
            "answer, which is allowed: nothing was there to answer them"
        )
    if silent:
        by_phase = _outage.outcomes_by_phase(silent)
        where = "; ".join(
            f"{phase}: {', '.join(f'{o} x{n}' for o, n in counts.items())}"
            for phase, counts in by_phase.items()
            if counts
        )
        mark_fail(
            ctx,
            f"[{test.name}] {len(silent)}/{len(sent)} requests got no HTTP answer ({where})",
        )
    else:
        answered = len(sent) - len(excused)
        refused = sum(
            1 for record in sent if record.status is not None and not (record.accepted)
        )
        mark_pass(
            ctx,
            f"[{test.name}] the receiver answered all {answered} requests it was up "
            f"for ({refused} refused, the rest accepted)",
        )

    if test.outage.expect_refusals:
        refused_during = Counter(
            record.outcome
            for record in sent
            if record.phase == "during"
            and record.status is not None
            and not (record.accepted)
        )
        if refused_during:
            answers = ", ".join(f"{o} x{n}" for o, n in refused_during.most_common())
            mark_pass(
                ctx,
                f"[{test.name}] requests were refused while the outage ran ({answers})",
            )
        else:
            mark_fail(
                ctx,
                f"[{test.name}] no request was refused while the outage ran, so no "
                "answer was held for the frozen destination",
            )


# ------------------------------------------------------------------------------
# Verify Outage Landing
# - Every record for the test's table the receiver accepted, in any phase,
#   reaches that table. Duplicates are counted and reported, never failed. A
#   test that expects loss records the lost count instead of failing on it.
# ------------------------------------------------------------------------------
def verify_outage_landing(ctx, test, sent):
    accepted = _outage.accepted_by_seq(sent, test.table)
    if not (accepted):
        mark_fail(
            ctx, f"[{test.name}] the receiver accepted no record for '{test.table}'"
        )
        return

    def _render(counts):
        got = len(set(accepted) & counts.keys()) if counts is not None else "unreadable"
        return f"{got}/{len(accepted)} accepted records landed"

    counts = poll_until(
        lambda: landed_records(test),
        timeout=OUTAGE_SETTLE_TIMEOUT,
        interval=OUTAGE_POLL_SECONDS,
        done=lambda got: got is not None and set(accepted) <= got.keys(),
        on_attempt=report_changes(_render),
    )
    if counts is None:
        mark_fail(
            ctx,
            f"[{test.name}] no marker readable in '{test.database}.{test.table}', "
            "so landing could not be checked",
        )
        return

    landing = _outage.tally_landing(
        accepted, counts, _outage.phase_by_seq(sent, test.table)
    )
    LOGGER.info(
        f"[{test.name}] landing: {landing.accepted} accepted, {landing.landed} landed, "
        f"{len(landing.missing)} lost, {landing.duplicated} duplicated "
        f"({landing.extra_rows} extra rows, by phase sent: {landing.duplicated_by_phase})"
    )
    table = f"'{test.database}.{test.table}'"
    if not (landing.missing):
        mark_pass(
            ctx,
            f"[{test.name}] all {landing.accepted} accepted records landed in {table} "
            f"(by phase sent: {dict(Counter(accepted.values()))})",
        )
    elif test.outage.expect_loss:
        mark_pass(
            ctx,
            f"[{test.name}] LOSS RECORDED, as this test expects: "
            f"{len(landing.missing)}/{landing.accepted} accepted records never landed "
            f"in {table} within {OUTAGE_SETTLE_TIMEOUT}s "
            f"(by phase sent: {landing.lost_by_phase})",
        )
    else:
        mark_fail(
            ctx,
            f"[{test.name}] {len(landing.missing)}/{landing.accepted} accepted records "
            f"never landed in {table} within {OUTAGE_SETTLE_TIMEOUT}s "
            f"(by phase sent: {landing.lost_by_phase}; first: {landing.missing[:5]})",
        )


# ------------------------------------------------------------------------------
# Verify Spool
# - What the named service wrote under its disk spool, read before the load,
#   mid-outage and at the end: reported, and a failure when the test requires
#   the spool to stay empty
# ------------------------------------------------------------------------------
def verify_spool(ctx, test, before, at_peak):
    plan = test.outage
    after = container_files(plan.spool_service, plan.spool_path)
    where = f"'{plan.spool_path}' in '{plan.spool_service}'"
    readings = {"before the load": before, "mid-outage": at_peak, "at the end": after}
    unread = [label for label, files in readings.items() if files is None]
    if unread:
        mark_fail(
            ctx, f"[{test.name}] could not read the spool {where} {', '.join(unread)}"
        )
        return

    LOGGER.info(
        f"[{test.name}] spool {where}: "
        + "; ".join(
            f"{len(files)} file(s), {sum(files.values())} bytes {label}"
            for label, files in readings.items()
        )
    )
    if not (plan.spool_must_stay_empty):
        return
    written = sorted(
        set(_outage.grown(before, at_peak)) | set(_outage.grown(before, after))
    )
    if written:
        mark_fail(
            ctx,
            f"[{test.name}] records were spooled under {where}, which this test "
            f"requires to stay empty ({', '.join(written[:5])})",
        )
    else:
        mark_pass(ctx, f"[{test.name}] nothing was spooled under {where}")


# ------------------------------------------------------------------------------
# Verify Consumers
# - Every Kafka consumer caught up after the broker came back: each subscribed
#   topic has a committing group per subscriber, and no group on any of the
#   test's topics is behind
# ------------------------------------------------------------------------------
def verify_consumers(ctx, test, services):
    expected = _outage.expected_consumers(
        (PROJECT_DIR / path for path in services.values()), test.expected_topics
    )
    if not (expected):
        mark_fail(ctx, f"[{test.name}] no service in the profile subscribes to a topic")
        return
    watched = set(expected) | set(test.expected_topics)

    def _behind():
        lag = consumer_group_lag()
        if lag is None:
            return None
        return _outage.consumers_behind(lag, expected, test.expected_topics)

    def _render(problems):
        if problems is None:
            return "broker did not answer"
        return "; ".join(problems) or "caught up"

    problems = poll_until(
        _behind,
        timeout=OUTAGE_SETTLE_TIMEOUT,
        interval=OUTAGE_POLL_SECONDS,
        done=lambda got: got == [],
        on_attempt=report_changes(_render),
    )
    if problems == []:
        lag = consumer_group_lag() or {}
        groups = sorted(g for g, topics in lag.items() if set(topics) & watched)
        mark_pass(
            ctx,
            f"[{test.name}] every consumer caught up after the outage ({', '.join(groups)})",
        )
    else:
        mark_fail(
            ctx,
            f"[{test.name}] consumers still behind {OUTAGE_SETTLE_TIMEOUT}s after the "
            f"outage: {'; '.join(problems) if problems else 'broker did not answer'}",
        )


# ------------------------------------------------------------------------------
# Watch Exits
# - Streams die and OOM events for the given containers while the test runs,
#   because the daemon replays only a short window of past events
# ------------------------------------------------------------------------------
def watch_exits(container_ids):
    containers = [f"container={container}" for container in container_ids]
    if not (containers):
        return None
    command = ["docker", "events", "--format", "{{json .}}"]
    command += ["--filter", "event=die", "--filter", "event=oom"]
    for container in containers:
        command += ["--filter", container]
    return subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# ------------------------------------------------------------------------------
# Stop Watching
# - Ends the event stream and returns every exit it saw
# ------------------------------------------------------------------------------
def stop_watching(watcher):
    if watcher is None:
        return []
    watcher.terminate()
    try:
        out, _ = watcher.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        watcher.kill()
        out, _ = watcher.communicate()
    return _outage.exits(out or "")


# ------------------------------------------------------------------------------
# Verify Survivors
# - Each DFE service ran straight through, one process with no die, OOM or
#   restart, and a dead one's log up to its first exit prints with the failure
# ------------------------------------------------------------------------------
def verify_survivors(ctx, test, before, exited, since, began_at):
    for service, earlier in sorted(before.items()):
        died = [(at, label) for cid, at, label in exited if cid == earlier.container_id]
        later = container_state(service)
        changes = (
            _outage.state_changes(earlier, later) if later else ["container is gone"]
        )
        if not (died) and not (changes):
            mark_pass(
                ctx,
                f"[{test.name}] '{service}' ran straight through "
                f"(pid {later.pid}, restarts {later.restarts})",
            )
            continue

        timed = [
            f"{label} {at - began_at:+.0f}s from the outage start"
            if began_at
            else label
            for at, label in died
        ]
        mark_fail(
            ctx,
            f"[{test.name}] '{service}' did not survive: {'; '.join(timed + changes)}",
        )
        window = ["--since", str(int(since))]
        if died:
            window += ["--until", str(died[0][0] + 1)]
        logs = run_cmd(["docker", "logs", *window, earlier.container_id], capture=True)
        log = (logs.stdout or "") + (logs.stderr or "")
        lines = _outage.failure_lines(log, 12) or log.strip().splitlines()[-12:]
        if lines:
            LOGGER.error(
                f"    '{service}' logged, up to its exit:\n" + "\n".join(lines)
            )


# ------------------------------------------------------------------------------
# Drive Outage
# - Steady load, a pre-outage landing gate (plus, on Kafka, proof the load went
#   through the broker), then the outage itself, returning every request sent
#   and the Disruption, None when no outage ran
# ------------------------------------------------------------------------------
def drive_outage(ctx, test, services, ingest_url):
    plan = test.outage
    load = _outage.SteadyLoad(
        url=ingest_url,
        prefix=test.marker,
        records=outage_records(test),
        workers=plan.workers,
        interval=plan.interval,
        timeout=OUTAGE_REQUEST_TIMEOUT,
    )
    on_kafka = KAFKA_BACKEND_PROFILE in test.compose_profiles
    started = time.time()
    disruption = None
    LOGGER.info(
        f"Sending steady load to '{ingest_url}' ({plan.workers} workers, "
        f"{plan.interval}s between requests)..."
    )
    load.start()
    try:
        path_lands = poll_until(
            lambda: landed_records(test),
            timeout=OUTAGE_READY_TIMEOUT,
            interval=OUTAGE_POLL_SECONDS,
            done=bool,
        )
        if path_lands:
            time.sleep(max(0.0, OUTAGE_LEAD_SECONDS - (time.time() - started)))
            unreached = kafka_unreached(test, services) if (on_kafka) else []
            if unreached:
                mark_fail(
                    ctx,
                    f"[{test.name}] the load never reached Kafka, so no outage was "
                    f"run: {'; '.join(unreached)}",
                )
            else:
                disruption = break_and_restore(ctx, test, load)
            if disruption is not None:
                time.sleep(OUTAGE_TAIL_SECONDS)
        else:
            mark_fail(
                ctx,
                f"[{test.name}] nothing landed in '{test.database}.{test.table}' within "
                f"{OUTAGE_READY_TIMEOUT}s of load, so the path was broken before the "
                "outage",
            )
    finally:
        load.stop()
    sent = load.sent()
    elapsed = max(time.time() - started, 1.0)
    LOGGER.info(
        f"[{test.name}] load: {len(sent)} requests in {elapsed:.0f}s, "
        f"{len(sent) / elapsed:.0f} req/s"
    )
    return sent, disruption


# ------------------------------------------------------------------------------
# Run Outage
# - Every service healthy, then the outage under load, then the verdicts:
#   answers, landing, the spool (when named), consumers (Kafka only) and
#   survivors. `ingress` is the service the load posts to.
# ------------------------------------------------------------------------------
def run_outage(ctx, test, services, ingest_url, ingress):
    plan = test.outage
    backing = outage_service(plan.service)
    for service in sorted(test.services):
        if not (wait_healthy(service, OUTAGE_READY_TIMEOUT)):
            mark_fail(
                ctx,
                f"[{test.name}] '{service}' never became healthy, so no outage was run",
            )
            report_unready(service)
            return
    survivors = sorted(service for service in test.services if service != backing)
    before = {service: container_state(service) for service in survivors}
    spool_before = (
        container_files(plan.spool_service, plan.spool_path)
        if (plan.spool_service)
        else None
    )

    since = time.time() - 1
    disruption = None
    watcher = watch_exits(state.container_id for state in before.values())
    try:
        sent, disruption = drive_outage(ctx, test, services, ingest_url)
        ingress_down = (
            (disruption.signalled_at, disruption.back_at)
            if (disruption and disruption.signalled_at and backing == ingress)
            else None
        )
        print()
        verify_answers(ctx, test, sent, ingress_down)
        if disruption is not None:
            print()
            verify_outage_landing(ctx, test, sent)
            if plan.spool_service:
                print()
                verify_spool(ctx, test, spool_before, disruption.spool_at_peak)
            if KAFKA_BACKEND_PROFILE in test.compose_profiles:
                print()
                verify_consumers(ctx, test, services)
    finally:
        exited = stop_watching(watcher)
    print()
    verify_survivors(
        ctx,
        test,
        before,
        exited,
        since,
        disruption.started_at if (disruption) else None,
    )


# ==============================================================================
# Test Functions
# - Functions for resolving test cases from config and running the test flow
# ==============================================================================


# ------------------------------------------------------------------------------
# Resolve Test Case
# - Builds a TestCase object from the config
# ------------------------------------------------------------------------------
def resolve_test_case(test_config, global_config):
    test_name = get_config("name", test_config, global_config, required=True)
    profile_name = get_config("profile", test_config, global_config, required=True)

    compose_profiles, services, extra_services = resolve_services_profile(profile_name)

    # Apply any per-test config overrides
    overrides = test_config.get("config_overrides") or {}
    for svc_name, override_path in overrides.items():
        if svc_name not in services:
            error(
                f"[{test_name}] config_overrides for '{svc_name}' but service not in profile '{profile_name}'"
            )
        services[svc_name] = override_path

    outage = None
    if test_config.get("outage"):
        if not (isinstance(test_config["outage"], dict)):
            error(f"[{test_name}] 'outage' must be a mapping")
        try:
            outage = _outage.outage_plan(
                test_config["outage"],
                OUTAGE_DEFAULT_SECONDS,
                default_workers=OUTAGE_WORKERS,
                default_interval=OUTAGE_INTERVAL_SECONDS,
            )
        except ValueError as problem:
            error(f"[{test_name}] {problem}")
        if outage.spool_service and outage.spool_service not in services:
            error(
                f"[{test_name}] outage 'spool' reads '{outage.spool_service}', which "
                f"profile '{profile_name}' does not run"
            )

    return TestCase(
        name=test_name,
        profile=profile_name,
        compose_profiles=compose_profiles,
        services=services,
        data_file=get_config("data_file", test_config, global_config, required=True),
        database=get_config(
            "database", test_config, global_config, default_value=TARGET_DB
        ).replace("-", "_"),
        table=get_config(
            "table", test_config, global_config, default_value=TARGET_TABLE
        ).replace("-", "_"),
        expected_topics=get_config(
            "expected_topics", test_config, global_config, is_list=True
        ),
        extra_services=extra_services,
        expected_http=get_config(
            "expected_http", test_config, global_config, is_list=True
        ),
        marker=f"{RUN_ID}-{test_name}",
        outage=outage,
    )


# ------------------------------------------------------------------------------
# Run Test
# - Executes the test flow for a given TestCase
# ------------------------------------------------------------------------------
def run_test(ctx, mode, test, persistent_services):
    # Ensure configuration files exist for services used by this test
    for svc_name, config_path in test.services.items():
        if not ((PROJECT_DIR / config_path).exists()):
            error(
                f"[{test.name}] Config for '{svc_name}' not found: '{PROJECT_DIR / config_path}'"
            )

    # Print header with test details and configuration for visibility
    print()
    print("------------------------------------------------------------")
    print(f"E2E Test: {test.name}")
    print(f"  - Profile: {test.profile}")
    print(f"  - Data File: {test.data_file}")
    if test.outage:
        plan = test.outage
        if plan.pause_service:
            print(f"  - Pause: '{plan.pause_service}' frozen for {plan.pause_seconds}s")
        if plan.service:
            print(f"  - Outage: '{plan.service}' {plan.signal} for {plan.seconds}s")
        print(f"  - Load: {plan.workers} workers, {plan.interval}s between requests")
        if plan.expect_loss:
            print("  - Expects loss: lost records are recorded, not failed")
    for svc_name in sorted(test.services):
        print(f"  - {svc_name} Config: {test.services[svc_name]}")
    if test.database != RUN_ID.replace("-", "_"):
        print(f"  - ClickHouse Database: {test.database}")
    if test.table != test.name.replace("-", "_"):
        print(f"  - ClickHouse Table: {test.table}")
    print("------------------------------------------------------------")

    # Generate a per-run loader config with the test database and table
    effective_services = dict(test.services)
    if "dfe-loader" in effective_services:
        effective_services["dfe-loader"] = generate_tmp_loader_config(
            effective_services["dfe-loader"], test.database, test.table
        )

    # Start the stack (Kafka profiles: infra first, create topics, then full stack)
    if not (stack_up(mode, test, effective_services)):
        mark_fail(ctx, f"[{test.name}] Stack failed to start")
        stack_down(keep_services=persistent_services or None)
        return

    # Wait for the stack to be healthy before proceeding
    if not (wait_for_stack(test.services)):
        mark_fail(ctx, f"[{test.name}] Stack failed to reach healthy state")
        dump_logs()
        stack_down(keep_services=persistent_services or None)
        return

    # A service on another config tests another path, so nothing after it counts.
    print()
    if not (verify_config_mounts(ctx, test)):
        dump_logs()
        stack_down(keep_services=persistent_services or None)
        return

    has_fetcher = "dfe-fetcher" in test.services
    has_receiver = "dfe-receiver" in test.services
    ingest_url = (
        f"{DFE_FETCHER_INGEST_URL}/{test.table}"
        if (has_fetcher and not (has_receiver))
        else DFE_RECEIVER_INGEST_URL
    )
    if test.outage:
        ingress = "dfe-receiver" if (has_receiver) else "dfe-fetcher"
        run_outage(ctx, test, effective_services, ingest_url, ingress)
    else:
        # Baseline the landing table before send so verification measures the delta
        # this run contributes (the engine-provisioned table is shared, not per-run).
        baseline = ch_count(test.database, test.table)
        # The archive volume outlives the stack, so an archiver is judged on what
        # it writes after this baseline, never on files an earlier run left.
        archive_baselines = {
            service: (directory, container_files(service, directory) or {})
            for service, directory in archive_targets(test, effective_services).items()
        }
        send_events(
            ctx,
            test.name,
            test.marker,
            test.data_file,
            test.database,
            test.table,
            ingest_url,
        )

        # Verify the events landed in ClickHouse (row-count delta from the baseline)
        print()
        verify_table(
            ctx,
            test.name,
            test.database,
            test.table,
            baseline,
            ctx.total_sent,
            test.marker,
        )

        if archive_baselines:
            print()
            verify_archive(ctx, test.name, archive_baselines)

    # If the test runs Kafka and expected topics are defined, verify the topics exist
    if KAFKA_BACKEND_PROFILE in test.compose_profiles and test.expected_topics:
        print()
        verify_topics(ctx, test.name, test.expected_topics)

    if test.expected_http:
        print()
        verify_http(ctx, test.name, test.expected_http)

    if OTEL_SERVICE in test.extra_services:
        print()
        verify_self_monitoring(ctx, test.name)

    # Dump container logs for debugging purposes, then tear down the stack if configured to do so
    dump_logs()
    print()
    if stack_is_up():
        stack_down(keep_services=persistent_services or None)

    # Print footer for test separation in logs
    print("------------------------------------------------------------")


# ==============================================================================
# Miscellaneous Functions
# ==============================================================================


# ------------------------------------------------------------------------------
# Cleanup
# - Removes any temporary files and databases created during the test run
# ------------------------------------------------------------------------------
def cleanup(clickhouse_config):
    if TMP_DIR.exists():
        for path in TMP_DIR.iterdir():
            # rmtree for directories: `.tmp/` is shared, and a cached Vector
            # binary lives in `.tmp/vector/`. A bare unlink() raises
            # IsADirectoryError here, which runs AFTER every test and would
            # destroy the results summary and exit code of a complete run.
            rmtree(path) if path.is_dir() else path.unlink()
        TMP_DIR.rmdir()
    if clickhouse_config.get("drop_database", False):
        ch_query(f"DROP DATABASE IF EXISTS {RUN_ID.replace('-', '_')}")


# ------------------------------------------------------------------------------
# Error
# - Logs an error message and exits with the specified code
# ------------------------------------------------------------------------------
def error(msg, test_name="", code=1, level=50):
    LOGGER.log(level, f"{f'[{test_name}] ' if (test_name) else ''}{msg}")
    sys.exit(code)


# ------------------------------------------------------------------------------
# Require Command
# - Identifies if a command is in the system PATH and exits with an error if not
# ------------------------------------------------------------------------------
def require_command(name):
    if which(name) is None:
        error(f"'{name}' not found. Please install it first")


# ------------------------------------------------------------------------------
# Setup Logging
# - Configures the logging system with custom levels and formatting
# ------------------------------------------------------------------------------
def setup_logging():
    logging.addLevelName(SKIP_LEVEL, "SKIP")
    logging.addLevelName(PASS_LEVEL, "PASS")
    logging.addLevelName(FAIL_LEVEL, "FAIL")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColouredFormatter("%(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


# ==============================================================================
# Main
# ==============================================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Config-driven e2e test runner for the DFE Docker stack"
    )
    parser.add_argument(
        "tests",
        nargs="*",
        help="test names to run; every test of the selected kind when omitted",
    )
    parser.add_argument(
        "--outages",
        action="store_true",
        help="run the outage tests, which stop a service under load, instead of the default suite",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Initial setup with logging, test context and start_time
    setup_logging()
    ctx = TestContext()
    start_time = datetime.now().astimezone()

    # Print header with test suite information and config details
    print("============= DFE Docker End-to-End Test Suite =============")
    print()
    print(f"Config:     {TEST_CONFIG}")
    print(f"Run ID:     {RUN_ID}")
    print(f"Log level:  {LOG_LEVEL}")
    print(f"Start Time: {start_time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    print()
    print("============================================================")

    # Ensure required commands are available. Not curl -- this harness speaks HTTP
    # through urllib and has not shelled out to curl for some time.
    require_command("docker")

    # Confirm test configuration file exists and load it
    if not (TEST_CONFIG.exists()):
        error(f"Test configuration file '{TEST_CONFIG}' could not be found")
    config = load_config(TEST_CONFIG)

    # Build global_config from the config sections (global + clickhouse) so that
    # get_config can resolve dotted paths like "clickhouse.drop_database"
    global_config = config.get("global", {}).copy()
    global_config["clickhouse"] = config.get("clickhouse", {})

    # Tear down any existing stack before starting
    persistent_services = get_config("persistent_services", {}, global_config)
    if stack_is_up():
        print()
        stack_down(persistent_services)

    mode = get_config("mode", {}, global_config)

    # Resolve which tests to run - CLI args filter, otherwise every test of the
    # selected kind. Outage tests stop services, so they run only on --outages.
    configured = config.get("tests", [])
    other_target = "make test-e2e" if (args.outages) else "make test-resilience"

    tests_to_run = []
    if args.tests:
        by_name = {test_config.get("name"): test_config for test_config in configured}
        for test_name in args.tests:
            test_config = by_name.get(test_name)
            if test_config is None:
                mark_skip(
                    ctx,
                    f"Test name '{test_name}' could not be found in config '{TEST_CONFIG}'",
                )
            elif bool(test_config.get("outage")) != args.outages:
                mark_skip(
                    ctx, f"Test '{test_name}' runs under '{other_target}', not this one"
                )
            else:
                tests_to_run.append(resolve_test_case(test_config, global_config))
    else:
        for test_config in configured:
            if bool(test_config.get("outage")) == args.outages:
                tests_to_run.append(resolve_test_case(test_config, global_config))

    # Build only the DFE services the selected tests actually need.
    all_services = set()
    for test in tests_to_run:
        all_services.update(test.services.keys())

    build_images(mode, all_services)

    # Run resolved tests
    for test in tests_to_run:
        run_test(ctx, mode, test, persistent_services)

    # Post test execution cleanup
    cleanup(global_config.get("clickhouse", {}))

    # Capture end time for test suite completion and calculate duration
    end_time = datetime.now().astimezone()
    duration = end_time - start_time

    # Print footer with test results summary and end time
    print()
    print("========================== Results =========================")
    print()
    print(f"Passed:     {ctx.passed}")
    print(f"Failed:     {ctx.failed}")
    print(f"Skipped:    {ctx.skipped}")
    print(f"Total:      {ctx.passed + ctx.failed + ctx.skipped}")
    print(f"End Time:   {end_time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    print(
        f"Duration:   {int(duration.total_seconds()) // 60}m {int(duration.total_seconds()) % 60}s"
    )
    print()
    print("============================================================")

    # A SKIP is not a pass. Both things that skip here mean the suite did not
    # check what it claims to: a marker column it could not locate (so the run
    # silently falls back to the weak row-count delta the marker exists to
    # replace), or a test name that does not exist in the config (so the thing
    # you asked to run never ran). Exiting 0 on either lets CI go green over an
    # assertion that was never made.
    if ctx.failed > 0 or ctx.skipped > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
