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
