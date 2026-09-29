#!/usr/bin/env python3
#  Project:      dfe-docker
#  File:         tests/test_build_dev_images.py
#  Purpose:      Prove `make dev` builds each image the way the release does and
#                repoints every service that runs it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for build_dev_images.py against the committed compose files.

Which services run a component's image is read off docker-compose.yml itself,
so a service added there and not to IMAGE_CONSUMERS fails here instead of
running the registry image beside a local build.
"""

import re
from pathlib import Path

import build_dev_images
from _common import COMPOSE_FILE

_OVERRIDE_FILE = COMPOSE_FILE.parent / "docker-compose.override.yml"
_SERVICE_KEY_RE = re.compile(r"^  ([A-Za-z0-9._-]+):$")
_IMAGE_RE = re.compile(r"^    image: (.+)$")
_DEPENDS_ON_RE = re.compile(r"^    depends_on:$")
_DEPENDENCY_RE = re.compile(r"^      (?:- )?([A-Za-z0-9._-]+):?$")
_REGISTRY_IMAGE_RE = re.compile(r"^\$\{IMAGE_REGISTRY:-[^}]*\}/([a-z0-9-]+):")
# One-shots that emit an artefact of the PINNED release (docs/developing.md).
_PINNED_ONE_SHOT_RE = re.compile(r"^(catalogue|contract)-")


def _services(path: Path) -> dict[str, dict[str, object]]:
    """Return {service: {"image": str, "depends_on": [str]}} read off a compose file."""
    text = path.read_text(encoding="utf-8")
    block = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    services: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    in_depends_on = False
    for line in block.splitlines():
        if key := _SERVICE_KEY_RE.match(line):
            current = services.setdefault(key.group(1), {"depends_on": []})
            in_depends_on = False
        elif current is None:
            continue
        elif image := _IMAGE_RE.match(line):
            current["image"] = image.group(1)
        elif _DEPENDS_ON_RE.match(line):
            in_depends_on = True
        elif in_depends_on and (dependency := _DEPENDENCY_RE.match(line)):
            current["depends_on"].append(dependency.group(1))
        elif line.strip() and len(line) - len(line.lstrip()) <= 4:
            in_depends_on = False
    return services


def _runners(*, component: str) -> set[str]:
    """Return every compose service that runs component's registry image, pinned one-shots aside."""
    image = build_dev_images.SERVICE_REPO_DIRS.get(component, component)
    runners = set()
    for service, config in _services(COMPOSE_FILE).items():
        match = _REGISTRY_IMAGE_RE.match(str(config.get("image", "")))
        if match and match.group(1) == image and not _PINNED_ONE_SHOT_RE.match(service):
            runners.add(service)
    return runners


def test_the_compose_reader_sees_images_and_depends_on():
    """The derived tests below pass vacuously on a reader that finds nothing."""
    services = _services(COMPOSE_FILE)

    assert {"dfe-engine", "dfe-hunt-runner"} <= _runners(component="dfe-engine")
    assert "dlq-init" in services["dfe-fetcher"]["depends_on"]
    assert "clickhouse" in services["dfe-engine"]["depends_on"]


def test_a_local_build_repoints_every_service_that_runs_its_image():
    """dfe-dashboards ran the registry engine beside a local one."""
    wrong = {}
    for component in build_dev_images.buildable_components():
        want = sorted(_runners(component=component))
        got = sorted(build_dev_images.local_image_services([component]))
        if got != want:
            wrong[component] = {"compose runs it as": want, "a local build moves": got}

    assert wrong == {}


def test_the_committed_override_repoints_every_service_that_runs_a_component():
    override = _services(_OVERRIDE_FILE)
    wrong = {}
    for component in build_dev_images.buildable_components():
        for service in sorted(_runners(component=component)):
            got = override.get(service, {}).get("image")
            if got != f"{component}:local":
                wrong[service] = got

    assert wrong == {}


def test_a_consumer_started_without_its_component_still_gets_it_built():
    """hyperdx starts dfe-dashboards on a stack whose engine is off."""
    services = _services(COMPOSE_FILE)
    missing = []
    for component in build_dev_images.buildable_components():
        for consumer in sorted(_runners(component=component) - {component}):
            for dependent, config in sorted(services.items()):
                if consumer not in config["depends_on"] or dependent == component:
                    continue
                built = build_dev_images._implicit_components(services=[dependent])
                if built.get(consumer) != component:
                    missing.append(f"{dependent} starts {consumer} ({component})")

    assert missing == []


def test_dev_images_are_packaged_with_no_build_arg_the_release_does_not_set():
    """hyperdx:local was built in browser-local mode, with authentication off."""
    for component in build_dev_images.buildable_components():
        command = build_dev_images._package_command(
            context=Path("ctx"), dockerfile=Path("ctx/Dockerfile"), service=component
        )

        assert "--build-arg" not in command, component
        assert command[-1] == "ctx"
        assert f"{component}:local" in command
