#  Project:      dfe-docker
#  File:         tests/test_stack.py
#  Purpose:      Assert VERSION=latest needs no GitHub access, and the opt-in repin covers only our images
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""`make stack VERSION=latest` pins the newest published stack from the public registry alone.

It runs end to end here with nothing on PATH but a stand-in `oras` -- no `gh`, no token -- because that is all an outsider has.

The opt-in repin (`DFE_STACK_REPIN_IMAGES=1`) repins the DFE images on top of that stack. Which keys that covers is DERIVED from docker-compose.yml rather than listed, so the test runs against the real compose file: a service added to the stack has to turn up here without anyone editing a list, and a third-party image must never turn up at all -- floating ClickHouse or Kafka is the thing this mode is not.

The repin itself is asserted against fixed releases and digests, so what is pinned is the shape of the line written into `.env` and the source each version comes from, not GitHub's or GHCR's current contents.

`--print KEY` is how `make check-proxy` finds the Envoy on a checkout with no `.env`, so it runs on the same stand-in `oras` and must leave `.env` as it found it.
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

import _registry
import stack

# Every image published to ghcr.io/hyperi-io that docker-compose.yml tags with a
# pin key. A new DFE service is expected to break this and be added.
_OUR_KEYS = {
    "DFE_ARCHIVER_VERSION",
    "DFE_ENGINE_VERSION",
    "DFE_FETCHER_VERSION",
    "DFE_HYPERDX_VERSION",
    "DFE_LOADER_VERSION",
    "DFE_RECEIVER_VERSION",
    "DFE_TRANSFORM_ELASTIC_VERSION",
    "DFE_TRANSFORM_VECTOR_VERSION",
    "DFE_TRANSFORM_VRL_VERSION",
    "DFE_UI_VERSION",
}

# Third-party pins the certified stack owns. Floating any of these is what the
# scope decision ruled out.
_THIRD_PARTY_KEYS = {
    "APACHE_KAFKA_VERSION",
    "CLICKHOUSE_VERSION",
    "DFE_OTEL_COLLECTOR_VERSION",
    "DFE_PROXY_VERSION",
    "HYPERDX_DOCUMENTDB_VERSION",
    "KAFBAT_VERSION",
    "REDPANDA_VERSION",
}


def test_the_repo_map_covers_every_image_we_publish() -> None:
    assert set(stack._our_image_repos()) == _OUR_KEYS


def test_the_repo_map_excludes_every_third_party_image() -> None:
    assert _THIRD_PARTY_KEYS.isdisjoint(stack._our_image_repos())


def test_a_key_maps_to_its_own_ghcr_repo() -> None:
    repos = stack._our_image_repos()

    assert repos["DFE_ENGINE_VERSION"] == "ghcr.io/hyperi-io/dfe-engine"
    assert repos["DFE_TRANSFORM_VRL_VERSION"] == "ghcr.io/hyperi-io/dfe-transform-vrl"


def test_an_image_registry_override_repoints_every_repo(dotenv: Path) -> None:
    dotenv.write_text("IMAGE_REGISTRY=registry.local/mirror\n", encoding="utf-8")

    repos = stack._our_image_repos()

    assert repos["DFE_ENGINE_VERSION"] == "registry.local/mirror/dfe-engine"
    assert repos["DFE_UI_VERSION"] == "registry.local/mirror/dfe-ui"


def test_each_image_is_released_by_the_github_repo_of_the_same_name() -> None:
    sources = stack._our_release_repos()

    assert set(sources) == _OUR_KEYS
    assert sources["DFE_ENGINE_VERSION"] == "hyperi-io/dfe-engine"
    assert sources["DFE_HYPERDX_VERSION"] == "hyperi-io/dfe-hyperdx"


def test_a_mirror_moves_the_image_and_not_its_release_source(dotenv: Path) -> None:
    dotenv.write_text("IMAGE_REGISTRY=registry.local/mirror\n", encoding="utf-8")

    assert stack._our_image_repos()["DFE_UI_VERSION"] == "registry.local/mirror/dfe-ui"
    assert stack._our_release_repos()["DFE_UI_VERSION"] == "hyperi-io/dfe-ui"


def test_a_repin_takes_the_release_and_never_the_highest_registry_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dfe-hyperdx's registry carries a v1.0.0 that outranks its Latest release, v0.2.7."""
    asked: dict[str, str] = {}

    def _latest_tag(*, prereleases: str = "exclude", repo: str) -> str:
        return "v1.0.0"

    def _latest_release(*, prereleases: str = "exclude", repo: str) -> str:
        asked[repo] = prereleases
        return "v0.2.7"

    def _resolve_digest(*, reference: str) -> str:
        assert reference.endswith(":v0.2.7"), reference
        return "sha256:" + "c" * 64

    monkeypatch.setattr(stack, "latest_tag", _latest_tag)
    monkeypatch.setattr(stack, "latest_release", _latest_release)
    monkeypatch.setattr(stack, "resolve_digest", _resolve_digest)

    pins = stack._latest_image_pins(prereleases="fallback")

    assert pins["DFE_HYPERDX_VERSION"].startswith("DFE_HYPERDX_VERSION=v0.2.7@sha256:")
    assert asked["hyperi-io/dfe-hyperdx"] == "fallback"
    assert set(asked) == set(stack._our_release_repos().values())


def test_a_repin_line_carries_the_tag_the_digest_and_both_repos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _latest_release(*, prereleases: str = "exclude", repo: str) -> str:
        return "v1.19.36"

    def _resolve_digest(*, reference: str) -> str:
        return "sha256:" + "a" * 64

    monkeypatch.setattr(stack, "latest_release", _latest_release)
    monkeypatch.setattr(stack, "resolve_digest", _resolve_digest)

    pins = stack._latest_image_pins(prereleases="fallback")

    assert set(pins) == _OUR_KEYS
    assert pins["DFE_ENGINE_VERSION"] == (
        f"DFE_ENGINE_VERSION=v1.19.36@sha256:{'a' * 64}"
        "  # ghcr.io/hyperi-io/dfe-engine, newest release of hyperi-io/dfe-engine"
    )


def test_a_repin_never_writes_a_floating_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    def _latest_release(*, prereleases: str = "exclude", repo: str) -> str:
        return "v1.19.36"

    def _resolve_digest(*, reference: str) -> str:
        return "sha256:" + "b" * 64

    monkeypatch.setattr(stack, "latest_release", _latest_release)
    monkeypatch.setattr(stack, "resolve_digest", _resolve_digest)

    for line in stack._latest_image_pins(prereleases="fallback").values():
        value = line.split("=", 1)[1].split("  #", 1)[0]
        assert "@sha256:" in value
        assert not value.startswith("latest")


def test_a_pin_with_no_digest_is_refused() -> None:
    """A bare tag is what the render emits when versions.yaml has no digests entry."""
    pins = {
        "DFE_ENGINE_VERSION": f"DFE_ENGINE_VERSION=v1.21.2@sha256:{'a' * 64}",
        "DFE_HYPERDX_VERSION": "DFE_HYPERDX_VERSION=v2.3.1  # no digests entry",
    }

    with pytest.raises(stack.StackError, match="DFE_HYPERDX_VERSION"):
        stack._refuse_undigested(pins)


def test_a_fully_digested_set_passes() -> None:
    pins = {
        "DFE_ENGINE_VERSION": f"DFE_ENGINE_VERSION=v1.21.2@sha256:{'a' * 64}",
        "CLICKHOUSE_VERSION": f"CLICKHOUSE_VERSION=25.8@sha256:{'b' * 64}  # certified",
    }

    stack._refuse_undigested(pins)


def test_the_stack_marker_is_not_read_as_an_image_pin() -> None:
    """It names the stack these pins came from, so it carries no digest."""
    stack._refuse_undigested({stack.STACK_VERSION_KEY: "DFE_STACK_VERSION=2.2.0-rc.14"})


def test_the_discovery_words_are_the_two_the_makefile_documents() -> None:
    assert _registry.DISCOVERY_WORDS == {"latest": "fallback", "rc": "include"}
    assert set(_registry.DISCOVERY_WORDS.values()) <= set(_registry.PRERELEASES)


# Stand-ins for `oras` and `cosign` at the process boundary. `oras` answers
# `repo tags`, `resolve` and `pull` from fixed data, and pulls by digest only.
# `cosign` passes a verify unless the test has written a refusal. Each logs
# every argv to one shared log, and refuses any other call.
_FAKE_ORAS = """#!{python}
import hashlib, json, pathlib, sys
args = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(["oras", *args]) + "\\n")
tags = pathlib.Path({tags!r})
if args[:2] == ["repo", "tags"]:
    print(tags.read_text(encoding="utf-8"))
elif args[:1] == ["resolve"]:
    tag = args[-1].rsplit(":", 1)[1]
    print("sha256:" + hashlib.sha256(tag.encode()).hexdigest())
elif args[:1] == ["pull"] and "@sha256:" in args[-1]:
    digest = args[-1].rsplit("@", 1)[1]
    names = [t for t in tags.read_text(encoding="utf-8").split() if not t.startswith("sha256-")]
    version = next(t for t in names if "sha256:" + hashlib.sha256(t.encode()).hexdigest() == digest)
    out = pathlib.Path(args[args.index("--output") + 1])
    fragment = pathlib.Path({fragment!r}).read_text(encoding="utf-8")
    (out / f"dfe-env-{{version}}.txt").write_text(fragment, encoding="utf-8")
else:
    sys.exit(2)
"""

# `docker` stands in the same way, accepting only a `run` of the pinned cosign image.
_FAKE_COSIGN = """#!{python}
import json, pathlib, sys
args = sys.argv[1:]
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps([{tool!r}, *args]) + "\\n")
refusal = pathlib.Path({refusal!r})
lead = {lead!r}
if args[: len(lead)] != lead or args[len(lead) : len(lead) + 1] != ["verify"]:
    sys.exit(2)
if refusal.exists():
    print(refusal.read_text(encoding="utf-8"), file=sys.stderr)
    sys.exit(1)
"""


def _digest_of(tag: str) -> str:
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


# The stack-manifest's real tag shape: semver beside `sha256-<digest>` tags.
_MANIFEST_TAGS = "2.1.0\n2.2.0-rc.12\nsha256-99ba56dfc0cc\n2.2.0-rc.13\n"

_FRAGMENT = (
    "# rendered for docker\n"
    f"DFE_ENGINE_VERSION=v1.22.8@sha256:{'a' * 64}  # ghcr.io/hyperi-io/dfe-engine\n"
    f"CLICKHOUSE_VERSION=25.8@sha256:{'b' * 64}\n"
)


@pytest.fixture
def anonymous_oras(
    tmp_path: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A PATH holding only a fake `oras` and `cosign`, no `gh`, no GitHub token; returns their call log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "oras-calls.jsonl"
    (tmp_path / "tags.txt").write_text(_MANIFEST_TAGS, encoding="utf-8")
    (tmp_path / "fragment.txt").write_text(_FRAGMENT, encoding="utf-8")
    stand_ins = {
        "oras": _FAKE_ORAS.format(
            python=sys.executable,
            log=str(log),
            tags=str(tmp_path / "tags.txt"),
            fragment=str(tmp_path / "fragment.txt"),
        ),
        "cosign": _FAKE_COSIGN.format(
            python=sys.executable,
            lead=[],
            log=str(log),
            refusal=str(tmp_path / "cosign-refuses.txt"),
            tool="cosign",
        ),
    }
    for name, script in stand_ins.items():
        path = bindir / name
        path.write_text(script, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "DFE_INFRA_DIR",
        "DFE_STACK_MANIFEST_REPO",
        "DFE_STACK_ENV_MEMBER",
        stack.VERIFY_ENV,
        stack.SIGNER_IDENTITY_ENV,
        stack.SIGNER_ISSUER_ENV,
        stack.REPIN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    dotenv.write_text("HTTP_PORT=8080\n", encoding="utf-8")
    monkeypatch.setattr(stack, "DOTENV_FILE", dotenv)
    return log


@pytest.mark.parametrize(
    ("word", "expected"), [("latest", "2.1.0"), ("rc", "2.2.0-rc.13")]
)
def test_a_discovery_word_resolves_with_no_gh_and_no_token(
    word: str,
    expected: str,
    anonymous_oras: Path,
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outsider has no GitHub access to the component repos, only the public registry."""
    assert shutil.which("gh") is None
    monkeypatch.setattr(sys, "argv", ["stack.py", word])

    assert stack.main() == 0

    log = anonymous_oras.read_text(encoding="utf-8")
    calls = [json.loads(line) for line in log.splitlines()]
    pinned = f"{_registry.DEFAULT_MANIFEST_REPO}@{_digest_of(expected)}"
    assert [call[:3] for call in calls] == [
        ["oras", "repo", "tags"],
        ["oras", "resolve", f"{_registry.DEFAULT_MANIFEST_REPO}:{expected}"],
        ["cosign", "verify", "--certificate-identity"],
        ["oras", "pull", "--output"],
    ]
    assert calls[2][-1] == pinned
    assert calls[3][-1] == pinned
    written = dotenv.read_text(encoding="utf-8")
    assert f"DFE_ENGINE_VERSION=v1.22.8@sha256:{'a' * 64}" in written
    assert f"DFE_STACK_VERSION={word}  # resolved {expected}\n" in written
    assert "HTTP_PORT=8080" in written


def test_the_github_release_repin_runs_only_when_asked(
    anonymous_oras: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repin needs `gh` read access to every component repo, so it is opt-in."""
    asked: list[str] = []

    def _latest_image_pins(*, prereleases: str) -> dict[str, str]:
        asked.append(prereleases)
        return {"DFE_ENGINE_VERSION": f"DFE_ENGINE_VERSION=v9.9.9@sha256:{'c' * 64}"}

    monkeypatch.setattr(stack, "_latest_image_pins", _latest_image_pins)
    monkeypatch.setattr(sys, "argv", ["stack.py", "latest"])

    assert stack.main() == 0
    assert asked == []

    monkeypatch.setenv(stack.REPIN_ENV, "1")
    assert stack.main() == 0
    assert asked == ["fallback"]


def test_an_explicit_version_never_repins(
    anonymous_oras: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned version is the certified set, whatever the opt-in says."""

    def _latest_image_pins(*, prereleases: str) -> dict[str, str]:
        raise AssertionError("an explicit version must not repin")

    monkeypatch.setattr(stack, "_latest_image_pins", _latest_image_pins)
    monkeypatch.setenv(stack.REPIN_ENV, "1")
    monkeypatch.setattr(sys, "argv", ["stack.py", "2.2.0-rc.13"])

    assert stack.main() == 0


def test_print_writes_one_pin_value_and_leaves_env_alone(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        name="argv",
        target=sys,
        value=["stack.py", "--print", "DFE_ENGINE_VERSION", "2.2.0-rc.13"],
    )

    assert stack.main() == 0

    assert capsys.readouterr().out == f"v1.22.8@sha256:{'a' * 64}\n"
    log = anonymous_oras.read_text(encoding="utf-8")
    calls = [json.loads(s=line) for line in log.splitlines()]
    pinned = f"{_registry.DEFAULT_MANIFEST_REPO}@{_digest_of('2.2.0-rc.13')}"
    assert [call[-1] for call in calls] == [
        f"{_registry.DEFAULT_MANIFEST_REPO}:2.2.0-rc.13",
        pinned,
        pinned,
    ]
    assert dotenv.read_text(encoding="utf-8") == "HTTP_PORT=8080\n"


def test_print_with_no_version_reads_the_stack_the_example_dial_pins(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dial = tmp_path / "deployment.example.yaml"
    dial.write_text(data="version:\n  pin: 2.1.0  # certified\n", encoding="utf-8")
    monkeypatch.setattr(name="DEPLOYMENT_DIAL_TEMPLATE", target=stack, value=dial)
    monkeypatch.setattr(
        name="argv", target=sys, value=["stack.py", "--print", "CLICKHOUSE_VERSION"]
    )

    assert stack.main() == 0

    assert capsys.readouterr().out == f"25.8@sha256:{'b' * 64}\n"
    log = anonymous_oras.read_text(encoding="utf-8")
    calls = [json.loads(s=line) for line in log.splitlines()]
    pinned = f"{_registry.DEFAULT_MANIFEST_REPO}@{_digest_of('2.1.0')}"
    assert [call[-1] for call in calls] == [
        f"{_registry.DEFAULT_MANIFEST_REPO}:2.1.0",
        pinned,
        pinned,
    ]


def test_print_refuses_a_key_the_stack_does_not_pin(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        name="argv",
        target=sys,
        value=["stack.py", "--print", "DFE_PROXY_VERSION", "2.2.0-rc.13"],
    )

    assert stack.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pins no 'DFE_PROXY_VERSION'" in captured.err


def test_the_committed_dial_pins_a_stack_version() -> None:
    assert re.fullmatch(
        pattern=r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", string=stack._dial_pin()
    )


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


# How each runner starts cosign: the one on PATH, else the pinned image through docker.
_RUNNERS = {
    "path": ["cosign"],
    "container": ["docker", "run", "--rm", stack.COSIGN_IMAGE],
}


def _use_runner(*, runner: str, tmp_path: Path) -> None:
    """Leave cosign on PATH, or swap it for a docker that runs only the pinned image."""
    if runner == "path":
        return
    bindir = tmp_path / "bin"
    (bindir / "cosign").unlink()
    docker = bindir / "docker"
    docker.write_text(
        _FAKE_COSIGN.format(
            python=sys.executable,
            lead=_RUNNERS["container"][1:],
            log=str(tmp_path / "oras-calls.jsonl"),
            refusal=str(tmp_path / "cosign-refuses.txt"),
            tool="docker",
        ),
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)


@pytest.mark.parametrize("runner", sorted(_RUNNERS))
def test_the_manifest_is_verified_against_the_release_workflow_identity(
    anonymous_oras: Path, monkeypatch: pytest.MonkeyPatch, runner: str, tmp_path: Path
) -> None:
    """A signature by anyone else, or a re-pointed tag, must not become the pins, whichever runner checks it."""
    _use_runner(runner=runner, tmp_path=tmp_path)
    monkeypatch.setattr(sys, "argv", ["stack.py", "2.2.0-rc.13"])

    assert stack.main() == 0

    lead = _RUNNERS[runner]
    verify = next(call for call in _calls(anonymous_oras) if call[0] == lead[0])
    assert verify == [
        *lead,
        "verify",
        "--certificate-identity",
        stack.DEFAULT_SIGNER_IDENTITY,
        "--certificate-oidc-issuer",
        stack.DEFAULT_SIGNER_ISSUER,
        f"{_registry.DEFAULT_MANIFEST_REPO}@{_digest_of('2.2.0-rc.13')}",
    ]


@pytest.mark.parametrize("runner", sorted(_RUNNERS))
def test_a_manifest_that_fails_verification_is_never_pulled(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
    tmp_path: Path,
) -> None:
    _use_runner(runner=runner, tmp_path=tmp_path)
    (tmp_path / "cosign-refuses.txt").write_text("no matching signatures", "utf-8")
    monkeypatch.setattr(sys, "argv", ["stack.py", "2.2.0-rc.13"])

    assert stack.main() == 1

    assert "no matching signatures" in capsys.readouterr().err
    assert all(call[:2] != ["oras", "pull"] for call in _calls(anonymous_oras))
    assert dotenv.read_text(encoding="utf-8") == "HTTP_PORT=8080\n"


@pytest.mark.parametrize(
    ("verify", "pulled"),
    [
        pytest.param("", False, id="refused"),
        pytest.param("false", True, id="opted-out"),
    ],
)
def test_a_host_without_cosign_or_docker_pulls_only_when_verification_is_off(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verify: str,
    pulled: bool,
) -> None:
    (tmp_path / "bin" / "cosign").unlink()
    if verify:
        monkeypatch.setenv(stack.VERIFY_ENV, verify)
    monkeypatch.setattr(sys, "argv", ["stack.py", "2.2.0-rc.13"])

    assert stack.main() == (0 if pulled else 1)

    err = capsys.readouterr().err
    assert stack.VERIFY_ENV in err
    assert (
        any(call[:2] == ["oras", "pull"] for call in _calls(anonymous_oras)) == pulled
    )


@pytest.mark.parametrize(
    ("dotenv_line", "environ", "pulled"),
    [
        pytest.param(f"{stack.VERIFY_ENV}=false", {}, True, id="dotenv-off"),
        pytest.param(
            f"{stack.VERIFY_ENV}=false",
            {stack.VERIFY_ENV: "true"},
            False,
            id="env-wins",
        ),
        pytest.param("", {}, False, id="unset"),
    ],
)
def test_the_verify_setting_is_read_from_dotenv_under_the_environment(
    anonymous_oras: Path,
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dotenv_line: str,
    environ: dict[str, str],
    pulled: bool,
) -> None:
    """`make` reads .env without exporting it, so stack.py reads the file itself."""
    (tmp_path / "bin" / "cosign").unlink()
    dotenv.write_text(f"HTTP_PORT=8080\n{dotenv_line}\n", encoding="utf-8")
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        sys, "argv", ["stack.py", "--print", "CLICKHOUSE_VERSION", "2.2.0-rc.13"]
    )

    assert stack.main() == (0 if pulled else 1)
    assert (
        any(call[:2] == ["oras", "pull"] for call in _calls(anonymous_oras)) == pulled
    )


def test_a_signer_named_in_dotenv_is_the_one_verified(
    anonymous_oras: Path, dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = (
        "https://github.com/example/stack/.github/workflows/release.yml@refs/heads/main"
    )
    dotenv.write_text(
        f"{stack.SIGNER_IDENTITY_ENV}={identity}\n"
        f"{stack.SIGNER_ISSUER_ENV}=https://issuer.example\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys, "argv", ["stack.py", "--print", "CLICKHOUSE_VERSION", "2.2.0-rc.13"]
    )

    assert stack.main() == 0

    verify = next(call for call in _calls(anonymous_oras) if call[0] == "cosign")
    assert verify[2:6] == [
        "--certificate-identity",
        identity,
        "--certificate-oidc-issuer",
        "https://issuer.example",
    ]


def test_a_refused_signature_names_the_ref_the_signer_and_the_escape(
    anonymous_oras: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "cosign-refuses.txt").write_text("no matching signatures", "utf-8")
    monkeypatch.setattr(sys, "argv", ["stack.py", "2.2.0-rc.13"])

    assert stack.main() == 1

    err = capsys.readouterr().err
    pinned = f"{_registry.DEFAULT_MANIFEST_REPO}@{_digest_of('2.2.0-rc.13')}"
    assert (
        f"could not verify {pinned} against {stack.DEFAULT_SIGNER_IDENTITY}: "
        "no matching signatures"
    ) in err
    assert f"{stack.VERIFY_ENV}=false" in err
