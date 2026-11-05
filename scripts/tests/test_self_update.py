#  Project:      dfe-docker
#  File:         tests/test_self_update.py
#  Purpose:      Assert the VM updater's update order, version floor, checkout refresh and lock
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The checks on the daemon updater that need no stack and no registry."""

import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest

import _stack_lock
import stack
from _registry import RegistryError, images_version

UPDATER_DIR = Path(__file__).resolve().parents[2] / "ops" / "daemon-update"
if str(UPDATER_DIR) not in sys.path:
    sys.path.insert(0, str(UPDATER_DIR))

import self_update  # noqa: E402

_IMAGE_PINS = {"DFE_ENGINE_VERSION": "DFE_ENGINE_VERSION=v2@sha256:b"}
_IMAGE_TARGET = images_version(pins=_IMAGE_PINS.values(), version="2.2.1")
_OLD_IMAGE_TARGET = "2.2.1+images.000000000000"


class ApplyCase(TypedDict):
    """The wipe flag and the steps an update runs, in order."""

    expected: list[str]
    wipe: str


class ImageReleasesCase(TypedDict):
    """A state file and flag, and what one tick following image releases does."""

    allow_prerelease: str
    applied: str
    dry_run: bool
    expected_applied: list[str]
    expected_log: str
    expected_prereleases: list[str]
    expected_refreshed: bool
    expected_state: str


class IsDowngradeCase(TypedDict):
    """An applied version, a target and whether moving to it is refused."""

    applied: str
    expected: bool
    target: str


class MainCase(TypedDict):
    """A state file, the newest tag and what one tick does about them."""

    applied: str
    dry_run: bool
    expected_applied: list[str]
    expected_log: str
    expected_refreshed: bool
    expected_state: str
    latest: str


class RefreshPlanCase(TypedDict):
    """A checkout's state and the words --dry-run uses for it."""

    dirty: bool
    expected: str


APPLY_CASES = [
    ApplyCase(
        expected=["refresh", "init", "stack VERSION=2.2.0", "ci"],
        wipe="",
    ),
    ApplyCase(
        expected=["refresh", "init", "stack VERSION=2.2.0", "clean", "ci"],
        wipe="1",
    ),
]
IMAGE_RELEASES_CASES = [
    ImageReleasesCase(
        allow_prerelease="",
        applied="",
        dry_run=False,
        expected_applied=["latest"],
        expected_log=f"updated to {_IMAGE_TARGET!r}",
        expected_prereleases=["fallback", "fallback"],
        expected_refreshed=False,
        expected_state=_IMAGE_TARGET,
    ),
    ImageReleasesCase(
        allow_prerelease="",
        applied="2.2.1",
        dry_run=False,
        expected_applied=["latest"],
        expected_log=f"updated to {_IMAGE_TARGET!r}",
        expected_prereleases=["fallback", "fallback"],
        expected_refreshed=False,
        expected_state=_IMAGE_TARGET,
    ),
    ImageReleasesCase(
        allow_prerelease="",
        applied=_OLD_IMAGE_TARGET,
        dry_run=False,
        expected_applied=["latest"],
        expected_log=f"updated to {_IMAGE_TARGET!r}",
        expected_prereleases=["fallback", "fallback"],
        expected_refreshed=False,
        expected_state=_IMAGE_TARGET,
    ),
    ImageReleasesCase(
        allow_prerelease="1",
        applied=_OLD_IMAGE_TARGET,
        dry_run=False,
        expected_applied=["rc"],
        expected_log=f"updated to {_IMAGE_TARGET!r}",
        expected_prereleases=["include", "include"],
        expected_refreshed=False,
        expected_state=_IMAGE_TARGET,
    ),
    ImageReleasesCase(
        allow_prerelease="",
        applied=_IMAGE_TARGET,
        dry_run=False,
        expected_applied=[],
        expected_log="checkout refreshed",
        expected_prereleases=["fallback", "fallback"],
        expected_refreshed=True,
        expected_state=_IMAGE_TARGET,
    ),
    ImageReleasesCase(
        allow_prerelease="",
        applied=_OLD_IMAGE_TARGET,
        dry_run=True,
        expected_applied=[],
        expected_log="dry-run: would update",
        expected_prereleases=["fallback", "fallback"],
        expected_refreshed=False,
        expected_state=_OLD_IMAGE_TARGET,
    ),
]
IS_DOWNGRADE_CASES = [
    IsDowngradeCase(applied="2.2.1", expected=False, target=_IMAGE_TARGET),
    IsDowngradeCase(applied=_IMAGE_TARGET, expected=False, target="2.2.1"),
    IsDowngradeCase(applied="2.2.0-rc.13", expected=True, target="2.2.0-rc.12"),
    IsDowngradeCase(applied="2.2.0-rc.13", expected=False, target="2.2.0-rc.14"),
    IsDowngradeCase(applied="2.2.0-rc.13", expected=False, target="2.2.0-rc.13"),
    IsDowngradeCase(applied="2.2.0", expected=True, target="2.2.0-rc.14"),
    IsDowngradeCase(applied="2.2.0-rc.14", expected=False, target="2.2.0"),
    IsDowngradeCase(applied="2.3.0", expected=True, target="2.2.9"),
    IsDowngradeCase(applied="hand-edited", expected=False, target="2.2.0-rc.12"),
    IsDowngradeCase(applied="2.2.0-rc.13", expected=False, target="not-a-version"),
]
MAIN_CASES = [
    MainCase(
        applied="",
        dry_run=False,
        expected_applied=["2.2.0-rc.14"],
        expected_log="updated to '2.2.0-rc.14'",
        expected_refreshed=False,
        expected_state="2.2.0-rc.14",
        latest="2.2.0-rc.14",
    ),
    MainCase(
        applied="2.2.0-rc.13",
        dry_run=False,
        expected_applied=["2.2.0-rc.14"],
        expected_log="updated to '2.2.0-rc.14'",
        expected_refreshed=False,
        expected_state="2.2.0-rc.14",
        latest="2.2.0-rc.14",
    ),
    MainCase(
        applied="2.2.0-rc.13",
        dry_run=False,
        expected_applied=[],
        expected_log="refusing '2.2.0-rc.13' -> '2.2.0-rc.12'",
        expected_refreshed=False,
        expected_state="2.2.0-rc.13",
        latest="2.2.0-rc.12",
    ),
    MainCase(
        applied="2.2.1-rc.6",
        dry_run=False,
        expected_applied=[],
        expected_log="checkout refreshed",
        expected_refreshed=True,
        expected_state="2.2.1-rc.6",
        latest="2.2.1-rc.6",
    ),
    MainCase(
        applied="2.2.1-rc.6",
        dry_run=True,
        expected_applied=[],
        expected_log="dry-run: already current",
        expected_refreshed=False,
        expected_state="2.2.1-rc.6",
        latest="2.2.1-rc.6",
    ),
    MainCase(
        applied="2.2.0-rc.13",
        dry_run=True,
        expected_applied=[],
        expected_log="dry-run: would update '2.2.0-rc.13' -> '2.2.0-rc.14'",
        expected_refreshed=False,
        expected_state="2.2.0-rc.13",
        latest="2.2.0-rc.14",
    ),
]
REFRESH_PLAN_CASES = [
    RefreshPlanCase(dirty=False, expected="run git pull --ff-only"),
    RefreshPlanCase(dirty=True, expected="refuse"),
]


def _checkout(*, applied: str, tmp_path: Path) -> Path:
    """Make a directory the updater accepts, with a state file when there is a version."""
    (tmp_path / "Makefile").write_text("", encoding="utf-8")
    if applied:
        state = tmp_path / self_update.STATE_FILENAME
        state.write_text(f"{applied}\n", encoding="utf-8")
    return tmp_path


def _discover(*, latest: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the registry and the apply, returning the versions applied."""

    def apply(*, repo_dir: Path, version: str) -> None:
        applied.append(version)

    def latest_tag(*, prereleases: str, repo: str) -> str:
        return latest

    def manifest_repo() -> str:
        return "ghcr.io/test/manifest"

    applied = []
    monkeypatch.delenv(name=stack.REPIN_ENV, raising=False)
    monkeypatch.delenv(name="DFE_UPDATE_ALLOW_PRERELEASE", raising=False)
    monkeypatch.setattr(name="_apply", target=self_update, value=apply)
    monkeypatch.setattr(name="latest_tag", target=self_update, value=latest_tag)
    monkeypatch.setattr(name="manifest_repo", target=self_update, value=manifest_repo)
    return applied


def _git(*args: str, cwd: Path) -> None:
    """Run git with a throwaway identity."""
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        check=True,
        cwd=cwd,
    )


def _main(*, dry_run: bool, monkeypatch: pytest.MonkeyPatch, repo: Path) -> int:
    """Run the updater's entry point against one directory."""
    argv = ["self_update.py"]
    if dry_run:
        argv.append("--dry-run")
    monkeypatch.setattr(name="argv", target=sys, value=argv)
    monkeypatch.setattr(name="_REPO_ROOT", target=self_update, value=repo)
    return self_update.main()


def _state(*, repo: Path) -> str:
    """Return the version the state file records."""
    return (repo / self_update.STATE_FILENAME).read_text(encoding="utf-8").strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clean clone of a bare remote, the way a deployed box holds its checkout."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    target = tmp_path / "clone"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    _git("clone", "-q", str(remote), str(seed), cwd=tmp_path)
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=seed)
    _git("commit", "-qm", "seed", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    _git("clone", "-q", "-b", "main", str(remote), str(target), cwd=tmp_path)
    return target


class TestApply:
    @pytest.mark.parametrize("case", APPLY_CASES)
    def test_matches_expected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: ApplyCase
    ) -> None:
        def make(*, repo_dir: Path, target: list[str]) -> None:
            steps.append(" ".join(target))

        def refresh(*, repo_dir: Path) -> None:
            steps.append("refresh")

        steps = []
        monkeypatch.setenv(name="DFE_UPDATE_WIPE_STATE", value=case["wipe"])
        monkeypatch.setattr(name="_make", target=self_update, value=make)
        monkeypatch.setattr(name="_refresh_checkout", target=self_update, value=refresh)

        self_update._apply(repo_dir=tmp_path, version="2.2.0")

        assert steps == case["expected"]


class TestImageReleases:
    @pytest.mark.parametrize("case", IMAGE_RELEASES_CASES)
    def test_matches_expected(
        self,
        capsys: pytest.CaptureFixture[str],
        clone: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: ImageReleasesCase,
    ) -> None:
        def latest_tag(*, prereleases: str, repo: str) -> str:
            asked.append(prereleases)
            return "2.2.1"

        def pins(*, prereleases: str) -> dict[str, str]:
            asked.append(prereleases)
            return _IMAGE_PINS

        def refresh(*, repo_dir: Path) -> None:
            refreshed.append(repo_dir)

        asked = []
        refreshed = []
        repo = _checkout(applied=case["applied"], tmp_path=clone)
        applied = _discover(latest="unused", monkeypatch=monkeypatch)
        monkeypatch.setenv(name=stack.REPIN_ENV, value="1")
        monkeypatch.setenv(
            name="DFE_UPDATE_ALLOW_PRERELEASE", value=case["allow_prerelease"]
        )
        monkeypatch.setattr(name="latest_tag", target=self_update, value=latest_tag)
        monkeypatch.setattr(name="_latest_image_pins", target=stack, value=pins)
        monkeypatch.setattr(name="_refresh_checkout", target=self_update, value=refresh)

        code = _main(dry_run=case["dry_run"], monkeypatch=monkeypatch, repo=repo)

        assert code == 0
        assert applied == case["expected_applied"]
        assert asked == case["expected_prereleases"]
        assert bool(refreshed) is case["expected_refreshed"]
        assert case["expected_log"] in capsys.readouterr().out
        assert _state(repo=repo) == case["expected_state"]

    def test_a_failed_apply_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fail(*, repo_dir: Path, version: str) -> None:
            raise self_update.UpdateError("'make ci' failed (exit 2)")

        def pins(*, prereleases: str) -> dict[str, str]:
            return _IMAGE_PINS

        repo = _checkout(applied=_OLD_IMAGE_TARGET, tmp_path=tmp_path)
        _discover(latest="2.2.1", monkeypatch=monkeypatch)
        monkeypatch.setenv(name=stack.REPIN_ENV, value="1")
        monkeypatch.setattr(name="_apply", target=self_update, value=fail)
        monkeypatch.setattr(name="_latest_image_pins", target=stack, value=pins)

        assert _main(dry_run=False, monkeypatch=monkeypatch, repo=repo) == 1

        assert _state(repo=repo) == _OLD_IMAGE_TARGET

    def test_a_failed_release_lookup_fails_the_tick(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def pins(*, prereleases: str) -> dict[str, str]:
            raise RegistryError("GitHub answered 403")

        repo = _checkout(applied=_OLD_IMAGE_TARGET, tmp_path=tmp_path)
        applied = _discover(latest="2.2.1", monkeypatch=monkeypatch)
        monkeypatch.setenv(name=stack.REPIN_ENV, value="1")
        monkeypatch.setattr(name="_latest_image_pins", target=stack, value=pins)

        assert _main(dry_run=False, monkeypatch=monkeypatch, repo=repo) == 1

        assert applied == []
        assert _state(repo=repo) == _OLD_IMAGE_TARGET


class TestIsDowngrade:
    @pytest.mark.parametrize("case", IS_DOWNGRADE_CASES)
    def test_matches_expected(self, case: IsDowngradeCase) -> None:
        downgrade = self_update._is_downgrade(
            applied=case["applied"], target=case["target"]
        )

        assert downgrade is case["expected"]


class TestMake:
    def test_output_reaches_the_journal_uncaptured(
        self, capfd: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        (tmp_path / "Makefile").write_text("say:\n\t@echo live\n", encoding="utf-8")

        self_update._make(repo_dir=tmp_path, target=["say"])

        assert "live" in capfd.readouterr().out

    def test_a_failed_target_raises(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("fail:\n\t@exit 3\n", encoding="utf-8")

        with pytest.raises(self_update.UpdateError, match="'make fail' failed"):
            self_update._make(repo_dir=tmp_path, target=["fail"])


class TestRefreshCheckout:
    def test_a_clean_checkout_fast_forwards(self, clone: Path, tmp_path: Path) -> None:
        seed = tmp_path / "seed"
        (seed / "new.txt").write_text("two\n", encoding="utf-8")
        _git("add", "new.txt", cwd=seed)
        _git("commit", "-qm", "next", cwd=seed)
        _git("push", "-q", "origin", "HEAD:main", cwd=seed)

        self_update._refresh_checkout(repo_dir=clone)

        assert (clone / "new.txt").is_file()

    def test_a_dirty_checkout_is_refused(self, clone: Path) -> None:
        (clone / "tracked.txt").write_text("edited\n", encoding="utf-8")

        with pytest.raises(self_update.UpdateError, match="uncommitted changes"):
            self_update._refresh_checkout(repo_dir=clone)

    def test_an_unreachable_remote_fails_the_tick(
        self, clone: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "remote.git").rename(tmp_path / "gone.git")

        with pytest.raises(self_update.UpdateError, match="git pull --ff-only failed"):
            self_update._refresh_checkout(repo_dir=clone)

    def test_a_directory_with_no_git_is_left_alone(self, tmp_path: Path) -> None:
        self_update._refresh_checkout(repo_dir=tmp_path)

        assert list(tmp_path.iterdir()) == []


class TestRefreshPlan:
    @pytest.mark.parametrize("case", REFRESH_PLAN_CASES)
    def test_matches_expected(self, clone: Path, case: RefreshPlanCase) -> None:
        if case["dirty"]:
            (clone / "tracked.txt").write_text("edited\n", encoding="utf-8")

        plan = self_update._refresh_plan(repo_dir=clone)

        assert plan.startswith(case["expected"])

    def test_a_directory_with_no_git_is_skipped(self, tmp_path: Path) -> None:
        plan = self_update._refresh_plan(repo_dir=tmp_path)

        assert plan.startswith("be skipped")


class TestMain:
    @pytest.mark.parametrize("case", MAIN_CASES)
    def test_matches_expected(
        self,
        capsys: pytest.CaptureFixture[str],
        clone: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: MainCase,
    ) -> None:
        def refresh(*, repo_dir: Path) -> None:
            refreshed.append(repo_dir)

        refreshed = []
        repo = _checkout(applied=case["applied"], tmp_path=clone)
        applied = _discover(latest=case["latest"], monkeypatch=monkeypatch)
        monkeypatch.setattr(name="_refresh_checkout", target=self_update, value=refresh)

        code = _main(dry_run=case["dry_run"], monkeypatch=monkeypatch, repo=repo)

        assert code == 0
        assert applied == case["expected_applied"]
        assert bool(refreshed) is case["expected_refreshed"]
        assert case["expected_log"] in capsys.readouterr().out
        assert _state(repo=repo) == case["expected_state"]

    def test_a_refused_refresh_fails_the_tick(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def refuse(*, repo_dir: Path) -> None:
            raise self_update.UpdateError("the checkout has uncommitted changes")

        repo = _checkout(applied="2.2.1-rc.6", tmp_path=tmp_path)
        _discover(latest="2.2.1-rc.6", monkeypatch=monkeypatch)
        monkeypatch.setattr(name="_refresh_checkout", target=self_update, value=refuse)

        assert _main(dry_run=False, monkeypatch=monkeypatch, repo=repo) == 1

    def test_a_tick_skips_while_a_proof_holds_the_stack_lock(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
        applied = _discover(latest="2.2.0-rc.14", monkeypatch=monkeypatch)

        with _stack_lock.stack_lock(directory=repo) as proof_holds_it:
            assert proof_holds_it
            assert _main(dry_run=False, monkeypatch=monkeypatch, repo=repo) == 0

        assert applied == []
        assert _state(repo=repo) == "2.2.0-rc.13"
        assert "skipping this tick" in capsys.readouterr().out

    def test_the_stack_lock_is_held_while_an_update_applies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def probing_apply(*, repo_dir: Path, version: str) -> None:
            with _stack_lock.stack_lock(directory=repo_dir) as other:
                free_during_apply.append(other)

        free_during_apply = []
        repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
        _discover(latest="2.2.0-rc.14", monkeypatch=monkeypatch)
        monkeypatch.setattr(name="_apply", target=self_update, value=probing_apply)

        assert _main(dry_run=False, monkeypatch=monkeypatch, repo=repo) == 0

        assert free_during_apply == [False]
        with _stack_lock.stack_lock(directory=repo) as after:
            assert after

    def test_a_dry_run_ignores_the_stack_lock(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        repo = _checkout(applied="2.2.0-rc.13", tmp_path=tmp_path)
        _discover(latest="2.2.0-rc.14", monkeypatch=monkeypatch)

        with _stack_lock.stack_lock(directory=repo):
            assert _main(dry_run=True, monkeypatch=monkeypatch, repo=repo) == 0

        assert "dry-run: would update" in capsys.readouterr().out
