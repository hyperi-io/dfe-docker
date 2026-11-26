#  Project:      dfe-docker
#  File:         _reauth.py
#  Purpose:      The re-entered-password grant the engine wants before a password session creates or deletes an account
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The grant an account create or delete carries.

Internal support module, imported by the seed, demo and e2e scripts. The engine
refuses a password session's POST or DELETE on /auth/accounts with 403
`reauth_required` unless the request carries a grant for that one account name,
earned by POST /auth/reauth with the session's own password. `post.with_grant`
is the same exchange for the other kinds an engine guards (an org delete, an
ingest credential write): it asks only after a 403 `reauth_required`, so an
engine that never asks is called as before.
"""

from dataclasses import dataclass, field

import post

REAUTH_HEADER = post.REAUTH_HEADER


@dataclass(frozen=True, slots=True)
class Session:
    """A signed-in engine session: its token, and the password the engine wants again before it changes an account.

    Neither field shows in a repr, so a log line or a traceback carries no credential.
    """

    password: str = field(repr=False)
    token: str = field(repr=False)


def account_grant(
    *, account: str, base: str, session: Session
) -> dict[str, str] | None:
    """The headers that let `session` change `account`, None when the engine gave no grant.

    A refused grant is not raised here: the change itself then answers 403 and
    its caller reports that, with the engine's reason.
    """
    header, _, _ = post.earn_grant(
        base=base,
        kind="account",
        password=session.password,
        target=account,
        token=session.token,
    )
    return header
