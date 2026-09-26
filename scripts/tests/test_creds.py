#  Project:      dfe-docker
#  File:         tests/test_creds.py
#  Purpose:      Assert the access summary prints only the passwords it may print
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The access summary is the one place the minted admin password is printed.

`make up` and `make dev` end with it, so its stdout is a build log as often as it
is a terminal. These pin the branch: the value on a TTY, the .env key that holds
it otherwise, and DFE_CREDS_SHOW=0 taking the second branch on a TTY too.

The break-glass and OIDC fixture passwords have no such branch on the terminal --
they are named, never printed, and these pin that on both.

`--write` is the other artefact: a 0600 file that DOES carry both passwords, for
the operator to read once and delete. These pin the mode, the plaintext, and the
three closing steps that let it be deleted.

The URLs have a branch of their own: DFE_EXTERNAL_ORIGIN where a deployment has
set one to an address browsers use, this box's own loopback otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import creds

_MINTED = "s3cr3t-minted-value"
_FIXTURE_PASSWORD = "shared-fixture-value"
_ENV = f"""\
DFE_ENV=dev
DFE_AUTH_LOCAL_ADMIN_NAME=admin
DFE_AUTH_LOCAL_ADMIN_PASSWORD={_MINTED}
DFE_AUTH_BREAKGLASS_PASSWORD=another-minted-value
"""


def test_tty_prints_the_password(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert _MINTED in capsys.readouterr().out


def test_non_tty_prints_the_fetch_hint_instead(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    out = capsys.readouterr().out
    assert _MINTED not in out
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in out


def test_show_zero_hides_it_on_a_tty(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("DFE_CREDS_SHOW", "0")

    assert creds.main() == 0
    out = capsys.readouterr().out
    assert _MINTED not in out
    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in out


def test_the_break_glass_password_is_never_printed(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert "another-minted-value" not in capsys.readouterr().out


def test_a_missing_dotenv_reports_rather_than_prints(
    dotenv: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert creds.main() == 1
    assert "make init" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("is_tty", "setting", "expected"),
    [
        (True, "", True),
        (True, "0", False),
        (True, "false", False),
        (True, "1", True),
        (False, "", False),
        (False, "1", False),
    ],
)
def test_show_password_rule(is_tty: bool, setting: str, expected: bool) -> None:
    assert creds.show_password(is_tty=is_tty, setting=setting) is expected


def test_the_fixture_password_is_never_printed(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(
        f"{_ENV}DFE_OIDC_FIXTURE_USER=dfe-test@dfe-oidc.test\n"
        f"DFE_OIDC_FIXTURE_PASSWORD={_FIXTURE_PASSWORD}\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    out = capsys.readouterr().out
    assert _FIXTURE_PASSWORD not in out
    assert "dfe-test@dfe-oidc.test" in out
    assert "DFE_OIDC_FIXTURE_PASSWORD" in out


def test_write_leaves_a_0600_file_with_both_passwords(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert creds.main(["--write"]) == 0

    summary = creds.ACCESS_SUMMARY_FILE
    assert summary.stat().st_mode & 0o777 == 0o600
    body = summary.read_text(encoding="utf-8")
    assert _MINTED in body
    assert "another-minted-value" in body
    assert "access-summary.md" in capsys.readouterr().out


def test_the_terminal_summary_tells_the_operator_what_to_do_next(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main([]) == 0

    out = capsys.readouterr().out
    assert "Finish the first-run wizard" in out
    assert "Retire the bootstrap admin" in out
    assert "keeps only the hash" in out


# The literals, not the constants: pinning the constant against itself would pass
# whatever it drifted to, and drifting from dfe-infra is the failure to catch.
def test_the_file_is_the_shape_dfe_infra_writes(dotenv: Path) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")

    body = creds.summary_markdown(values=creds._dotenv_values())

    assert body.startswith("# DFE access -- first login\n")
    assert "| Account | Username | Password |" in body
    assert "1. Log in at the console URL above and finish the setup wizard.\n" in body
    assert (
        "2. Retire the bootstrap admin from the wizard's last step once your own "
        "admin exists.\n"
    ) in body
    assert body.endswith(
        "3. Keep the break-glass password somewhere safe, then delete this file.\n"
    )


def test_the_file_also_names_the_two_dotenv_keys(dotenv: Path) -> None:
    """Deleting the file is the whole clean-up for dfe-infra, but not here."""
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")

    body = creds.summary_markdown(values=creds._dotenv_values())

    assert "DFE_AUTH_LOCAL_ADMIN_PASSWORD" in body
    assert "DFE_AUTH_BREAKGLASS_PASSWORD" in body


def test_a_rerun_reasserts_the_mode(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    creds.main(["--write"])
    creds.ACCESS_SUMMARY_FILE.chmod(0o644)

    creds.main(["--write"])

    assert creds.ACCESS_SUMMARY_FILE.stat().st_mode & 0o777 == 0o600


def test_without_write_no_file_is_left_behind(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    assert creds.main([]) == 0

    assert not creds.ACCESS_SUMMARY_FILE.exists()


def test_an_unminted_password_says_so_rather_than_printing_blank(dotenv: Path) -> None:
    dotenv.write_text("DFE_ENV=dev\n", encoding="utf-8", newline="\n")

    body = creds.summary_markdown(values=creds._dotenv_values())

    assert "NOT MINTED" in body
    assert "make init" in body


def test_no_fixture_keys_prints_no_fixture_line() -> None:
    assert creds.fixture_lines(values={"DFE_ENV": "dev"}) == []


def test_the_password_alone_falls_back_to_the_default_user() -> None:
    lines = creds.fixture_lines(values={"DFE_OIDC_FIXTURE_PASSWORD": _FIXTURE_PASSWORD})
    assert "dfe-test@dfe-oidc.test" in lines[0]
    assert _FIXTURE_PASSWORD not in lines[0]
    assert "DFE_OIDC_FIXTURE_PASSWORD" in lines[0]


def test_a_user_with_no_password_says_the_specs_skip() -> None:
    lines = creds.fixture_lines(
        values={"DFE_OIDC_FIXTURE_USER": "someone@example.test"}
    )
    assert "NO PASSWORD" in lines[0]


def test_per_provider_overrides_are_named_not_printed() -> None:
    lines = creds.fixture_lines(
        values={
            "DFE_OIDC_FIXTURE_PASSWORD": "generic",
            "DFE_OIDC_OKTA_FIXTURE_PASSWORD": "okta-only",
            "DFE_OIDC_ENTRA_FIXTURE_USER": "entra@example.test",
            "DFE_OIDC_GOOGLE_FIXTURE_PASSWORD": "",
        }
    )
    assert "per-provider override set for ENTRA, OKTA" in lines[1]
    assert "GOOGLE" not in lines[1]
    assert "okta-only" not in "".join(lines)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, "http://127.0.0.1:3000"),
        ({"DFE_BIND_SCOPE": "all"}, "http://localhost:3000"),
        ({"DFE_EXTERNAL_ORIGIN": "http://localhost"}, "http://127.0.0.1:3000"),
        (
            {"DFE_BIND_SCOPE": "all", "DFE_EXTERNAL_ORIGIN": "http://127.0.0.1"},
            "http://localhost:3000",
        ),
        (
            {"DFE_BIND_SCOPE": "all", "DFE_EXTERNAL_ORIGIN": "http://dfe.example.test"},
            "http://dfe.example.test:3000",
        ),
        (
            {"DFE_EXTERNAL_ORIGIN": "https://dfe.example.test/"},
            "https://dfe.example.test:3000",
        ),
        (
            {"DFE_EXTERNAL_ORIGIN": "http://dfe.example.test", "DFE_UI_PORT": "8443"},
            "http://dfe.example.test:8443",
        ),
    ],
)
def test_the_console_url_follows_the_external_origin(
    values: dict[str, str], expected: str
) -> None:
    """The origin wins when it names somewhere other than this box's loopback."""
    assert (
        creds._url(values=values, port_key="DFE_UI_PORT", default_port="3000")
        == expected
    )


def test_the_summary_hands_over_the_external_origin(dotenv: Path) -> None:
    dotenv.write_text(
        f"{_ENV}DFE_BIND_SCOPE=all\nDFE_EXTERNAL_ORIGIN=http://dfe.example.test\n",
        encoding="utf-8",
        newline="\n",
    )

    body = creds.summary_markdown(values=creds._dotenv_values())

    assert "- Console: http://dfe.example.test:3000" in body
    assert "- Engine API: http://dfe.example.test:8003" in body


def test_a_port_the_environment_sets_beats_the_dotenv_one(dotenv: Path) -> None:
    """Compose publishes the environment's port, so that is the one handed over."""
    dotenv.write_text(f"{_ENV}DFE_UI_PORT=3000\n", encoding="utf-8", newline="\n")

    values = creds.resolved_values(
        dotenv=creds._dotenv_values(), environ={"DFE_UI_PORT": "23000"}
    )

    assert "    console      http://127.0.0.1:23000" in creds.summary_lines(
        values=values
    )


def test_the_printed_summary_follows_the_environment(
    dotenv: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dotenv.write_text(
        f"{_ENV}DFE_UI_PORT=3000\nDFE_ENGINE_PORT=8003\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setenv("DFE_UI_PORT", "23000")
    monkeypatch.setenv("DFE_ENGINE_PORT", "28003")
    monkeypatch.setenv("DFE_BIND_SCOPE", "all")
    monkeypatch.setenv("DFE_EXTERNAL_ORIGIN", "http://dfe.example.test")

    assert creds.main([]) == 0

    out = capsys.readouterr().out
    assert "console      http://dfe.example.test:23000" in out
    assert "engine API   http://dfe.example.test:28003" in out


def test_a_password_in_the_environment_is_never_read(dotenv: Path) -> None:
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")

    values = creds.resolved_values(
        dotenv=creds._dotenv_values(),
        environ={
            "DFE_AUTH_LOCAL_ADMIN_PASSWORD": "from-the-environment",
            "DFE_AUTH_BREAKGLASS_PASSWORD": "also-from-the-environment",
        },
    )

    assert values["DFE_AUTH_LOCAL_ADMIN_PASSWORD"] == _MINTED
    assert values["DFE_AUTH_BREAKGLASS_PASSWORD"] == "another-minted-value"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"DFE_OIDC_FIXTURE_USER": "a"}, []),
        ({"DFE_OIDC_FIXTURE_PASSWORD": "a"}, []),
        ({"DFE_OIDC_DEX_FIXTURE_USER": "a"}, ["DEX"]),
        ({"DFE_OIDC_OKTA_FIXTURE_PASSWORD": "a"}, ["OKTA"]),
        ({"DFE_OIDC_OKTA_FIXTURE_PASSWORD": " "}, []),
        ({"DFE_OIDC_OKTA_CLIENT_SECRET": "a"}, []),
    ],
)
def test_fixture_provider_detection(
    values: dict[str, str], expected: list[str]
) -> None:
    assert creds.fixture_providers(values=values) == expected


_DEFAULT_ENV = _ENV.replace(_MINTED, "changeme")
_REFUSAL = "is not a dev posture"


@pytest.mark.parametrize("is_tty", [True, False])
def test_a_dev_posture_never_reports_the_refusal(
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    is_tty: bool,
) -> None:
    """The refusal is about the POSTURE, so a pipe must not conjure one.

    DFE_ENV=dev accepts the default password. Reporting otherwise sends the
    operator to rotate credentials on a stack that is running.
    """
    dotenv.write_text(_DEFAULT_ENV, encoding="utf-8", newline="\n")
    monkeypatch.setattr("sys.stdout.isatty", lambda: is_tty)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert _REFUSAL not in capsys.readouterr().out


@pytest.mark.parametrize("is_tty", [True, False])
def test_a_non_dev_posture_reports_the_refusal_either_way(
    dotenv: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    is_tty: bool,
) -> None:
    """A stack that cannot boot is worth saying in a build log too."""
    dotenv.write_text(
        _DEFAULT_ENV.replace("DFE_ENV=dev", "DFE_ENV=production"),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("sys.stdout.isatty", lambda: is_tty)
    monkeypatch.delenv("DFE_CREDS_SHOW", raising=False)

    assert creds.main() == 0
    assert _REFUSAL in capsys.readouterr().out
