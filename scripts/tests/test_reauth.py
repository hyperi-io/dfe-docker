#  Project:      dfe-docker
#  File:         tests/test_reauth.py
#  Purpose:      Assert the account grant the seed, demo and e2e scripts send with an account change
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The grant an account create or delete carries, and what happens when the engine gives none."""

import pytest

import _reauth
import post


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"grant": "g1", "expires_in": 300}, {"X-DFE-Reauth": "g1"}),
        ({"code": "invalid_password"}, None),
        ({"grant": ""}, None),
        ("<html>bad gateway</html>", None),
    ],
    ids=["grant", "refused", "empty-grant", "not-json"],
)
def test_the_grant_is_the_header_or_nothing(
    monkeypatch: pytest.MonkeyPatch, answer: object, expected: dict | None
) -> None:
    sent = []

    def reauth(
        url: str, payload: dict, token: str = "", timeout: int = 30, headers=None
    ) -> tuple[int, object]:
        sent.append((url, payload, token))
        return 200, answer

    monkeypatch.setattr(post, "_api_post_json", reauth)

    grant = _reauth.account_grant(
        account="ops",
        base="http://engine.invalid/api/v1",
        session=_reauth.Session(password="pw", token="t"),
    )

    assert grant == expected
    assert sent == [
        (
            "http://engine.invalid/api/v1/auth/reauth",
            {"kind": "account", "password": "pw", "target": "ops"},
            "t",
        )
    ]


def test_a_session_shows_no_credential_in_its_repr() -> None:
    shown = repr(_reauth.Session(password="hunter2", token="tok-123"))

    assert "hunter2" not in shown
    assert "tok-123" not in shown


_DENIED = (403, {"code": "reauth_required"})
_GRANTED = (200, {"grant": "g1", "expires_in": 600})
_GRANT_HEADER = {"X-DFE-Reauth": "g1"}


@pytest.mark.parametrize(
    ("first", "exchange", "retry", "password", "expected", "sent", "exchanged"),
    [
        ((204, ""), None, None, "pw", (204, ""), [None], 0),
        (_DENIED, _GRANTED, (204, ""), "pw", (204, ""), [None, _GRANT_HEADER], 1),
        (
            (403, {"code": "forbidden"}),
            None,
            None,
            "pw",
            (403, {"code": "forbidden"}),
            [None],
            0,
        ),
        (
            _DENIED,
            (403, {"code": "invalid_password"}),
            None,
            "wrong",
            (403, {"code": "invalid_password"}),
            [None],
            1,
        ),
        (
            _DENIED,
            (409, {"code": "reauth_via_idp"}),
            None,
            "pw",
            (409, {"code": "reauth_via_idp"}),
            [None],
            1,
        ),
        (_DENIED, (200, {"detail": "no grant"}), None, "pw", _DENIED, [None], 1),
        (_DENIED, _GRANTED, _DENIED, "pw", _DENIED, [None, _GRANT_HEADER], 1),
        (_DENIED, None, None, "", _DENIED, [None], 0),
    ],
    ids=[
        "engine-never-asks",
        "asks-then-takes-the-grant",
        "other-403-is-left-alone",
        "wrong-password",
        "identity-provider-account",
        "exchange-answers-no-grant",
        "second-403-after-retry-fails-loudly",
        "no-password-to-offer",
    ],
)
def test_a_grant_is_earned_only_when_the_engine_asks_and_asked_once(
    monkeypatch: pytest.MonkeyPatch,
    first: tuple[int, object],
    exchange: tuple[int, object] | None,
    retry: tuple[int, object] | None,
    password: str,
    expected: tuple[int, object],
    sent: list[dict[str, str] | None],
    exchanged: int,
) -> None:
    answers = iter([first, *([retry] if retry else [])])
    seen: list[dict[str, str] | None] = []
    posts: list[tuple[str, dict, str]] = []

    def send(headers: dict[str, str] | None) -> tuple[int, object]:
        seen.append(headers)
        return next(answers)

    def reauth(
        url: str, payload: dict, token: str = "", timeout: int = 30, headers=None
    ) -> tuple[int, object]:
        posts.append((url, payload, token))
        return exchange

    monkeypatch.setattr(post, "_api_post_json", reauth)

    answered = post.with_grant(
        base="http://engine.invalid/api/v1",
        kind="org",
        password=password,
        send=send,
        target="e2e",
        token="t",
    )

    assert (answered, seen, len(posts)) == (expected, sent, exchanged)
    assert all(
        (url, payload["kind"], payload["target"])
        == ("http://engine.invalid/api/v1/auth/reauth", "org", "e2e")
        for url, payload, _ in posts
    )
    assert "wrong" not in repr(answered)
