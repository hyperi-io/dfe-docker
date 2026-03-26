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
TMP_DIR = PROJECT_DIR / ".tmp" / ".test-e2e"
RUN_ID = f"e2e-{datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")}"


# ------------------------------------------------------------------------------
# Config Related
# - Supported config values
# ------------------------------------------------------------------------------
MODES = ["dev", "ci"]
DEFAULTS = {
    "mode": "ci",
    "profile": "full",
    "teardown": True
}

# ------------------------------------------------------------------------------
# Endpoint Related
# - Configurable URLs for services, with appropriate defaults
# ------------------------------------------------------------------------------
CLICKHOUSE_URL = os.environ.get(
    "CLICKHOUSE_URL",
    f"http://localhost:{os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")}",
)
DFE_LOADER_HEALTH_URL = os.environ.get(
    "DFE_LOADER_HEALTH_URL",
    f"http://localhost:{os.environ.get("DFE_LOADER_PROMETHEUS_PORT", "9091")}/health/ready",
)
DFE_RECEIVER_HEALTH_URL = os.environ.get(
    "DFE_RECEIVER_HEALTH_URL",
    f"http://localhost:{os.environ.get("DFE_RECEIVER_PROMETHEUS_PORT", "9090")}/health/ready",
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
    mode: str
    profile: str
    data_file: str
    receiver_config: str
    loader_config: str
    database: str
    table: str
    marker: str
    teardown: bool = True
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
    try:
        with urlopen(request, timeout = 10) as response:
            return response.read().decode().strip()
    except Exception:
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
# - Gets a config field for a test, with fallback to defaults
# ------------------------------------------------------------------------------
def get_config(config, index, field, default_value = None, required = False, is_list = False):
    test_value = config["tests"][index].get(field)
    if (test_value is not None):
        if (is_list):
            return test_value if (isinstance(test_value, list)) else []
        return test_value
    global_value = config.get("global", {}).get(field)
    if (global_value is not None):
        if (is_list):
            return global_value if (isinstance(test_value, list)) else []
        return global_value
    if (is_list):
        default_value = DEFAULTS.get(field)
        if (default_value is not None):
            return default_value if (isinstance(test_value, list)) else []
        return []
    if (default_value is not None):
        return default_value
    if (required):
        error(f"Test '{config['tests'][index].get('name', f'index {index}')}' missing required field '{field}'")
    return None


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
# Services for Profile
# - Returns the list of services associated with a given profile
# ------------------------------------------------------------------------------
def services_for_profile(profile):
    profile_map = parse_compose_profiles()
    if (profile not in profile_map):
        error(f"Unknown profile: '{profile}'")
    return profile_map[profile]

# ------------------------------------------------------------------------------
# Generate Compose Override
# - Creates a temp docker-compose override file to mount test-specific configs
# ------------------------------------------------------------------------------
def generate_compose_override(profile, receiver_config, loader_config):
    services = services_for_profile(profile)
    receiver_service = next(service for service in services if (service.startswith("dfe-receiver")))
    loader_service = next(service for service in services if (service.startswith("dfe-loader")))
    
    TMP_DIR.mkdir(exist_ok = True)
    override_file = TMP_DIR / "docker-compose.e2e.yaml"

    content = (
        "services:\n"
        f"  {receiver_service}:\n"
        f"    volumes:\n"
        f"      - ./{receiver_config}:/etc/dfe-receiver/config.yaml:ro\n"
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
# - Runs a subprocess command with logging and error handling
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
def stack_up(mode, profile, receiver_config, loader_config):
    LOGGER.info("Starting stack...")
    LOGGER.debug(f"Services for profile '{profile}':")
    for service in services_for_profile(profile):
        LOGGER.debug(f"  - {service}")

    override_file = generate_compose_override(profile, receiver_config, loader_config)

    compose_files = ["-f", "docker-compose.yml"]
    if (mode not in MODES):
        error(f"Unknown mode: '{mode}'. Supported modes: '{MODES}'")
    elif (mode == "dev" and (PROJECT_DIR / "docker-compose.override.yml").exists()):
        compose_files += ["-f", "docker-compose.override.yml"]

    compose_files += ["-f", override_file]
    profile_flags = ["--profile", profile]

    base_cmd = ["docker", "compose"] + compose_files + profile_flags
    build_args = base_cmd + ["build", "--no-cache", "--pull"]
    up_args = base_cmd + ["up", "-d"]

    if (mode == "ci"):
        build_result = run_cmd(build_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
        if (build_result.returncode != 0):
            filtered = filter_build_output(build_result.stderr or build_result.stdout or "")
            LOGGER.error(f"Docker compose build failed: {filtered if (filtered) else (build_result.stderr or build_result.stdout)}")
            return False
    elif (mode == "dev"):
        up_args += ["--build"]
    
    up_result = run_cmd(up_args, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    if (up_result.returncode != 0):
        filtered = filter_build_output(up_result.stderr or up_result.stdout or "")
        LOGGER.error(f"Docker compose up failed: {filtered if (filtered) else (up_result.stderr or up_result.stdout)}")
        return False
    return (up_result.returncode == 0)

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
def stack_down():
    base_cmd = ["docker", "compose"]
    LOGGER.info("Stopping stack...")

    profiles = parse_compose_profiles()
    profile_flags = []
    for profile in profiles:
        profile_flags += ["--profile", profile]
    
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
    time.sleep(5)
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
def wait_for_stack():
    required_services = {
        "ClickHouse": f"{CLICKHOUSE_URL}/ping",
        "dfe-loader": f"{DFE_LOADER_HEALTH_URL}",
        "dfe-receiver": f"{DFE_RECEIVER_HEALTH_URL}"
    }
    for name, url in required_services.items():
        if not(wait_for_service(name, url)):
            return False
    return True


# ==============================================================================
# Data Helpers
# - Functions for preparing test data, sending events, and verifying results
# ==============================================================================

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
def send_events(ctx, test_name, marker, data_file_name):
    data_file_path = PROJECT_DIR / data_file_name
    if not(data_file_path.exists()):
        error(test_name, f"Data file '{data_file_path}' could not be found")

    events = data_file_path.read_text().splitlines()
    num_events = len(events)

    LOGGER.info(f"Sending {num_events} events from data file...")
    LOGGER.debug(f"Marker: '{marker}'")

    ctx.total_sent = 0
    send_errors = 0

    for line in events:
        event = json.loads(line)
        event["metadata"] = json.dumps({"marker": marker, "test_name": test_name})
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
        mark_pass(ctx, f"[{test_name}] All {ctx.total_sent} ingest requests succesful (200 response codes)")
    else:
        mark_fail(ctx, f"[{test_name}] {send_errors}/{ctx.total_sent} ingest requests failed (non-200 response codes)")

# ---------------------------------------------------------------------------
# Verify Table
# - Queries ClickHouse to check the expected number of events with the marker
# ---------------------------------------------------------------------------
def verify_table(ctx, test_name, database, table, marker, max_attempts = 3):
    expected = ctx.total_sent
    LOGGER.info(f"Verifying events in ClickHouse table '{database}.{table}' (marker: '{marker}')...")

    actual = 0
    for attempt in range(1, max_attempts + 1):
        raw = ch_query(f"SELECT count() FROM {database}.{table} WHERE metadata LIKE '%{marker}%'")
        try:
            actual = int(raw.strip())
        except (ValueError, AttributeError):
            actual = 0

        if (actual >= expected):
            LOGGER.info(f"    Attempt {attempt}/{max_attempts}: got {actual}/{expected}")
            break
        else:
            LOGGER.info(f"    Attempt {attempt}/{max_attempts}: got {actual}/{expected}, retrying in 10s...")
            time.sleep(10)

    if (actual >= expected):
        mark_pass(ctx, f"[{test_name}] '{database}.{table}': {actual}/{expected}")
    else:
        mark_fail(ctx, f"[{test_name}] '{database}.{table}': Expected {expected}, got {actual}")

# ---------------------------------------------------------------------------
# Verify Topics
# - Checks if the expected Kafka topics exist in the Kafka container
# ---------------------------------------------------------------------------
def verify_topics(ctx, test_name, expected_topics):
    LOGGER.info("Verifying Kafka topics...")
    
    base_cmd = ["docker", "compose", "exec", "-T", "kafka", "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "kafka:9092", "--list"]
    result = run_cmd(base_cmd, check = False, capture = not(LOG_LEVEL == "DEBUG"))
    topics = result.stdout.strip().splitlines() if (result.returncode == 0) else []

    for topic in expected_topics:
        if not(topic):
            continue
        if (topic in topics):
            mark_pass(ctx, f"[{test_name}] Kafka topic '{topic}' exists")
        else:
            mark_fail(ctx, f"[{test_name}] Kafka topic '{topic}' not found")


# ==============================================================================
# Test Functions
# - Functions for resolving test cases from config and running the test flow
# ==============================================================================

# ------------------------------------------------------------------------------
# Resolve Test Case
# - Builds a TestCase object from the config
# ------------------------------------------------------------------------------
def resolve_test_case(config, index):
    test_name = get_config(config, index, "name", required = True)
    return TestCase(
        name = test_name,
        mode = get_config(config, index, "mode", default_value = DEFAULTS["mode"], required = True),
        profile = get_config(config, index, "profile", default_value = DEFAULTS["profile"], required = True),
        data_file = get_config(config, index, "data_file", required = True),
        receiver_config = get_config(config, index, "receiver_config", required = True),
        loader_config = get_config(config, index, "loader_config", required = True),
        database = "dfe",
        table = "default",
        teardown = get_config(config, index, "teardown", default_value = DEFAULTS["teardown"], required = True),
        expected_topics = get_config(config, index, "expected_topics", required = False, is_list = True),
        marker = f"{RUN_ID}-{test_name}"
    )

# ------------------------------------------------------------------------------
# Run Test
# - Executes the test flow for a given TestCase
# ------------------------------------------------------------------------------
def run_test(ctx, test):
    # Ensure configuration files exist
    if not (PROJECT_DIR / test.receiver_config).exists():
        error(f"Receiver configuration '{PROJECT_DIR / test.receiver_config}' not found")
    if not (PROJECT_DIR / test.loader_config).exists():
        error(f"Loader configuration '{PROJECT_DIR / test.loader_config}' not found")

    # Print header with test details and configuration for visibility
    print()
    print("------------------------------------------------------------")
    print(f"E2E Test: {test.name}")
    print(f"  - Mode: {test.mode}")
    print(f"  - Profile: {test.profile}")
    print(f"  - Data File: {test.data_file}")
    print(f"  - Receiver Config: {test.receiver_config}")
    print(f"  - Loader Config: {test.loader_config}")
    if not(test.teardown):
        print(f"  - Teardown: {test.teardown}")
    print("------------------------------------------------------------")

    # Start the stack with the appropriate configuration for the test
    if not(stack_up(test.mode, test.profile, test.receiver_config, test.loader_config)):
        mark_fail(ctx, f"[{test.name}] Stack failed to start")
        if (test.teardown):
            stack_down()
        return

    # Wait for the stack to be healthy before proceeding
    if not(wait_for_stack()):
        mark_fail(ctx, f"[{test.name}] Stack failed to reach healthy state")
        dump_logs()
        if (test.teardown):
            stack_down()
        return

    # Clean the ClickHouse table to ensure fresh state and send events
    clean_table(test.database, test.table)
    send_events(ctx, test.name, test.marker, test.data_file)
    
    # Verify the events have been ingested into ClickHouse with the correct marker
    print()
    verify_table(ctx, test.name, test.database, test.table, test.marker)

    # If profile has Kafka and expected topics are defined, verify the topics exist in Kafka
    if ("kafka" in test.profile and test.expected_topics):
        print()
        verify_topics(ctx, test.name, test.expected_topics)

    # Dump container logs for debugging purposes, then tear down the stack if configured to do so
    dump_logs()
    print()
    if (test.teardown and stack_is_up()):
        stack_down()
    else:
        LOGGER.info(f"Teardown disabled - stack left running for '{test.name}'")

    # Print footer for test separation in logs
    print("------------------------------------------------------------")

# ==============================================================================
# Miscellaneous Functions
# ==============================================================================

# ------------------------------------------------------------------------------
# Cleanup
# - Removes any temporary files created during the test run
# ------------------------------------------------------------------------------
def cleanup():
    if (TMP_DIR.exists()):
        for path in TMP_DIR.iterdir():
            path.unlink()
        TMP_DIR.rmdir()

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

    # Tear down any existing stack before starting
    if (stack_is_up()):
        print()
        stack_down()
    
    # Run test based on CLI args or run all if no args provided
    test_count = len(config.get("tests", []))
    test_names = sys.argv[1:]
    if (test_names):
        for test_name in test_names:
            exists = False
            for index in range(test_count):
                if (config["tests"][index].get("name") == test_name):
                    run_test(ctx, resolve_test_case(config, index))
                    exists = True
                    break
            if not(exists):
                mark_skip(ctx, f"Test name '{test_name}' could not be found in config '{TEST_CONFIG}'")
    else:
        for index in range(test_count):
            run_test(ctx, resolve_test_case(config, index))
    
    # Post test execution cleanup
    cleanup()
    
    # Capture end time for test suite completion and calcualte duration
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
