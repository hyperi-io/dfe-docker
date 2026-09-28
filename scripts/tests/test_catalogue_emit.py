#  Project:      dfe-docker
#  File:         tests/test_catalogue_emit.py
#  Purpose:      Assert the elastic transform's image supplies the source catalogue
#                the engine offers, and that an image without it stops nothing
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where the engine's source catalogue comes from on Compose.

dfe-transform-elastic carries its catalogue inside the binary and prints it with
`emit-catalogue`, so a one-shot runs the elastic image the stack already pins and
writes the file into the content volume the engine mounts. Two properties matter:
the emitter runs the SAME pinned image as the app, and an image that predates the
subcommand leaves the catalogue absent instead of stopping the engine.

Read off the interpolated model `docker compose config` produces, for the reason
check_compose gives. The one-shot's script is then run as rendered, with a
stand-in for the app binary on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import check_compose
import resolve_profile
from _common import PROJECTED_PROFILES

_APP = "dfe-transform-elastic"
_EMITTER = f"catalogue-{_APP}"
_CONTENT_MOUNT = "/app/content"
_CONTENT_VOLUME = "dfe-engine-content"
_CATALOGUE_FILE = f"{_CONTENT_MOUNT}/catalogue/sources.yaml"
_ENGINE_SERVICE = "dfe-engine"

# Stand-ins for the app binary, one per outcome. Exit 2 is what an image built
# before the subcommand answers.
_WITHOUT_SUBCOMMAND = (
    "#!/bin/sh\necho \"error: unrecognized subcommand '$1'\" >&2\nexit 2\n"
)
_DIES_MIDWAY = "#!/bin/sh\nprintf 'sources:\\n  cisco_ios:\\n'\nexit 1\n"
_WITH_SUBCOMMAND = (
    "#!/bin/sh\n"
    '[ "$1" = emit-catalogue ] || exit 64\n'
    "printf 'sources:\\n  cisco_ios:\\n    package: cisco_ios\\n'\n"
)


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


def _container_script(services: dict[str, dict]) -> str:
    """The script the one-shot's shell receives.

    `docker compose config` writes a literal `$` back as `$$`, so the model has to
    be unescaped once to read what the container runs.
    """
    return "".join(services[_EMITTER]["command"]).replace("$$", "$")


def _run_emitter(
    services: dict[str, dict], app_body: str, tmp_path: Path
) -> tuple[int, str, dict[str, str]]:
    """Run the one-shot's rendered script with this stand-in for the app binary.

    The content mount is moved under tmp_path, so the script writes where it would
    in the container, relative to the volume. Returns (exit code, output, every
    file left in the catalogue directory).
    """
    mount = tmp_path / "content"
    script = _container_script(services).replace(_CONTENT_MOUNT, str(mount))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / _APP
    binary.write_text(app_body, encoding="utf-8", newline="\n")
    binary.chmod(0o755)
    shell = ["busybox", "sh"] if shutil.which("busybox") else ["/bin/sh"]
    done = subprocess.run(
        [*shell, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        check=False,
        timeout=60,
    )
    root = mount / "catalogue"
    written = (
        {p.name: p.read_text(encoding="utf-8") for p in root.iterdir()}
        if root.is_dir()
        else {}
    )
    return done.returncode, done.stdout + done.stderr, written


@pytest.fixture(scope="module", autouse=True)
def _default_container_prefix() -> Iterator[None]:
    """Pin the default (empty) container prefix, whatever the caller's checkout sets.

    Every name below assumes it -- a second local stack's .env sets one, and this
    file must not fail depending on the environment it happens to run in.
    """
    previous = os.environ.pop("DFE_CONTAINER_PREFIX", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["DFE_CONTAINER_PREFIX"] = previous


@pytest.fixture(scope="module")
def services() -> dict[str, dict]:
    """The rendered stack, once for the module."""
    return _model()


def test_the_catalogue_comes_from_the_image_the_stack_runs(
    services: dict[str, dict],
) -> None:
    """A catalogue belongs to a release, so the emitter must not drift off the pin."""
    emitter = services[_EMITTER]

    assert emitter["image"] == services[_APP]["image"]
    assert emitter["container_name"] == f"dfe-{_EMITTER}"
    assert emitter["profiles"] == ["core"]
    assert emitter["restart"] == "no"
    # The volume lands root-owned at a path no image creates.
    assert emitter["user"] == "0:0"
    assert emitter["entrypoint"] == ["/bin/sh", "-c"]
    assert [volume["source"] for volume in emitter["volumes"]] == [_CONTENT_VOLUME]
    assert [volume["target"] for volume in emitter["volumes"]] == [_CONTENT_MOUNT]


def test_the_emitter_asks_the_app_and_lands_only_a_whole_catalogue(
    services: dict[str, dict],
) -> None:
    command = _container_script(services)

    assert f"dst={_CATALOGUE_FILE}" in command
    assert f'{_APP} emit-catalogue > "$dst.tmp"' in command
    assert 'mv "$dst.tmp" "$dst"' in command
    assert 'rm -f "$dst.tmp"' in command


def test_an_image_without_the_subcommand_exits_0_and_writes_nothing(
    services: dict[str, dict], tmp_path: Path
) -> None:
    """The case every released image is in until the next elastic release."""
    rc, output, written = _run_emitter(services, _WITHOUT_SUBCOMMAND, tmp_path)

    assert rc == 0, output
    assert "emitted nothing" in output
    assert written == {}


def test_a_half_printed_catalogue_never_lands(
    services: dict[str, dict], tmp_path: Path
) -> None:
    rc, output, written = _run_emitter(services, _DIES_MIDWAY, tmp_path)

    assert rc == 0, output
    assert written == {}


def test_an_image_with_the_subcommand_lands_what_it_printed(
    services: dict[str, dict], tmp_path: Path
) -> None:
    rc, output, written = _run_emitter(services, _WITH_SUBCOMMAND, tmp_path)

    assert rc == 0, output
    assert written == {
        "sources.yaml": "sources:\n  cisco_ios:\n    package: cisco_ios\n"
    }


def test_the_engine_waits_for_the_catalogue_and_does_not_require_it(
    services: dict[str, dict],
) -> None:
    """A tier that runs no core profile still starts, and a failed emit only warns."""
    assert services[_ENGINE_SERVICE]["depends_on"][_EMITTER] == {
        "condition": "service_completed_successfully",
        "required": False,
    }


def test_the_engine_reads_the_file_the_one_shot_wrote(
    services: dict[str, dict],
) -> None:
    engine = services[_ENGINE_SERVICE]

    assert engine["environment"]["DFE_SOURCE_CATALOGUE_FILE"] == _CATALOGUE_FILE
    assert _CONTENT_MOUNT in {volume["target"] for volume in engine["volumes"]}


def test_a_tier_that_projects_nothing_leaves_the_engine_no_catalogue() -> None:
    """The resolver's EMPTY has to survive: `${VAR-...}`, never `${VAR:-...}`."""
    services = _model(**{resolve_profile.ENGINE_CATALOGUE_FILE_VAR: ""})

    assert services[_ENGINE_SERVICE]["environment"]["DFE_SOURCE_CATALOGUE_FILE"] == ""


@pytest.mark.parametrize("profile", PROJECTED_PROFILES)
def test_a_projected_tier_hands_the_engine_the_catalogue(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _resolved(profile, tmp_path, monkeypatch)

    assert values[resolve_profile.ENGINE_CATALOGUE_FILE_VAR] == _CATALOGUE_FILE


def test_a_hand_crafted_profile_hands_the_engine_no_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _resolved("kafka-filebeat", tmp_path, monkeypatch)

    assert values[resolve_profile.ENGINE_CATALOGUE_FILE_VAR] == ""


@pytest.mark.parametrize("profile", [*PROJECTED_PROFILES, "kafka-filebeat"])
def test_the_catalogue_key_is_written_whatever_the_answer(
    profile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A line that vanished with its own value would make the included makefile
    # re-settle on every pass, which restarts `make` forever.
    values = _resolved(profile, tmp_path, monkeypatch)

    assert resolve_profile.ENGINE_CATALOGUE_FILE_VAR in values
