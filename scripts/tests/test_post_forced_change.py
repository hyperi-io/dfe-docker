#  Project:      dfe-docker
#  File:         tests/test_post_forced_change.py
#  Purpose:      Assert POST replaces an issued admin password and keeps .env on the live one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""POST's login against both engine shapes, and what it leaves in .env.

An engine that issues its bootstrap admin a password to replace at first login
answers that login with `password_change_required` and refuses every route but
the owner's own change. An older engine sends no flag at all. POST has to work
against both, and on the newer one .env has to end on whichever password the
engine holds, whatever the change answered, because nothing else records it.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import _common
import post

_ISSUED = "IssuedByMakeInit0123456789"
_BASE = post.ENGINE_NETWORK_BASE
_ENV = f"""\
## The bootstrap admin.
# {post.ADMIN_PASSWORD_KEY}=commented-out-example
DFE_ENV=production
{post.ADMIN_PASSWORD_KEY}={_ISSUED}
DFE_AUTH_BREAKGLASS_PASSWORD=untouched-breakglass-value
"""


class _Engine:
    """The engine's login and own-password change, served from one account's state.

    `sends_flag=False` is an engine from before the forced change: the login body
    carries no flag and nothing is refused.
    """

    def __init__(
        self,
        *,
        password: str,
        sends_flag: bool,
        change_status: int = 200,
        change_body: object = None,
    ) -> None:
        self.password = password
        self.sends_flag = sends_flag
        self.change_required = sends_flag
        self.change_status = change_status
        self.change_body = change_body
        self.changes: list[dict] = []
        self.logins: list[str] = []

    def post_json(self, url: str, payload: dict, token: str = "", timeout: int = 30):
        route = url.removeprefix(_BASE)
        if route == "/auth/login":
            self.logins.append(payload["password"])
            if payload["password"] != self.password:
                return 401, {"detail": {"code": "invalid_credentials"}}
            body: dict = {"access_token": f"token-{len(self.logins)}"}
            if self.sends_flag:
                body["password_change_required"] = self.change_required
            return 200, body
        if route == post.CHANGE_PASSWORD_PATH:
            self.changes.append({"token": token, **payload})
            if self.change_status != 200:
                return self.change_status, self.change_body
            self.password = payload["new_password"]
            self.change_required = False
            return 200, {"message": "password reset", "git": {}}
        raise AssertionError(f"unexpected route {route}")


@pytest.fixture
def issued(dotenv: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A .env holding the issued admin password, 0600, and nothing else steering POST."""
    dotenv.write_text(_ENV, encoding="utf-8", newline="\n")
    dotenv.chmod(0o600)
    for name in ("DFE_POST_LOGIN_USER", "DFE_POST_LOGIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DFE_AUTH_LOCAL_ADMIN_NAME", "admin")
    monkeypatch.setenv(post.ADMIN_PASSWORD_KEY, _ISSUED)
    return dotenv


def _serve(monkeypatch: pytest.MonkeyPatch, engine: _Engine) -> _Engine:
    monkeypatch.setattr(post, "_api_post_json", engine.post_json)
    return engine


def _recorded(path: Path, key: str = post.ADMIN_PASSWORD_KEY) -> str:
    """The live value .env gives key -- the last assignment, as every reader takes it."""
    values = [
        line.partition("=")[2]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]
    return values[-1]


def test_an_engine_without_the_flag_is_logged_into_as_it_is(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=False))

    login = post._login(_BASE)

    assert login == post.Login("token-1", 200, "admin", "")
    assert engine.changes == []
    assert issued.read_text(encoding="utf-8") == _ENV


def test_an_issued_password_is_replaced_recorded_and_logged_in_again(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    login = post._login(_BASE)

    recorded = _recorded(issued)
    assert login == post.Login("token-2", 200, "admin", "")
    assert engine.changes == [{"token": "token-1", "new_password": recorded}]
    assert engine.password == recorded != _ISSUED
    assert len(recorded) == 24
    assert recorded.isalnum()
    assert post.os.environ[post.ADMIN_PASSWORD_KEY] == recorded


def test_the_next_login_in_the_same_run_uses_the_new_password(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every claim logs in on its own, so only the first may make the change."""
    engine = _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)
    second = post._login(_BASE)

    assert second.fault == ""
    assert len(engine.changes) == 1


def test_only_the_password_line_changes_and_the_mode_stays(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)

    recorded = _recorded(issued)
    assert issued.read_text(encoding="utf-8") == _ENV.replace(
        f"{post.ADMIN_PASSWORD_KEY}={_ISSUED}",
        f"{post.ADMIN_PASSWORD_KEY}={recorded}",
    )
    assert stat.S_IMODE(issued.stat().st_mode) == 0o600
    assert sorted(path.name for path in issued.parent.iterdir()) == [".env"]


def test_a_dotenv_left_open_to_others_is_closed_by_the_rewrite(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewrite narrows to the documented mode rather than carrying a wide one over."""
    issued.chmod(0o644)
    _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)

    assert _recorded(issued) != _ISSUED
    assert stat.S_IMODE(issued.stat().st_mode) == 0o644 & _common.DOTENV_MODE


def test_neither_password_is_printed(
    issued: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert _ISSUED not in output
    assert _recorded(issued) not in output
    assert post.ADMIN_PASSWORD_KEY in output


def test_no_dotenv_to_record_it_in_means_nothing_is_changed(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password the engine holds and nothing recorded is a lost admin."""
    engine = _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))
    issued.unlink()

    login = post._login(_BASE)

    assert login.token == ""
    assert "nothing was changed" in login.fault
    assert engine.changes == []
    assert engine.password == _ISSUED


def test_a_refused_change_puts_dotenv_back(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _serve(
        monkeypatch,
        _Engine(
            password=_ISSUED,
            sends_flag=True,
            change_status=500,
            change_body={"detail": "boom"},
        ),
    )

    login = post._login(_BASE)

    assert login.token == ""
    assert "HTTP 500" in login.fault
    assert issued.read_text(encoding="utf-8") == _ENV
    assert engine.password == _ISSUED
    assert post.os.environ[post.ADMIN_PASSWORD_KEY] == _ISSUED


def test_a_refusal_that_echoes_the_new_password_does_not_print_it(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request validation error carries the submitted body back in `input`."""
    engine = _Engine(password=_ISSUED, sends_flag=True, change_status=422)

    def _echoing(url: str, payload: dict, token: str = "", timeout: int = 30):
        if url.endswith(post.CHANGE_PASSWORD_PATH):
            engine.change_body = {"detail": [{"input": payload["new_password"]}]}
        return engine.post_json(url, payload, token=token, timeout=timeout)

    monkeypatch.setattr(post, "_api_post_json", _echoing)

    login = post._login(_BASE)

    submitted = engine.changes[0]["new_password"]
    assert "HTTP 422" in login.fault
    assert submitted not in login.fault
    assert "<new password>" in login.fault


def test_a_change_that_applied_without_an_answer_is_kept(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine holds the new password, so .env must too."""
    engine = _Engine(password=_ISSUED, sends_flag=True)

    def _applied_then_lost(url: str, payload: dict, token: str = "", timeout: int = 30):
        answer = engine.post_json(url, payload, token=token, timeout=timeout)
        if url.endswith(post.CHANGE_PASSWORD_PATH):
            raise post.ApiUnreachable(f"POST {url} from dfe-engine: timed out")
        return answer

    monkeypatch.setattr(post, "_api_post_json", _applied_then_lost)

    login = post._login(_BASE)

    assert login.fault == ""
    assert _recorded(issued) == engine.password != _ISSUED


def test_a_change_the_engine_took_is_kept_when_the_next_login_fails(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(password=_ISSUED, sends_flag=True)
    calls: list[str] = []

    def _second_login_lost(url: str, payload: dict, token: str = "", timeout: int = 30):
        calls.append(url)
        if url.endswith("/auth/login") and len(calls) > 1:
            raise post.ApiUnreachable(f"POST {url} from dfe-engine: timed out")
        return engine.post_json(url, payload, token=token, timeout=timeout)

    monkeypatch.setattr(post, "_api_post_json", _second_login_lost)

    login = post._login(_BASE)

    assert login.token == ""
    assert login.status == 200
    assert "replaced the password" in login.fault
    assert _recorded(issued) == engine.password != _ISSUED


def test_a_change_that_leaves_the_flag_set_fails(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))
    change = engine.post_json

    def _sticky(url: str, payload: dict, token: str = "", timeout: int = 30):
        answer = change(url, payload, token=token, timeout=timeout)
        engine.change_required = True
        return answer

    monkeypatch.setattr(post, "_api_post_json", _sticky)

    login = post._login(_BASE)

    assert login.token == ""
    assert "still requires a change" in login.fault
    assert _recorded(issued) == engine.password


def test_a_post_login_password_is_written_back_to_its_own_key(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key the login read from is the one the next run reads from."""
    monkeypatch.setenv("DFE_POST_LOGIN_PASSWORD", "post-only-password-value")
    engine = _serve(
        monkeypatch, _Engine(password="post-only-password-value", sends_flag=True)
    )

    login = post._login(_BASE)

    assert login.fault == ""
    assert _recorded(issued, "DFE_POST_LOGIN_PASSWORD") == engine.password
    assert _recorded(issued) == _ISSUED


def test_an_existing_access_summary_is_rewritten(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = issued.parent / "access-summary.md"
    summary.write_text(f"| Admin | `admin` | `{_ISSUED}` |\n", encoding="utf-8")
    _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)

    text = summary.read_text(encoding="utf-8")
    assert _recorded(issued) in text
    assert _ISSUED not in text
    assert stat.S_IMODE(summary.stat().st_mode) == 0o600


def test_no_access_summary_is_created_where_there_was_none(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a plaintext copy the operator was told to delete."""
    _serve(monkeypatch, _Engine(password=_ISSUED, sends_flag=True))

    post._login(_BASE)

    assert not (issued.parent / "access-summary.md").exists()


def test_every_live_assignment_is_rewritten_and_a_comment_is_not() -> None:
    text = "# KEY=example\nKEY=one\nOTHER=x\nKEY=two\n"

    assert (
        post._dotenv_with(text=text, key="KEY", value="new")
        == "# KEY=example\nKEY=new\nOTHER=x\nKEY=new\n"
    )


def test_a_key_with_no_live_line_is_appended() -> None:
    rendered = post._dotenv_with(text="# KEY=example\n", key="KEY", value="new")

    assert rendered.startswith("# KEY=example\n")
    assert rendered.endswith("\nKEY=new\n")


def test_a_rejected_login_is_not_a_forced_change(
    issued: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _serve(monkeypatch, _Engine(password="something-else", sends_flag=True))

    login = post._login(_BASE)

    assert login == post.Login("", 401, "admin", "login as 'admin' returned HTTP 401")
    assert engine.changes == []
    assert issued.read_text(encoding="utf-8") == _ENV


def test_the_hunt_claim_reports_a_failed_change_as_its_fault(
    issued: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _serve(
        monkeypatch,
        _Engine(password=_ISSUED, sends_flag=True, change_status=500, change_body={}),
    )
    monkeypatch.setattr(post, "_resolved_services", lambda: list(post.HUNT_SERVICES))

    claim = post._verify_hunt(
        database="dfe", ingest_url="http://ingest/ingest", marker="m", table="main"
    )

    assert claim == post.Claim(asserted=True, failed=1)
    assert "FAIL  'admin' must replace the password" in capsys.readouterr().err
