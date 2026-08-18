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
#    TEST_CONFIG=tests/e2e/e2e-tests.yaml ./scripts/test-e2e.py  # Custom config
#    LOG_LEVEL=debug ./scripts/test-e2e.py                       # Verbose service logs

import json
import logging
import os
import subprocess
import sys
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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree, which
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _common import FALSY, _load_dotenv
from _pipeline import MARKER_EXPRESSIONS  # noqa: F401 - re-exported for callers
from _pipeline import ch_marker_count as _ch_marker_count
from _pipeline import otel_fresh_counts, poll_until


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
    "dfe-transform-vector": "/etc/dfe-transform-vector/config.yaml",
    "dfe-transform-vrl": "/etc/dfe-transform-vrl/config.yaml",
}

KNOWN_DFE_SERVICES = set(SERVICE_CONFIG_MOUNTS.keys())

# ------------------------------------------------------------------------------
# Schema Authority
# - dfe-engine is brought up as INFRA, health-gated before the DFE services,
#   because the loader pre-warms the schemas it registers. The harness sends with
#   _source = the target table, so rows land in the engine-provisioned default
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
TARGET_DB = "dfe"
TARGET_TABLE = "default"

# Marker lookup lives in _pipeline, shared with the power-on self test.


# ------------------------------------------------------------------------------
# Load Dotenv
# - Shared with init/stack/resolve_profile via _common. This file used to carry a
#   third private copy of the same parser, and three copies of "how do we read
#   .env" is three chances to disagree with docker compose about it.
# ------------------------------------------------------------------------------
_load_dotenv()

# ------------------------------------------------------------------------------
# Endpoint Related
# - Configurable URLs for services, with appropriate defaults
# ------------------------------------------------------------------------------
CLICKHOUSE_URL = os.environ.get(
    "CLICKHOUSE_URL",
    f"http://localhost:{os.environ.get('CLICKHOUSE_HTTP_PORT', '8123')}",
)
CLICKHOUSE_USERNAME = os.environ.get("CLICKHOUSE_USERNAME", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
# Readiness, not liveness -- the harness needs "usable", not "the process exists".
# Every service uses /readyz, matching the compose healthchecks. The
# /health/live|ready|startup aliases are gone from the scalo-py in the pinned
# dfe-engine and 404, which reads as an engine that never comes ready.
DFE_LOADER_HEALTH_URL = os.environ.get(
    "DFE_LOADER_HEALTH_URL",
    f"http://localhost:{os.environ.get('DFE_LOADER_PROMETHEUS_PORT', '9091')}/readyz",
)
DFE_RECEIVER_HEALTH_URL = os.environ.get(
    "DFE_RECEIVER_HEALTH_URL",
    f"http://localhost:{os.environ.get('DFE_RECEIVER_PROMETHEUS_PORT', '9090')}/readyz",
)
DFE_ARCHIVER_HEALTH_URL = os.environ.get(
    "DFE_ARCHIVER_HEALTH_URL",
    f"http://localhost:{os.environ.get('DFE_ARCHIVER_PROMETHEUS_PORT', '9093')}/readyz",
)
DFE_FETCHER_HEALTH_URL = os.environ.get(
    "DFE_FETCHER_HEALTH_URL",
    f"http://localhost:{os.environ.get('DFE_FETCHER_PROMETHEUS_PORT', '9094')}/readyz",
)
DFE_ENGINE_HEALTH_URL = os.environ.get(
    "DFE_ENGINE_HEALTH_URL",
    f"http://localhost:{os.environ.get('DFE_ENGINE_PORT', '8003')}/readyz",
)
DFE_FETCHER_INGEST_URL = os.environ.get(
    "DFE_FETCHER_INGEST_URL",
    f"http://localhost:{os.environ.get('DFE_FETCHER_INGEST_PORT', '8082')}/ingest",
)
DFE_RECEIVER_INGEST_URL = os.environ.get(
    "DFE_RECEIVER_INGEST_URL",
    f"http://localhost:{os.environ.get('DFE_RECEIVER_HTTP_PORT', '8080')}/ingest",
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

    compose_files += ["-f", override_file]

    # Schema authority: dfe-engine provisions dfe.default and registers the schemas
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
# Report Unready Service
# - Dumps what a timed-out service was actually doing, before teardown removes it
#
#   A bare timeout cannot distinguish the three states it might have been in: no
#   container, a container whose port is published but not yet listening, or a
#   process that is running and not answering. They call for different fixes, and
#   the container is gone by the time anyone reads the failure.
# ------------------------------------------------------------------------------
def report_unready(name):
    inspect = run_cmd(
        [
            "docker",
            "inspect",
            "-f",
            "status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}",
            name,
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
            name,
        ],
        capture=True,
    )
    if (health.stdout or "").strip():
        LOGGER.error(f"    '{name}' healthcheck log: {health.stdout.strip()[:400]}")

    logs = run_cmd(["docker", "logs", "--tail", "20", name], capture=True)
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
# Verify HTTP
# - Asserts the user-facing surface answers: the proxy origin, and what it routes
#   to. A complete-stack profile is only proven when the UI and the engine API
#   both answer on ONE origin, which is the whole reason dfe-proxy exists.
# ---------------------------------------------------------------------------
def verify_http(ctx, test_name, checks):
    LOGGER.info("Verifying HTTP surface...")

    for check in checks:
        url = (check or {}).get("url")
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
    database = os.environ.get("DFE_OTEL_DATABASE", "otel")
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

    has_fetcher = "dfe-fetcher" in test.services
    has_receiver = "dfe-receiver" in test.services
    ingest_url = (
        f"{DFE_FETCHER_INGEST_URL}/{test.table}"
        if (has_fetcher and not (has_receiver))
        else DFE_RECEIVER_INGEST_URL
    )
    # Baseline the landing table before send so verification measures the delta
    # this run contributes (the engine-provisioned table is shared, not per-run).
    baseline = ch_count(test.database, test.table)
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
        ctx, test.name, test.database, test.table, baseline, ctx.total_sent, test.marker
    )

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
    logging.addLevelName(PASS_LEVEL, "PASS")
    logging.addLevelName(FAIL_LEVEL, "FAIL")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColouredFormatter("%(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))


# ==============================================================================
# Main
# ==============================================================================


def main():
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

    # Resolve which tests to run - CLI args filter, otherwise run all
    test_count = len(config.get("tests", []))
    test_names = sys.argv[1:]

    tests_to_run = []
    if test_names:
        for test_name in test_names:
            exists = False
            for index in range(test_count):
                if config["tests"][index].get("name") == test_name:
                    tests_to_run.append(
                        resolve_test_case(config["tests"][index], global_config)
                    )
                    exists = True
                    break
            if not (exists):
                mark_skip(
                    ctx,
                    f"Test name '{test_name}' could not be found in config '{TEST_CONFIG}'",
                )
    else:
        for index in range(test_count):
            tests_to_run.append(
                resolve_test_case(config["tests"][index], global_config)
            )

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
