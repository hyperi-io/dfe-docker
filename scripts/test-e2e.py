#!/usr/bin/env python3

#  Project:   dfe-docker
#  File:      scripts/test-e2e.py
#  Purpose:   Config-driven e2e test runner for the DFE Docker stack
#  Language:  Python
#
#  License:   FSL-1.1-ALv2
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

import yaml

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from urllib.error import URLError
from urllib.request import Request, urlopen


# ==============================================================================
# Global Variables
# ==============================================================================

# ------------------------------------------------------------------------------
# Project Related
# - Paths and identifiers for the project and test run
# ------------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
RUN_ID = f"e2e-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}"
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
    "profiles": ["full"],
    "clickhouse": {
        "drop_database": True,
        "table_ddl_file_path": "clickhouse/base_table_ddl.sql"
    }
}

# ------------------------------------------------------------------------------
# Load Dotenv Function
# - Loads a .env file into os.environ (existing env vars take precedence)
# ------------------------------------------------------------------------------
def load_dotenv(file_path):
    try:
        with open(file_path) as file:
            for line in file:
                line = line.strip()
                if (not(line) or line.startswith("#")):
                    continue
                key, separator, value = line.partition("=")
                if not(separator):
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), value)
    except FileNotFoundError:
        LOGGER.info(f"No '.env' file found at '{file_path}'. Skipping dotenv loading")
        pass

load_dotenv(PROJECT_DIR / ".env")

# ------------------------------------------------------------------------------
# Endpoint Related
# - Configurable URLs for services, with appropriate defaults
# ------------------------------------------------------------------------------
CLICKHOUSE_URL = os.environ.get(
    "CLICKHOUSE_URL",
    f"http://localhost:{os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")}",
)
CLICKHOUSE_USERNAME = os.environ.get("CLICKHOUSE_USERNAME", "default")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
DFE_LOADER_HEALTH_URL = os.environ.get(
    "DFE_LOADER_HEALTH_URL",
    f"http://localhost:{os.environ.get("DFE_LOADER_PROMETHEUS_PORT", "9091")}/health/ready",
)
DFE_RECEIVER_HEALTH_URL = os.environ.get(
    "DFE_RECEIVER_HEALTH_URL",
    f"http://localhost:{os.environ.get("DFE_RECEIVER_PROMETHEUS_PORT", "9090")}/health/ready",
)
DFE_ARCHIVER_HEALTH_URL = os.environ.get(
    "DFE_ARCHIVER_HEALTH_URL",
    f"http://localhost:{os.environ.get("DFE_ARCHIVER_PROMETHEUS_PORT", "9093")}/health/ready",
)
DFE_RECEIVER_INGEST_URL = os.environ.get(
    "DFE_RECEIVER_INGEST_URL",
    f"http://localhost:{os.environ.get("DFE_RECEIVER_HTTP_PORT", "8080")}/ingest",
)
TEST_CONFIG = Path(os.environ.get(
    "TEST_CONFIG",
    str(PROJECT_DIR / "tests" / "e2e" / "e2e-tests.yaml")
))

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
# ------------------------------------------------------------------------------
@dataclass
class TestCase:
    name: str
    profiles: list[str]
    data_file: str
    database: str
    table: str
    marker: str
    ch_drop_database: str
    ch_table_ddl_file_path: str
    archiver_config: str | None = None
    loader_config: str | None = None
    receiver_config: str | None = None
    persistent_services: list[str] = field(default_factory = list)
    expected_topics: list[str] = field(default_factory = list)

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
        if (sys.stderr.isatty()):
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
def http_get(url, timeout = 5):
    request = Request(url, method = "GET")
    with urlopen(request, timeout = timeout) as response:
        return response.read().decode()

# ------------------------------------------------------------------------------
# HTTP Post
# - Helper to perform a HTTP POST request with set body and content type
# ------------------------------------------------------------------------------
def http_post(url, body, content_type = "application/json", timeout = 10):
    data = body.encode() if (isinstance(body, str)) else body
    request = Request(url, data = data, method = "POST")
    request.add_header("Content-Type", content_type)
    try:
        with urlopen(request, timeout = timeout) as response:
            return response.status
    except URLError as e:
        if (hasattr(e, "code")):
            return e.code
        raise

# ------------------------------------------------------------------------------
# ClickHouse Query
# - Execute a SQL query against ClickHouse via HTTP and return the result
# ------------------------------------------------------------------------------
def ch_query(sql):
    LOGGER.debug(f"Executing ClickHouse query: `{sql}`")
    data = sql.encode()
    request = Request(CLICKHOUSE_URL, data = data, method = "POST")
    request.add_header("X-ClickHouse-User", CLICKHOUSE_USERNAME)
    request.add_header("X-ClickHouse-Key", CLICKHOUSE_PASSWORD)
    try:
        with urlopen(request, timeout = 10) as response:
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
    with open(path) as config_file:
        return yaml.safe_load(config_file)

# ------------------------------------------------------------------------------
# Get Config
# - Resolves a config value: test_config → global_config → DEFAULTS
#   Each tier is searched independently — no merging or combining
# ------------------------------------------------------------------------------
def get_config(path, test_config, global_config, default_value = None, required = False, is_list = False):
    keys = path.split(".") if ("." in path) else [path]
    for config in [test_config, global_config, DEFAULTS]:
        value = config
        for key in keys:
            if (isinstance(value, dict) and key in value):
                value = value[key]
            else:
                value = None
                break
        if (value is not None):
            if (is_list):
                return value if (isinstance(value, list)) else []
            return value
    if (default_value is not None):
        return default_value
    if (required):
        error(f"Missing required config field '{path}'")
    return [] if (is_list) else None

# ------------------------------------------------------------------------------
# Generate Temporary Loader Config
# - Generates a temporary loader config file with the tests database and table
# ------------------------------------------------------------------------------
def generate_tmp_loader_config(loader_config, database, table):
    TMP_DIR.mkdir(parents = True, exist_ok = True)

    with open(PROJECT_DIR / loader_config) as config_file:
        config = yaml.safe_load(config_file)

    config.setdefault("clickhouse", {})["database"] = database
    config.setdefault("routing", {})["default_db"] = database
    config.setdefault("routing", {})["default_table"] = table

    generated_path = TMP_DIR / f"loader-{RUN_ID}-{table}.yaml"
    with open(generated_path, "w") as generated_file:
        yaml.safe_dump(config, generated_file, default_flow_style = False)

    LOGGER.debug(f"Generated loader config: '{generated_path}'")
    return str(generated_path.relative_to(PROJECT_DIR))


# ==============================================================================
# Docker Compose Parsing/Setup
# - Extracts service profiles from the docker-compose.yml for dynamic test setup
# ==============================================================================

# ------------------------------------------------------------------------------
# Parse Compose Profiles
# - Reads docker-compose.yml and builds a mapping of profiles to services
# ------------------------------------------------------------------------------
def parse_compose_profiles():
    compose_path = PROJECT_DIR / "docker-compose.yml"
    with open(compose_path) as compose_file:
        compose_data = yaml.safe_load(compose_file)

    profile_map = {}
    for service_name, service_data in compose_data.get("services", {}).items():
        for profile in service_data.get("profiles", []):
            profile_map.setdefault(profile, []).append(service_name)
    return profile_map

# ------------------------------------------------------------------------------
# Services for Profiles
# - Returns the deduplicated list of services associated with the given profiles
# ------------------------------------------------------------------------------
def services_for_profiles(profiles):
    profile_map = parse_compose_profiles()
    services = []
    seen = set()
    for profile in profiles:
        if (profile not in profile_map):
            error(f"Unknown profile: '{profile}'")
        for service in profile_map[profile]:
            if (service not in seen):
                seen.add(service)
                services.append(service)
    return services

# ------------------------------------------------------------------------------
# Has Service
# - Checks if a given service type is present in the resolved services for profiles
# ------------------------------------------------------------------------------
def has_service(profiles, prefix):
    return any(service.startswith(prefix) for service in services_for_profiles(profiles))

# ------------------------------------------------------------------------------
# Generate Compose Override
# - Creates a temp docker-compose override file to mount test-specific configs
# ------------------------------------------------------------------------------
def generate_compose_override(profiles, archiver_config, receiver_config, loader_config):
    services = services_for_profiles(profiles)

    TMP_DIR.mkdir(parents = True, exist_ok = True)
    override_file = TMP_DIR / f"docker-compose.{RUN_ID}.e2e.yaml"

    content = "services:\n"

    if (archiver_config):
        archiver_service = next(service for service in services if (service.startswith("dfe-archiver")))
        content += (
            f"  {archiver_service}:\n"
            f"    volumes:\n"
            f"      - ./{archiver_config}:/etc/dfe-archiver/config.yaml:ro\n"
        )

    if (receiver_config):
        receiver_service = next(service for service in services if (service.startswith("dfe-receiver")))
        content += (
            f"  {receiver_service}:\n"
            f"    volumes:\n"
            f"      - ./{receiver_config}:/etc/dfe-receiver/config.yaml:ro\n"
        )

    if (loader_config):
        loader_service = next(service for service in services if (service.startswith("dfe-loader")))
        content += (
            f"  {loader_service}:\n"
            f"    volumes:\n"
            f"      - ./{loader_config}:/etc/dfe/loader.yaml:ro\n"
        )

    override_file.write_text(content)
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
    if not(text):
        return ""
    output_lines = []

    for line in text.splitlines():
        stripped_line = line.strip()
        if not(stripped_line):
            continue

        # Step Headers: "#N [service stage N/M] CMD"
        if (stripped_line.startswith("#") and " [" in stripped_line and "]" in stripped_line):
            output_lines.append(line)
        # Completion: "DONE", "CACHED", "ERROR"
        elif (("DONE" in stripped_line) or ("CACHED" in stripped_line) or ("ERROR" in stripped_line)):
            output_lines.append(line)
        # curl/wget Failures
        elif (("curl:" in stripped_line) or ("wget:" in stripped_line)):
            output_lines.append(line)
        # Exit Codes from Failed Commands
        elif ((stripped_line.startswith("ERROR:")) or ("process" in stripped_line and "did not complete successfully" in stripped_line)):
            output_lines.append(line)
    return "\n".join(output_lines)

# ------------------------------------------------------------------------------
# Build Images
# - Builds DFE service images once on startup
# ------------------------------------------------------------------------------
def build_images(mode, profiles):
    services = set()
    services.update(services_for_profiles(profiles))

    # Deduplicate to base image names as multiple compose services share the same
    image_to_service = {}
    for service in sorted(service for service in services if service.startswith("dfe-")):
        base_name = service.split("-kafka")[0].split("-bare-bones")[0].split("-debug")[0]
        if (base_name not in image_to_service):
            image_to_service[base_name] = service
    dfe_services = list(image_to_service.values())

    if not(dfe_services):
        LOGGER.info("No DFE services to build")
        return

    image_names = sorted(image_to_service.keys())
    if (len(image_names) > 1):
        services_str = f"'{"', '".join(image_names[:-1])}' and '{image_names[-1]}'"
    else:
        services_str = f"'{image_names[0]}'"
    LOGGER.info(f"Building images for {services_str}...")

    compose_files = ["-f", "docker-compose.yml"]
    if (mode == "dev" and (PROJECT_DIR / "docker-compose.override.yml").exists()):
        compose_files += ["-f", "docker-compose.override.yml"]

    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]

    base_cmd = ["docker", "compose"] + compose_files + profile_flags
    build_args = base_cmd + ["build"] + dfe_services
    if (mode == "ci"):
        build_args += ["--no-cache", "--pull"]

    build_result = run_cmd(build_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    if (build_result.returncode != 0):
        filtered = filter_build_output(build_result.stderr or build_result.stdout or "")
        error(f"Docker image build failed: {filtered if (filtered) else (build_result.stderr or build_result.stdout)}")

# ------------------------------------------------------------------------------
# Run Command
# - Runs a subprocess command with logging and error handling
# ------------------------------------------------------------------------------
def run_cmd(args, check = True, capture = False, cwd = None):
    LOGGER.debug(f"Executing command: `$ {' '.join(args)}`...")
    kwargs = {"cwd": cwd or PROJECT_DIR}
    if (capture):
        kwargs["capture_output"] = True
        kwargs["text"] = True
    result = subprocess.run(args, **kwargs)
    if (check and result.returncode != 0):
        return result
    return result

# ------------------------------------------------------------------------------
# Stack Up
# - Starts the Docker stack with the appropriate compose files and profile
# ------------------------------------------------------------------------------
def stack_up(mode, profiles, archiver_config, receiver_config, loader_config, expected_topics = None):
    LOGGER.info("Starting stack...")
    LOGGER.debug(f"Services for profiles '{profiles}':")
    for service in services_for_profiles(profiles):
        LOGGER.debug(f"  - {service}")

    override_file = generate_compose_override(profiles, archiver_config, receiver_config, loader_config)

    compose_files = ["-f", "docker-compose.yml"]
    if (mode not in MODES):
        error(f"Unknown mode: '{mode}'. Supported modes: '{MODES}'")
    elif (mode == "dev" and (PROJECT_DIR / "docker-compose.override.yml").exists()):
        compose_files += ["-f", "docker-compose.override.yml"]

    compose_files += ["-f", override_file]

    # Kafka profiles: bring up infra first, create topics, then start the full profile.
    # The loader fails on startup if no matching topics exist on the broker.
    has_kafka = any("kafka" in profile for profile in profiles)
    if (has_kafka and expected_topics):
        infra_cmd = ["docker", "compose"] + compose_files + ["--profile", "infra"]
        infra_up = infra_cmd + ["up", "-d"]

        LOGGER.info("Preparing 'Kafka' service...")
        infra_result = run_cmd(infra_up, check = False, capture = not(LOG_LEVEL == "DEBUG"))
        if (infra_result.returncode != 0):
            filtered = filter_build_output(infra_result.stderr or infra_result.stdout or "")
            LOGGER.error(f"'Kafka' infrastructure failed to start: {filtered if (filtered) else (infra_result.stderr or infra_result.stdout)}")
            return False

        LOGGER.debug("Waiting for 'Kafka' to be healthy...")
        wait_cmd = ["docker", "compose"] + compose_files + ["--profile", "infra", "up", "--wait", "--wait-timeout", "60", "-d"]
        wait_result = run_cmd(wait_cmd, check = False, capture = not(LOG_LEVEL == "DEBUG"))
        if (wait_result.returncode != 0):
            LOGGER.error("'Kafka' broker not healthy within timeout")
            return False
        LOGGER.debug("'Kafka' is healthy")

        if not(create_topics(expected_topics)):
            return False

    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]
    base_cmd = ["docker", "compose"] + compose_files + profile_flags
    up_args = base_cmd + ["up", "-d"]

    up_result = run_cmd(up_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    if (up_result.returncode != 0):
        filtered = filter_build_output(up_result.stderr or up_result.stdout or "")
        LOGGER.error(f"Docker compose up failed: {filtered if (filtered) else (up_result.stderr or up_result.stdout)}")
        return False
    return True

# ------------------------------------------------------------------------------
# Stack Is Up
# - Checks if the stack is up
# ------------------------------------------------------------------------------
def stack_is_up():
    base_cmd = ["docker", "compose", "ps"]
    result = run_cmd(base_cmd, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    if (result.stdout is None):
        return (result.returncode == 0)
    return (result.returncode == 0 and "Up" in result.stdout)

# ------------------------------------------------------------------------------
# Stack Down
# - Stops the Docker stack
# ------------------------------------------------------------------------------
def stack_down(keep_services = None):
    base_cmd = ["docker", "compose"]

    profiles = parse_compose_profiles()
    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]

    if (keep_services):
        all_services = set()
        for services in profiles.values():
            all_services.update(services)
        stop_services = [service for service in all_services if (service not in keep_services)]

        if not(stop_services):
            LOGGER.info("All running services are in persistent_services, skipping teardown")
            return
        
        if (len(keep_services) > 1):
            keep_services_str = f"{", ".join(keep_services[:-1])} and {keep_services[-1]}"
        else:
            keep_services_str = keep_services[0]
        LOGGER.info(f"Stopping services (keeping {keep_services_str})...")
        stop_args = base_cmd + profile_flags + ["stop"] + stop_services
        run_cmd(stop_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
        rm_args = base_cmd + profile_flags + ["rm", "-f"] + stop_services
        run_cmd(rm_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    else:
        LOGGER.info("Stopping stack...")
        down_args = base_cmd + profile_flags + ["down", "--remove-orphans"]
        run_cmd(down_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))

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
    
    if (LOG_LEVEL == "DEBUG"):
        print("--------------- CONTAINER LOGS (50 TAIL LOGS) --------------")
        run_cmd(logs_args, check = False, capture = (not(LOG_LEVEL == "DEBUG")))
        print("------------------------- END LOGS -------------------------")

# ------------------------------------------------------------------------------
# Wait For Service
# - Waits for a service to become healthy by polling its endpoint
# ------------------------------------------------------------------------------
def wait_for_service(name, url, max_attempts = 30):
    LOGGER.info(f"Waiting for '{name}' at '{url}'...")
    time.sleep(2)
    for attempt in range(1, max_attempts + 1):
        try:
            http_get(url, timeout = 3)
            LOGGER.info(f"'{name}' ready after {attempt} attempt{"s" if (attempt > 1) else ""}")
            return True
        except Exception:
            LOGGER.debug(f"    Attempt {attempt}/{max_attempts}: '{name}' not ready, retrying in 2s...")
            time.sleep(2)
    LOGGER.error(f"'{name}' not ready after {max_attempts} attempt{"s" if (attempt > 1) else ""}")
    return False

# ------------------------------------------------------------------------------
# Wait For Stack
# - Waits for all required services in the stack to become healthy
# ------------------------------------------------------------------------------
def wait_for_stack(profiles):
    services = services_for_profiles(profiles)

    health_checks = {
        "clickhouse": ("ClickHouse", f"{CLICKHOUSE_URL}/ping"),
        "dfe-archiver": ("dfe-archiver", DFE_ARCHIVER_HEALTH_URL),
        "dfe-loader": ("dfe-loader", DFE_LOADER_HEALTH_URL),
        "dfe-receiver": ("dfe-receiver", DFE_RECEIVER_HEALTH_URL),
    }

    required_services = {}
    for service in services:
        for prefix, (name, url) in health_checks.items():
            if (service.startswith(prefix) and name not in required_services):
                required_services[name] = url

    for name, url in required_services.items():
        if not(wait_for_service(name, url)):
            return False
    return True


# ==============================================================================
# Data Helpers
# - Functions for preparing test data, sending events, and verifying results
# ==============================================================================

# ------------------------------------------------------------------------------
# Create Table
# - Creates the ClickHouse database/table before each test run
# ------------------------------------------------------------------------------
def create_table(database, table, ch_table_ddl_file_path):
    query = f"CREATE DATABASE IF NOT EXISTS {database}"
    LOGGER.debug(f"Creating database '{database}' with `{query}`...")
    ch_query(query)
    time.sleep(1)
    
    with open(ch_table_ddl_file_path, 'r') as ddl_file:
        ddl_template = ddl_file.read()
    query = ddl_template.replace("{database}", database).replace("{table}", table)
    LOGGER.debug(f"Creating table '{database}.{table}' with `{query}`...")
    ch_query(query)
    time.sleep(1)

# ------------------------------------------------------------------------------
# Truncate Table
# - Truncates the ClickHouse table before each test run
# ------------------------------------------------------------------------------
def clean_table(database, table):
    query = f"TRUNCATE TABLE IF EXISTS {database}.{table}"
    LOGGER.debug(f"Truncating '{database}.{table}' with `{query}`...")
    ch_query(query)
    time.sleep(1)

# ---------------------------------------------------------------------------
# Send Events
# - Reads events from a file and sends to the receiver
# ---------------------------------------------------------------------------
def send_events(ctx, test_name, marker, data_file_name, database, table):
    data_file_path = PROJECT_DIR / data_file_name
    if not(data_file_path.exists()):
        error(f"Data file '{data_file_path}' could not be found", test_name)

    events = data_file_path.read_text().splitlines()
    num_events = len(events)

    LOGGER.info(f"Sending {num_events} events from data file...")
    LOGGER.debug(f"Marker: '{marker}'")

    ctx.total_sent = 0
    send_errors = 0

    for line in events:
        event = json.loads(line)
        event["_source"] = table
        event["_tags"] = json.dumps({"marker": marker, "test_name": test_name})
        enriched = json.dumps(event, separators = (",", ":"))

        try:
            status = http_post(DFE_RECEIVER_INGEST_URL, enriched)
        except Exception:
            status = 0

        if (status < 200 or status >= 300):
            LOGGER.debug(f"Response code '{status}' received for '{enriched}'")
            send_errors += 1

        ctx.total_sent += 1

    LOGGER.info(f"Sent {ctx.total_sent} events (errors = {send_errors})")

    if (send_errors == 0):
        mark_pass(ctx, f"[{test_name}] All {ctx.total_sent} ingest requests successful (200 response codes)")
    else:
        mark_fail(ctx, f"[{test_name}] {send_errors}/{ctx.total_sent} ingest requests failed (non-200 response codes)")

# ---------------------------------------------------------------------------
# Verify Table
# - Queries ClickHouse to check the expected number of events with the marker
# ---------------------------------------------------------------------------
def verify_table(ctx, test_name, database, table, marker, max_attempts = 3):
    expected = ctx.total_sent
    LOGGER.info(f"Verifying events in ClickHouse table '{database}.{table}' (marker: '{marker}')...")
    time.sleep(5)

    actual = 0
    for attempt in range(1, max_attempts + 1):
        # TODO: CHANGE TO JSON OBJECT ONCE TAGS BACK TO JSON
        raw = ch_query(f"SELECT count() FROM {database}.{table} WHERE JSONExtractString(_tags, 'marker') == '{marker}'")
        try:
            actual = int(raw.strip())
        except (ValueError, AttributeError):
            actual = 0

        if (actual >= expected):
            LOGGER.info(f"    Attempt {attempt}/{max_attempts}: got {actual}/{expected}")
            break
        else:
            LOGGER.info(f"    Attempt {attempt}/{max_attempts}: got {actual}/{expected}, retrying in 5s...")
            time.sleep(5)

    if (actual >= expected):
        mark_pass(ctx, f"[{test_name}] '{database}.{table}': {actual}/{expected}")
    else:
        mark_fail(ctx, f"[{test_name}] '{database}.{table}': Expected {expected}, got {actual}")

# ---------------------------------------------------------------------------
# Create Topics
# - Pre-creates expected Kafka topics before dfe-loader starts consuming
# ---------------------------------------------------------------------------
def create_topics(expected_topics):
    if not(expected_topics):
        return True

    LOGGER.debug(f"Creating {len(expected_topics)} 'Kafka' topic{"s" if (len(expected_topics) > 1) else ""}...")
    for topic in expected_topics:
        if not(topic):
            continue
        LOGGER.debug(f"Creating topic: '{topic}'...")
        create_cmd = [
            "docker", "compose", "exec", "-T", "kafka",
            "/opt/kafka/bin/kafka-topics.sh",
            "--bootstrap-server", "kafka:9092",
            "--create", "--if-not-exists",
            "--topic", topic,
            "--partitions", "1",
            "--replication-factor", "1",
        ]
        result = run_cmd(create_cmd, check = False, capture = True)
        if (result.returncode != 0):
            LOGGER.error(f"Failed to create topic '{topic}': {result.stderr or result.stdout}")
            return False
    LOGGER.debug(f"{len(expected_topics)} topic{"s" if (len(expected_topics) > 1) else ""} created")
    return True

# ---------------------------------------------------------------------------
# Verify Topics
# - Checks if the expected Kafka topics exist in the Kafka container
# ---------------------------------------------------------------------------
def verify_topics(ctx, test_name, expected_topics):
    LOGGER.info("Verifying 'Kafka' topics...")
    
    base_cmd = ["docker", "compose", "exec", "-T", "kafka", "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "kafka:9092", "--list"]
    result = run_cmd(base_cmd, check = False, capture = True)
    topics = result.stdout.strip().splitlines() if (result.returncode == 0 and result.stdout) else []

    for topic in expected_topics:
        if not(topic):
            continue
        if (topic in topics):
            mark_pass(ctx, f"[{test_name}] 'Kafka' topic '{topic}' exists")
        else:
            mark_fail(ctx, f"[{test_name}] 'Kafka' topic '{topic}' not found")


# ==============================================================================
# Test Functions
# - Functions for resolving test cases from config and running the test flow
# ==============================================================================

# ------------------------------------------------------------------------------
# Resolve Test Case
# - Builds a TestCase object from the config
# ------------------------------------------------------------------------------
def resolve_test_case(test_config, global_config):
    test_name = get_config("name", test_config, global_config, required = True)
    profiles = get_config("profiles", test_config, global_config, required = True, is_list = True)

    has_archiver = has_service(profiles, "dfe-archiver")
    has_loader = has_service(profiles, "dfe-loader")
    has_receiver = has_service(profiles, "dfe-receiver")

    return TestCase(
        name = test_name,
        profiles = profiles,
        data_file = get_config("data_file", test_config, global_config, required = True),
        archiver_config = get_config("archiver_config", test_config, global_config, required = has_archiver),
        loader_config = get_config("loader_config", test_config, global_config, required = has_loader),
        receiver_config = get_config("receiver_config", test_config, global_config, required = has_receiver),
        database = get_config("database", test_config, global_config, default_value = RUN_ID).replace("-", "_"),
        table = get_config("table", test_config, global_config, default_value = test_name).replace("-", "_"),
        ch_drop_database = get_config("clickhouse.drop_database", test_config, global_config, required = True),
        ch_table_ddl_file_path = get_config("clickhouse.table_ddl_file_path", test_config, global_config, required = True),
        expected_topics = get_config("expected_topics", test_config, global_config, is_list = True),
        marker = f"{RUN_ID}-{test_name}"
    )

# ------------------------------------------------------------------------------
# Run Test
# - Executes the test flow for a given TestCase
# ------------------------------------------------------------------------------
def run_test(ctx, mode, test, persistent_services):
    # Ensure configuration files exist for services used by this test
    if (test.archiver_config and not (PROJECT_DIR / test.archiver_config).exists()):
        error(f"Archiver configuration '{PROJECT_DIR / test.archiver_config}' not found")
    if (test.receiver_config and not (PROJECT_DIR / test.receiver_config).exists()):
        error(f"Receiver configuration '{PROJECT_DIR / test.receiver_config}' not found")
    if (test.loader_config and not (PROJECT_DIR / test.loader_config).exists()):
        error(f"Loader configuration '{PROJECT_DIR / test.loader_config}' not found")

    # Print header with test details and configuration for visibility
    print()
    print("------------------------------------------------------------")
    print(f"E2E Test: {test.name}")
    print(f"  - Profiles: {', '.join(test.profiles)}")
    print(f"  - Data File: {test.data_file}")
    if (test.archiver_config):
        print(f"  - Archiver Config: {test.archiver_config}")
    if (test.receiver_config):
        print(f"  - Receiver Config: {test.receiver_config}")
    if (test.loader_config):
        print(f"  - Loader Config: {test.loader_config}")
    if (test.database != RUN_ID.replace("-", "_")):
        print(f"  - ClickHouse Database: {test.database}")
    if (test.table != test.name.replace("-", "_")):
        print(f"  - ClickHouse Table: {test.table}")
    print("------------------------------------------------------------")

    # Generate a per-run loader config with the test database and table
    loader_config = generate_tmp_loader_config(test.loader_config, test.database, test.table) if (test.loader_config) else None

    # Start the stack (Kafka profiles: infra first, create topics, then full stack)
    if not(stack_up(mode, test.profiles, test.archiver_config, test.receiver_config, loader_config, test.expected_topics)):
        mark_fail(ctx, f"[{test.name}] Stack failed to start")
        stack_down(keep_services = persistent_services or None)
        return

    # Wait for the stack to be healthy before proceeding
    if not(wait_for_stack(test.profiles)):
        mark_fail(ctx, f"[{test.name}] Stack failed to reach healthy state")
        dump_logs()
        stack_down(keep_services = persistent_services or None)
        return

    # Create the ClickHouse database and table for the test
    create_table(test.database, test.table, test.ch_table_ddl_file_path)
    
    # Clean the ClickHouse table to ensure fresh state and send events
    clean_table(test.database, test.table)
    send_events(ctx, test.name, test.marker, test.data_file, test.database, test.table)
    
    # Verify the events have been ingested into ClickHouse with the correct marker
    print()
    verify_table(ctx, test.name, test.database, test.table, test.marker)

    # If any profile has Kafka and expected topics are defined, verify the topics exist
    if (any("kafka" in profile for profile in test.profiles) and test.expected_topics):
        print()
        verify_topics(ctx, test.name, test.expected_topics)

    # Dump container logs for debugging purposes, then tear down the stack if configured to do so
    dump_logs()
    print()
    if (stack_is_up()):
        stack_down(keep_services = persistent_services or None)

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
    if (TMP_DIR.exists()):
        for path in TMP_DIR.iterdir():
            path.unlink()
        TMP_DIR.rmdir()
    if (clickhouse_config.get("drop_database", False)):
        ch_query(f"DROP DATABASE IF EXISTS {RUN_ID.replace("-", "_")}")

# ------------------------------------------------------------------------------
# Error
# - Logs an error message and exits with the specified code
# ------------------------------------------------------------------------------
def error(msg, test_name = "", code = 1, level = 50):
    LOGGER.log(level, f"{f"[{test_name}] " if (test_name) else ""}{msg}")
    sys.exit(code)

# ------------------------------------------------------------------------------
# Require Command
# - Identifies if a command is in the system PATH and exits with an error if not
# ------------------------------------------------------------------------------
def require_command(name):
    if (which(name) is None):
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
    print(f"Start Time: {start_time.strftime("%Y-%m-%dT%H:%M:%S%z")}")
    print()
    print("============================================================")

    # Ensure required commands are available
    require_command("curl")
    require_command("docker")
    
    # Confirm test configuration file exists and load it
    if not(TEST_CONFIG.exists()):
        error(f"Test configuration file '{TEST_CONFIG}' could not be found")
    config = load_config(TEST_CONFIG)

    # Build global_config from the config sections (global + clickhouse) so that
    # get_config can resolve dotted paths like "clickhouse.drop_database"
    global_config = config.get("global", {}).copy()
    global_config["clickhouse"] = config.get("clickhouse", {})

    # Tear down any existing stack before starting
    persistent_services = get_config("persistent_services", {}, global_config)
    if (stack_is_up()):
        print()
        stack_down(persistent_services)

    mode = get_config("mode", {}, global_config)

    # Extract unique profiles from the test config to determine which images to build
    global_profiles = get_config("profiles", {}, global_config, is_list = True)
    test_profiles = set()
    for test in config.get("tests", []):
        for profile in test.get("profiles", global_profiles):
            test_profiles.add(profile)

    # Build all DFE images once upfront - subsequent tests only restart containers
    build_images(mode, test_profiles)

    # Run test based on CLI args or run all if no args provided
    test_count = len(config.get("tests", []))
    test_names = sys.argv[1:]
    if (test_names):
        for test_name in test_names:
            exists = False
            for index in range(test_count):
                if (config["tests"][index].get("name") == test_name):
                    run_test(ctx, mode, resolve_test_case(config["tests"][index], global_config), persistent_services)
                    exists = True
                    break
            if not(exists):
                mark_skip(ctx, f"Test name '{test_name}' could not be found in config '{TEST_CONFIG}'")
    else:
        for index in range(test_count):
            run_test(ctx, mode, resolve_test_case(config["tests"][index], global_config), persistent_services)

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
    print(f"End Time:   {end_time.strftime("%Y-%m-%dT%H:%M:%S%z")}")
    print(f"Duration:   {int(duration.total_seconds()) // 60}m {int(duration.total_seconds()) % 60}s")
    print()
    print("============================================================")

    # Exit with code 1 if any tests failed, otherwise 0
    if ctx.failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
