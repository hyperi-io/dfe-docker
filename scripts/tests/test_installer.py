#  Project:      dfe-docker
#  File:         scripts/tests/test_installer.py
#  Purpose:      Assert the dfe-docker image's entrypoint refuses what it must and leaves .env a plain `docker compose` starts the profile from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on scripts/installer.py.

The block tests and the refusals need nothing installed. The apply tests run the folder's own copy on a copy of the tree, with placeholder pins in .env, through make and `docker compose config`, so they skip where either is missing. They prove what the installer proves before it writes: that a plain `docker compose` started with nothing but .env names exactly the services `make ci` starts.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import installer
from _common import COMPOSE_FILE, COMPOSE_ONE_SHOTS_FILE, _required_compose_vars

REPO_ROOT = Path(__file__).resolve().parents[2]
# What the image carries, as .dockerignore allows it
_TREE = (
    ".env.example",
    "Makefile",
    "compose",
    "config",
    "deployment.example.yaml",
    "docker-compose.yml",
    "env.example",
    "redpanda",
    "resources",
    "scripts",
    "service_profiles.yaml",
)
_PLACEHOLDER = "0.0.0-placeholder"


def test_the_block_goes_at_the_end_and_comes_out_whole() -> None:
    settings = "DFE_PROFILE=slim-grpc\nCLICKHOUSE_PASSWORD=x\n"

    written = installer.with_block(lines=["COMPOSE_PROFILES=core"], text=settings)
    rewritten = installer.with_block(lines=["COMPOSE_PROFILES=dfe"], text=written)

    assert written.startswith(settings)
    assert written.count(installer.BLOCK_BEGIN) == 1
    assert rewritten.count(installer.BLOCK_BEGIN) == 1
    assert "COMPOSE_PROFILES=core" not in rewritten
    assert installer.strip_block(text=rewritten) == settings


def test_a_line_after_the_block_survives_a_rewrite() -> None:
    """init.py appends a secret a template gained, which lands after the block."""
    written = installer.with_block(lines=["A=1"], text="DFE_PROFILE=x\n")

    kept = installer.strip_block(text=written + "NEXTAUTH_SECRET=minted\n")

    assert kept == "DFE_PROFILE=x\nNEXTAUTH_SECRET=minted\n"


def test_a_block_with_no_end_is_refused_rather_than_cut_to_the_end() -> None:
    text = f"DFE_PROFILE=x\n{installer.BLOCK_BEGIN}\nA=1\nCLICKHOUSE_PASSWORD=x\n"

    with pytest.raises(installer.InstallerError, match="Delete the block"):
        installer.strip_block(text=text)


@pytest.mark.parametrize(
    ("state", "upgrade", "said"),
    [
        pytest.param("empty", False, "", id="empty"),
        pytest.param("install", True, "", id="upgrade"),
        pytest.param("install", False, "init --upgrade", id="install-again"),
        pytest.param("empty", True, "no install here", id="upgrade-nothing"),
        pytest.param("other", False, "not empty", id="not-empty"),
        pytest.param("other", True, "no install here", id="upgrade-other"),
    ],
)
def test_init_refuses_a_folder_it_would_overwrite(
    said: str, state: str, upgrade: bool
) -> None:
    reason = installer.refusal(image="dfe-docker:test", state=state, upgrade=upgrade)

    assert (said in reason) if said else (reason == "")


@pytest.mark.parametrize(
    ("names", "state"),
    [
        pytest.param([], "empty", id="nothing"),
        pytest.param(
            [".DS_Store", "Thumbs.db", "desktop.ini"], "empty", id="file-manager"
        ),
        pytest.param(["notes.txt"], "other", id="a-file"),
        pytest.param([".DS_Store", ".env"], "install", id="env"),
        pytest.param(["docker-compose.yml"], "install", id="compose"),
    ],
)
def test_the_folder_reads_as_what_it_holds(
    names: list[str], state: str, tmp_path: Path
) -> None:
    for name in names:
        (tmp_path / name).write_text("", encoding="utf-8")

    assert installer.folder_state(target=tmp_path) == state


@pytest.mark.parametrize("name", ["", "single-kafka", "slim-grpc"])
def test_a_profile_the_tree_declares_is_taken(name: str) -> None:
    assert installer.profile_refusal(name=name, profiles=installer._profiles()) == ""


@pytest.mark.parametrize("name", ["single", "kafka-full", "SINGLE-KAFKA", "x"])
def test_an_unknown_profile_is_refused_naming_every_valid_one(name: str) -> None:
    profiles = installer._profiles()

    reason = installer.profile_refusal(name=name, profiles=profiles)

    assert repr(name) in reason
    assert all(profile in reason for profile in profiles)


@pytest.mark.parametrize(
    ("point", "path", "found"),
    [
        pytest.param("/dfe", "/dfe", True, id="mounted"),
        pytest.param("/home/a\\040b/dfe", "/home/a b/dfe", True, id="escaped-space"),
        pytest.param("/dfe/sub", "/dfe", False, id="only-below"),
        pytest.param("/", "/dfe", False, id="root-only"),
    ],
)
def test_the_mount_check_reads_mountinfo_as_the_kernel_writes_it(
    found: bool, path: str, point: str
) -> None:
    mountinfo = f"1 0 0:1 / / rw - overlay overlay rw\n2 1 8:1 /x {point} rw - ext4 /dev/sda1 rw\n"

    assert installer.mounted(mountinfo=mountinfo, path=Path(path)) == found


def test_the_printed_settings_read_back_as_written() -> None:
    printed = "COMPOSE_FILE=docker-compose.yml:.tmp/x.yml\nCOMPOSE_PROFILES=clickhouse,otel\nDFE_DEV_UID=1000\nnoise\n"

    assert installer.parse_settings(text=printed) == {
        "COMPOSE_FILE": "docker-compose.yml:.tmp/x.yml",
        "COMPOSE_PROFILES": "clickhouse,otel",
        "DFE_DEV_UID": "1000",
    }


@pytest.mark.parametrize(
    ("settings", "url"),
    [
        pytest.param({}, "http://clickhouse:8123", id="bundled"),
        pytest.param(
            {"CLICKHOUSE_HTTP_PORT": "36003"},
            "http://clickhouse:8123",
            id="host-publish-moved",
        ),
        pytest.param(
            {
                "CLICKHOUSE_HOST": "ch.example.test",
                "CLICKHOUSE_EXTERNAL_HTTP_PORT": "8443",
            },
            "http://ch.example.test:8443",
            id="external",
        ),
        pytest.param(
            {"CLICKHOUSE_HOST": " "}, "http://clickhouse:8123", id="blank-host"
        ),
    ],
)
def test_the_demo_reads_clickhouse_where_the_stack_dials_it(
    settings: dict[str, str], url: str
) -> None:
    assert installer.in_stack_clickhouse(settings=settings) == url


def test_every_command_the_image_prints_names_the_image() -> None:
    image = "ghcr.io/hyperi-io/dfe-docker:9.9.9"

    for command in (
        installer.run_command(image=image, args=["init"]),
        installer.login_command(image=image),
        installer.demo_command(image=image),
    ):
        assert command.startswith("docker run --rm ")
        assert f" {image} " in command


def _tree(*, root: Path) -> Path:
    """A copy of what the image carries, with .env from its template and every pin a placeholder."""
    root.mkdir()
    for name in _TREE:
        source = REPO_ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                root / name,
                ignore=shutil.ignore_patterns("tests", "__pycache__"),
            )
        else:
            shutil.copy2(source, root / name)
    pins = sorted(_required_compose_vars(files=(COMPOSE_FILE, COMPOSE_ONE_SHOTS_FILE)))
    template = (root / ".env.example").read_text(encoding="utf-8")
    (root / ".env").write_text(
        template + "".join(f"{key}={_PLACEHOLDER}\n" for key in pins),
        encoding="utf-8",
        newline="\n",
    )
    return root


def _apply(*, root: Path, profile: str) -> subprocess.CompletedProcess[str]:
    """Run the folder's own copy of the installer's `apply` with this profile in .env."""
    with (root / ".env").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"DFE_PROFILE={profile}\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith(("COMPOSE_", "DFE_", "MAKE"))) and key != "MFLAGS"
    }
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "installer.py"), "apply"],
        capture_output=True,
        check=False,
        cwd=root,
        encoding="utf-8",
        env={**environment, installer.PHASE_KEY: installer.WORK_PHASE},
        errors="replace",
        stdin=subprocess.DEVNULL,
        text=True,
    )


def _block(*, root: Path) -> dict[str, str]:
    """The KEY=value lines inside the block in .env."""
    lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    start = lines.index(installer.BLOCK_BEGIN)
    end = lines.index(installer.BLOCK_END)
    return installer.parse_settings(text="\n".join(lines[start + 1 : end]))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    if shutil.which("make") is None or shutil.which("docker") is None:
        pytest.skip("apply runs make and `docker compose config`")
    return _tree(root=tmp_path / "dfe")


@pytest.mark.parametrize(
    ("profile", "parked"),
    [
        pytest.param(
            "single-kafka",
            {
                "dfe-fetcher",
                "dfe-transform-elastic",
                "dfe-transform-vector",
                "dfe-transform-vrl",
            },
            id="single-kafka",
        ),
        pytest.param(
            "slim-kafka",
            {"dfe-archiver", "config-check-dfe-archiver", "dfe-fetcher"},
            id="slim-kafka",
        ),
        pytest.param(
            "single-grpc",
            {"dfe-archiver", "config-check-dfe-archiver", "dfe-transform-vrl"},
            id="single-grpc",
        ),
        pytest.param(
            "slim-grpc",
            {"dfe-archiver", "dfe-fetcher", "dfe-transform-elastic"},
            id="slim-grpc",
        ),
    ],
)
def test_apply_leaves_a_plain_compose_starting_only_the_profile(
    parked: set[str], profile: str, tree: Path
) -> None:
    result = _apply(root=tree, profile=profile)

    assert result.returncode == 0, result.stdout + result.stderr
    block = _block(root=tree)
    assert block["COMPOSE_FILE"].split(":")[0] == "docker-compose.yml"
    assert block["COMPOSE_FILE"].endswith(installer.OVERLAY.as_posix())
    assert block["COMPOSE_PATH_SEPARATOR"] == ":"
    overlay = (tree / installer.OVERLAY).read_text(encoding="utf-8")
    for service in parked:
        assert f"  {service}:\n" in overlay
    assert "  dfe-receiver:\n" not in overlay
    assert "  dfe-loader:\n" not in overlay


def test_a_second_apply_rewrites_the_block_in_place(tree: Path) -> None:
    first = _apply(root=tree, profile="single-kafka")
    second = _apply(root=tree, profile="slim-grpc")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    text = (tree / ".env").read_text(encoding="utf-8")
    assert text.count(installer.BLOCK_BEGIN) == 1
    assert "kafka-redpanda" not in _block(root=tree)["COMPOSE_PROFILES"]


def test_apply_puts_env_back_when_make_refuses_the_settings(tree: Path) -> None:
    with (tree / ".env").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("DFE_BIND_SCOPE=all\n")
    before = (tree / ".env").read_text(encoding="utf-8")

    result = _apply(root=tree, profile="single-kafka")

    assert result.returncode == 1
    assert "DFE_EXTERNAL_ORIGIN" in result.stderr
    assert (tree / ".env").read_text(
        encoding="utf-8"
    ) == before + "DFE_PROFILE=single-kafka\n"
