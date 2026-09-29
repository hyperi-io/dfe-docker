#  Project:      dfe-docker
#  File:         tests/test_instances.py
#  Purpose:      Assert the compose service count follows the sources, not a constant
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What a deployment actually runs for an app that runs one container per source.

The gap these close: Compose declared one dfe-fetcher, so a deployment with two
CrowdStrike accounts ran one of them. The count now comes from the index
dfe-engine writes when it renders each instance's config, so two sources are two
containers and no source is none.
"""

import re
import subprocess
from pathlib import Path

import pytest

import instances
from _common import COMPOSE_FILE, REPO_ROOT

FETCHER = "dfe-fetcher"
VRL = "dfe-transform-vrl"
COMPOSE_ARGS = ["-f", "docker-compose.yml", "-f", "docker-compose.instances.yml"]


def _index(env_dir: Path, service: str, *names: str) -> None:
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / f"{service}{instances.INDEX_SUFFIX}").write_text(
        "".join(f"{name}\n" for name in names), encoding="utf-8"
    )


def test_two_sources_of_one_connector_are_two_services(tmp_path: Path) -> None:
    _index(tmp_path, FETCHER, "crowdstrike-eu", "crowdstrike-us")

    found = instances.declared(env_dir=tmp_path)

    assert instances.services(found) == [
        "dfe-fetcher-crowdstrike-eu",
        "dfe-fetcher-crowdstrike-us",
    ]


def test_a_deployment_with_no_source_declares_nothing(tmp_path: Path) -> None:
    _index(tmp_path, FETCHER)

    assert instances.declared(env_dir=tmp_path) == {}


def test_a_deployment_that_never_rendered_declares_nothing(tmp_path: Path) -> None:
    assert instances.declared(env_dir=tmp_path / "absent") == {}


def test_every_app_with_an_index_is_carried(tmp_path: Path) -> None:
    # Which apps run one per source is apps.yaml's to say, so the generator
    # discovers them rather than holding a list of its own.
    _index(tmp_path, FETCHER, "okta-audit")
    _index(tmp_path, VRL, "filebeat")

    assert instances.services(instances.declared(env_dir=tmp_path)) == [
        "dfe-fetcher-okta-audit",
        "dfe-transform-vrl-filebeat",
    ]


def test_each_service_reads_its_own_instance_config() -> None:
    text = instances.fragment({FETCHER: ["crowdstrike-eu", "crowdstrike-us"]})

    assert '"/etc/dfe/apps/dfe-fetcher/crowdstrike-eu/fetcher.yaml"' in text
    assert '"/etc/dfe/apps/dfe-fetcher/crowdstrike-us/fetcher.yaml"' in text


def test_each_service_extends_the_committed_one() -> None:
    # One definition of the image, volumes, limits and healthcheck.
    text = instances.fragment(
        {FETCHER: ["okta-audit"]}, compose_file="docker-compose.yml"
    )

    assert (
        "    extends:\n      file: docker-compose.yml\n      service: dfe-fetcher"
        in text
    )


def test_each_service_reads_the_custom_env_file_the_engine_names_for_it() -> None:
    # dfe-engine writes a source's extraEnv to env/<app>-<source>.custom.env.
    text = instances.fragment({FETCHER: ["crowdstrike-eu", "crowdstrike-us"]})

    for source in ("crowdstrike-eu", "crowdstrike-us"):
        assert (
            "    env_file:\n"
            f"      - path: env/dfe-fetcher-{source}.custom.env\n"
            "        required: false\n"
        ) in text


def test_no_instance_publishes_a_host_port() -> None:
    # N containers cannot share one host port, and nothing outside the stack
    # addresses an instance directly.
    text = instances.fragment({FETCHER: ["a", "b"]})

    assert text.count("ports: !reset []") == 2


def test_each_container_is_named_for_its_source() -> None:
    text = instances.fragment({FETCHER: ["okta-audit"]})

    assert "  dfe-fetcher-okta-audit:" in text
    assert "OTEL_SERVICE_NAME: dfe-fetcher-okta-audit" in text


def test_each_instance_container_carries_the_stack_prefix() -> None:
    # A container name is daemon-wide, so two stacks collide here as well as on
    # the committed services.
    text = instances.fragment({FETCHER: ["okta-audit"]})

    assert "container_name: ${DFE_CONTAINER_PREFIX:-}dfe-fetcher-okta-audit" in text


def test_an_app_this_repo_has_no_service_for_is_named(tmp_path: Path) -> None:
    _index(tmp_path, "dfe-nonesuch", "somewhere")

    assert instances.unknown_apps(instances.declared(env_dir=tmp_path)) == [
        "dfe-nonesuch"
    ]


def test_an_app_with_a_config_file_but_no_compose_service_is_named(
    tmp_path: Path,
) -> None:
    # extends naming a service that is not there fails the whole compose file.
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  dfe-loader:\n    image: x\n", encoding="utf-8")
    _index(tmp_path, FETCHER, "okta-audit")

    found = instances.declared(env_dir=tmp_path)

    assert instances.unknown_apps(found, compose_file=compose) == [FETCHER]


def test_the_fragment_is_removed_when_the_last_source_goes(tmp_path: Path) -> None:
    # A stale file would declare containers for sources this deployment lost.
    target = tmp_path / "docker-compose.instances.yml"
    assert instances.write({FETCHER: ["okta-audit"]}, path=target) is True

    assert instances.write({}, path=target) is False
    assert not target.exists()


@pytest.mark.parametrize("service", sorted(instances.SERVICE_CONFIG_FILE))
def test_every_app_the_generator_knows_is_a_committed_compose_service(
    service: str,
) -> None:
    assert instances.extendable(service)


def test_each_instance_container_is_labelled_with_its_app() -> None:
    # The label is what tells a generated instance apart from any other orphan.
    text = instances.fragment({VRL: ["cisco-meraki"]})

    assert f"    labels:\n      {instances.INSTANCE_LABEL}: {VRL}\n" in text


def test_the_fragment_in_place_names_the_services_it_declares(tmp_path: Path) -> None:
    target = tmp_path / "docker-compose.instances.yml"
    found = {FETCHER: ["okta-audit"], VRL: ["cisco-ios", "cisco-meraki"]}
    instances.write(found, path=target)

    assert instances.fragment_services(path=target) == set(instances.services(found))


def test_no_fragment_declares_no_service(tmp_path: Path) -> None:
    assert instances.fragment_services(path=tmp_path / "absent.yml") == set()


def test_each_fetcher_instance_keeps_its_cursors_in_a_directory_of_its_own() -> None:
    text = instances.fragment(instances={FETCHER: ["crowdstrike-eu", "crowdstrike-us"]})

    for source in ("crowdstrike-eu", "crowdstrike-us"):
        assert (
            "      DFE_FETCHER_CURSOR__DIRECTORY: "
            f"/var/lib/dfe-fetcher/dfe-fetcher-{source}\n"
        ) in text


def test_an_app_that_keeps_no_state_gets_no_state_directory() -> None:
    text = instances.fragment(instances={VRL: ["cisco-meraki"]})

    for variable, _ in instances.STATE_DIRS.values():
        assert variable not in text


def _committed_block(*, name: str, text: str) -> str:
    """One top-level service or volume entry of a compose file, as text."""
    lines = text.splitlines()
    start = lines.index(f"  {name}:")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^ {0,2}\S", lines[index])
        ),
        len(lines),
    )
    return "\n".join(lines[start:end]) + "\n"


def _volume_at(*, block: str, target: str) -> str:
    """The named volume a service block mounts read-write at ``target``."""
    match = re.search(
        rf"^      - ([A-Za-z0-9_-]+):{re.escape(target)}$", block, re.MULTILINE
    )
    assert match, f"no named volume mounted read-write at {target!r}"
    return match.group(1)


@pytest.mark.parametrize("app", sorted(instances.STATE_DIRS))
def test_the_committed_service_keeps_its_state_on_a_named_volume(app: str) -> None:
    variable, root = instances.STATE_DIRS[app]
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    block = _committed_block(name=app, text=compose)

    volume = _volume_at(block=block, target=root)
    assert f"\n  {volume}:\n" in compose.split("\nvolumes:\n")[1]
    assert f"      {variable}: {root}/{app}\n" in block


@pytest.mark.parametrize("app", sorted(instances.STATE_DIRS))
def test_the_state_volume_is_made_writable_before_the_app_starts(app: str) -> None:
    # Docker creates the mountpoint root-owned and the app runs as uid 1000.
    _, root = instances.STATE_DIRS[app]
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    init = _committed_block(name="dlq-init", text=compose)

    assert _volume_at(block=init, target=root) == _volume_at(
        block=_committed_block(name=app, text=compose), target=root
    )
    assert re.search(rf"chown -R 1000:1000 .*{re.escape(root)}(\s|$)", init)


@pytest.mark.parametrize("app", sorted(instances.STATE_DIRS))
def test_the_state_volume_moves_with_the_data_root(app: str) -> None:
    _, root = instances.STATE_DIRS[app]
    volume = _volume_at(
        block=_committed_block(name=app, text=COMPOSE_FILE.read_text(encoding="utf-8")),
        target=root,
    )
    overlay = _committed_block(
        name=volume,
        text=(REPO_ROOT / "docker-compose.storage.yml").read_text(encoding="utf-8"),
    )
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    device = re.search(r'device: "\$\{DFE_DATA_ROOT\}/([^"]+)"', overlay)
    made = re.search(r"\$\(addprefix \$\(DFE_DATA_ROOT\)/,([^)]+)\)", makefile)
    assert device
    assert made
    assert device.group(1) in made.group(1).split()


class _Docker:
    """Answers `docker compose ps` with a fixed listing and records every other call."""

    def __init__(self, listing: str, *, ps_status: int = 0) -> None:
        self.listing = listing
        self.ps_status = ps_status
        self.listed: list[list[str]] = []
        self.changed: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess:
        if args[:2] == ["docker", "compose"]:
            self.listed.append(args)
            return subprocess.CompletedProcess(
                args, self.ps_status, stdout=self.listing, stderr="no daemon"
            )
        self.changed.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _listing(*rows: tuple[str, str, str]) -> str:
    return "".join(
        f"{container}\t{service}\t{app}\n" for container, service, app in rows
    )


# One container of each kind a compose project can hold here: a declared
# instance, a deleted source's instance, a committed static instance of the same
# app, and a service only a file outside the apply chain declares -- an orphan to
# the apply exactly as the deleted source is.
_PROJECT = _listing(
    ("c1", "dfe-transform-vrl-cisco-ios", VRL),
    ("c2", "dfe-transform-vrl-cisco-meraki", VRL),
    ("c3", "dfe-transform-e2e-vrl-filebeat", ""),
    ("c4", "dfe-e2e-sidecar", ""),
    ("c5", "dfe-loader", ""),
)


def test_apply_removes_a_deleted_sources_container_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(_PROJECT)
    monkeypatch.setattr(instances.subprocess, "run", docker)

    status = instances.prune(COMPOSE_ARGS, keep={"dfe-transform-vrl-cisco-ios"})

    assert status == 0
    assert docker.changed == [["docker", "stop", "c2"], ["docker", "rm", "c2"]]


def test_the_listing_is_of_the_applied_project_orphans_included(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(_PROJECT)
    monkeypatch.setattr(instances.subprocess, "run", docker)

    instances.prune(COMPOSE_ARGS, keep=set())

    (listed,) = docker.listed
    assert listed[: 2 + len(COMPOSE_ARGS) + 1] == [
        "docker",
        "compose",
        *COMPOSE_ARGS,
        "ps",
    ]
    assert "--all" in listed
    assert "--orphans" in listed


def test_nothing_is_removed_while_every_instance_is_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = _Docker(_PROJECT)
    monkeypatch.setattr(instances.subprocess, "run", docker)

    keep = {"dfe-transform-vrl-cisco-ios", "dfe-transform-vrl-cisco-meraki"}
    assert instances.prune(COMPOSE_ARGS, keep=keep) == 0
    assert docker.changed == []


def test_a_tier_with_no_fragment_keeps_no_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The profile removes the fragment where the engine renders nothing, so an
    # instance container left running is a leftover of the tier before.
    docker = _Docker(_PROJECT)
    monkeypatch.setattr(instances.subprocess, "run", docker)

    instances.prune(COMPOSE_ARGS, keep=set())

    assert docker.changed == [
        ["docker", "stop", "c1", "c2"],
        ["docker", "rm", "c1", "c2"],
    ]


def test_a_listing_that_fails_removes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = _Docker(_PROJECT, ps_status=1)
    monkeypatch.setattr(instances.subprocess, "run", docker)

    assert instances.prune(COMPOSE_ARGS, keep=set()) == 1
    assert docker.changed == []
