#  Project:      dfe-docker
#  File:         tests/test_contract_emit.py
#  Purpose:      Assert every app emits its container contract for the engine, and
#                that a custom env reaches the app it is written for
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""What the engine's settings API reads on Compose, and how it gets there.

The engine reports an app's configurable surface from the contract that app's own
binary emits, so each app runs once as a one-shot and writes into the content
volume the engine mounts. Two properties matter and neither is visible in the
source YAML: the emitter runs the SAME pinned image as the app, and an app that
cannot emit leaves its contract absent instead of stopping the engine.

Read off the interpolated model `docker compose config` produces, for the reason
check_compose gives -- interpolation and merging are most of the mechanism, so
the source text is not the thing to assert about.
"""

import os
import shutil
from pathlib import Path

import pytest

import check_compose
import instances
import resolve_profile
from _common import CUSTOM_ENV_SUFFIX, ENV_DIR, PROJECTED_PROFILES, SERVICE_CONFIG_FILE

# Every app with a contract to emit. The two -filebeat services run one of these
# images a second time, so they are deployments rather than apps and emit nothing.
_APPS = (
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-vector",
    "dfe-transform-vrl",
)
_CONTENT_MOUNT = "/app/content"
_CONTENT_VOLUME = "dfe-engine-content"
_CONTRACT_ROOT = f"{_CONTENT_MOUNT}/contract"
_ENGINE_SERVICE = "dfe-engine"
# The host `env/` tree inside the engine, where it writes each app's custom keys.
_APP_ENV_MOUNT = "/app/app-env"
# Written by this module and removed again: nothing in a checkout carries one,
# because the engine writes them at run time. Named as dfe-engine names it.
_CUSTOM_ENV = ENV_DIR / f"dfe-loader{CUSTOM_ENV_SUFFIX}"
_CUSTOM_KEY = "DFE_LOADER_CONTRACT_EMIT_TEST"
# One source's instance of a per-source app, as scripts/instances.py declares it.
_INSTANCE_APP = "dfe-transform-vrl"
_INSTANCE = {_INSTANCE_APP: ["contract-emit-test"]}
_INSTANCE_SERVICE = instances.service_name(_INSTANCE_APP, "contract-emit-test")


def _model(**overrides: str) -> dict[str, dict]:
    """Return the interpolated registry-path services, placeholders for the pins."""
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model these assert on")
    env, _ = check_compose._check_env()
    env.update(overrides)
    config = check_compose._config_json(
        env=env, files=[check_compose.COMPOSE_FILE.name]
    )
    assert config is not None, "the registry compose path did not resolve"
    return config["services"]


def _resolved(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Run the resolver for one profile and read back the assignments it wrote."""
    written = tmp_path / ".profile.mk"
    monkeypatch.setattr(resolve_profile, "PROFILE_MK", written)
    monkeypatch.setattr(resolve_profile, "_load_dotenv", lambda: None)
    monkeypatch.delenv(resolve_profile.ENGINE_CONTENT_DIR_VAR, raising=False)
    monkeypatch.setitem(os.environ, "DFE_PROFILE", profile)
    assert resolve_profile.main() == 0
    values: dict[str, str] = {}
    for line in written.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition(":=")
        values[name.replace("export", "").strip()] = value.strip()
    return values


@pytest.fixture(scope="module")
def services() -> dict[str, dict]:
    """The rendered stack, once for the module."""
    return _model()


@pytest.mark.parametrize("app", _APPS)
def test_each_app_emits_from_the_image_the_stack_runs(
    app: str, services: dict[str, dict]
) -> None:
    """A contract belongs to a digest, so the emitter must not drift off the pin."""
    emitter = services[f"contract-{app}"]

    assert emitter["image"] == services[app]["image"]
    assert emitter["container_name"] == f"dfe-contract-{app}"
    assert emitter["profiles"] == ["core"]
    assert emitter["restart"] == "no"
    # The volume lands root-owned at a path no image creates.
    assert emitter["user"] == "0:0"
    assert [volume["source"] for volume in emitter["volumes"]] == [_CONTENT_VOLUME]


@pytest.mark.parametrize("app", _APPS)
def test_an_app_that_cannot_emit_does_not_stop_the_engine(
    app: str, services: dict[str, dict]
) -> None:
    """scalo exits non-zero on a write it cannot make, hence the wrapper."""
    command = "".join(services[f"contract-{app}"]["command"])

    assert f"{app} config-schema --dir {_CONTRACT_ROOT}/{app}" in command
    assert command.lstrip().startswith("if ")
    assert "else" in command
    assert services[f"contract-{app}"]["entrypoint"] == ["/bin/sh", "-c"]


@pytest.mark.parametrize("app", _APPS)
def test_the_contract_names_the_ref_that_produced_it(
    app: str, services: dict[str, dict]
) -> None:
    """Nothing in the four emitted files says which image wrote them."""
    emitter = services[f"contract-{app}"]
    command = "".join(emitter["command"])

    assert emitter["environment"]["CONTRACT_REF"] == services[app]["image"]
    assert "CONTRACT_REF" in command
    assert f'"app":"{app}"' in command
    assert f"{_CONTRACT_ROOT}/{app}/source.json" in command


def test_the_engine_waits_for_every_contract_and_requires_none(
    services: dict[str, dict],
) -> None:
    """A tier that runs no such app still starts, and a failed emit only warns."""
    depends_on = services[_ENGINE_SERVICE]["depends_on"]

    for app in _APPS:
        assert depends_on[f"contract-{app}"] == {
            "condition": "service_completed_successfully",
            "required": False,
        }


def test_the_engine_reads_the_directory_the_one_shots_wrote(
    services: dict[str, dict],
) -> None:
    engine = services[_ENGINE_SERVICE]

    assert engine["environment"]["DFE_APP_CONTRACT_DIR"] == _CONTRACT_ROOT
    assert _CONTENT_MOUNT in {volume["target"] for volume in engine["volumes"]}


def test_a_tier_that_projects_nothing_leaves_the_engine_no_contract_directory() -> None:
    """The resolver's EMPTY has to survive: `${VAR-...}`, never `${VAR:-...}`."""
    services = _model(**{resolve_profile.ENGINE_CONTRACT_DIR_VAR: ""})

    assert services[_ENGINE_SERVICE]["environment"]["DFE_APP_CONTRACT_DIR"] == ""


def test_the_engine_is_told_where_to_write_a_custom_env(
    services: dict[str, dict],
) -> None:
    """The directory it is given has to BE the bind mount, or it writes nowhere."""
    engine = services[_ENGINE_SERVICE]

    assert engine["environment"]["DFE_DEPLOYMENT_APP_ENV_DIR"] == _APP_ENV_MOUNT
    writable = {
        volume["target"]
        for volume in engine["volumes"]
        if not volume.get("read_only", False)
    }
    assert _APP_ENV_MOUNT in writable


def test_a_missing_custom_env_leaves_every_app_resolving(
    services: dict[str, dict],
) -> None:
    """The engine writes these at run time, so a stack must resolve without them."""
    assert sorted(ENV_DIR.glob("*.custom.env")) == []
    assert services[_ENGINE_SERVICE]["image"]


def test_a_custom_key_reaches_the_app_it_is_written_for() -> None:
    """The whole point of the second env_file, and the half compose does at up-time."""
    if _CUSTOM_ENV.exists():
        pytest.skip(f"{_CUSTOM_ENV} already exists in this checkout")
    # A fresh checkout has no env/, and a check must leave it without one.
    made_dir = not _CUSTOM_ENV.parent.exists()
    _CUSTOM_ENV.parent.mkdir(parents=True, exist_ok=True)
    _CUSTOM_ENV.write_text(f"{_CUSTOM_KEY}=reached\n", encoding="utf-8")
    try:
        services = _model()
    finally:
        _CUSTOM_ENV.unlink()
        if made_dir:
            _CUSTOM_ENV.parent.rmdir()

    assert services["dfe-loader"]["environment"][_CUSTOM_KEY] == "reached"
    assert _CUSTOM_KEY not in services["dfe-receiver"]["environment"]


@pytest.fixture(scope="module")
def reads() -> dict[str, dict]:
    """The stack and one generated instance, rendered over sentinel env files."""
    if shutil.which("docker") is None:
        pytest.skip("`docker compose config` renders the model these assert on")
    env, _ = check_compose._check_env()
    services = check_compose._sentinel_model(
        env=env, fragment=instances.fragment(_INSTANCE)
    )
    assert services is not None, "the registry compose path did not resolve"
    return services


def _custom_files_read(service: dict) -> set[str]:
    return {
        value
        for key, value in service["environment"].items()
        if key.startswith(check_compose._SENTINEL_READ)
    }


@pytest.mark.parametrize("app", sorted(SERVICE_CONFIG_FILE))
def test_every_app_the_engine_renders_reads_the_file_it_writes_last(
    app: str, reads: dict[str, dict]
) -> None:
    """dfe-engine names the file for the Compose service, and a later file would outvote it."""
    own = f"{app}{CUSTOM_ENV_SUFFIX}"

    assert _custom_files_read(reads[app]) == {own}
    assert reads[app]["environment"][check_compose._SENTINEL_LAST] == own


def test_a_generated_instance_reads_its_apps_file_then_its_own(
    reads: dict[str, dict],
) -> None:
    """A per-source extraEnv reaches that source's container, over the app's."""
    own = f"{_INSTANCE_SERVICE}{CUSTOM_ENV_SUFFIX}"
    service = reads[_INSTANCE_SERVICE]

    assert _custom_files_read(service) == {f"{_INSTANCE_APP}{CUSTOM_ENV_SUFFIX}", own}
    assert service["environment"][check_compose._SENTINEL_LAST] == own


@pytest.mark.parametrize("profile", PROJECTED_PROFILES)
def test_a_projected_tier_hands_the_engine_the_contract_directory(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _resolved(profile, tmp_path, monkeypatch)

    assert values[resolve_profile.ENGINE_CONTRACT_DIR_VAR] == _CONTRACT_ROOT


def test_a_hand_crafted_profile_hands_the_engine_no_contract_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Those tiers ship the config they exist to exercise, contract included."""
    values = _resolved("kafka-filebeat", tmp_path, monkeypatch)

    assert values[resolve_profile.ENGINE_CONTRACT_DIR_VAR] == ""


@pytest.mark.parametrize("profile", [*PROJECTED_PROFILES, "kafka-filebeat"])
def test_the_contract_key_is_written_whatever_the_answer(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A line that vanished with its own value would make the included makefile
    # re-settle on every pass, which restarts `make` forever.
    values = _resolved(profile, tmp_path, monkeypatch)

    assert resolve_profile.ENGINE_CONTRACT_DIR_VAR in values
