"""The claude child sees only the scrub allowlist, never the operator's session.

A daemon started from inside an operator's own Claude Code session carries
that session's ``CLAUDE_CODE_*`` variables -- its session id, its messaging
socket and the token that authenticates to it. A claude child handed them
attaches to the operator's session instead of running as a clean install,
so the claude lane admits ``CLAUDE_*`` keys by exact match only.

The allowlist below is written out independently of the module constants so
the oracle cannot drift together with the code it checks.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import pytest

from eawf.runtime.sandbox import env_scrub
from eawf.runtime.sandbox.env_scrub import build_child_env

_CLAUDE_LANE = "claude-code"

#: The operator-session token the gate-fire proof seeds. Its name is the
#: shape a Claude Code session exports; the value is a neutral placeholder.
_OPERATOR_TOKEN_KEY = "CLAUDE_CODE_MESSAGING_TOKEN"

_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "HOME",
        "TERM",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "TMPDIR",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)
_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "ANTHROPIC_")

#: An operator environment as a daemon launched from a Claude Code session
#: inherits it: the account auth the child needs, the operator's own session
#: wiring, and unrelated credentials.
_SEEDED_ENV: dict[str, str] = {
    "HOME": "/sandbox/agent",
    "USER": "operator",
    "LANG": "en_US.UTF-8",
    "LC_CTYPE": "UTF-8",
    "PATH": "/sandbox/hostile/bin:/usr/bin",
    "CLAUDE_CONFIG_DIR": "/sandbox/agent/.claude",
    "CLAUDE_CODE_OAUTH_TOKEN": "placeholder-oauth",  # pragma: allowlist secret
    "ANTHROPIC_BASE_URL": "https://example.invalid",
    _OPERATOR_TOKEN_KEY: "placeholder-operator-token",  # pragma: allowlist secret
    "CLAUDE_CODE_MESSAGING_SOCKET": "/sandbox/operator.sock",
    "CLAUDE_CODE_SESSION_ID": "operator-session",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_PID": "12345",
    "CLAUDECODE": "1",
    "GH_TOKEN": "placeholder-gh",  # pragma: allowlist secret
    "AWS_SECRET_ACCESS_KEY": "placeholder-aws",  # pragma: allowlist secret
    "OPENAI_API_KEY": "placeholder-openai",  # pragma: allowlist secret
    "EAWF_RUNTIME_DIR": "/sandbox/runtime",
}


def _outside_allowlist(env: Mapping[str, str]) -> list[str]:
    """Return every key of *env* the claude lane must never hand a child."""
    return sorted(
        key for key in env if key not in _ALLOWED_EXACT and not key.startswith(_ALLOWED_PREFIXES)
    )


def test_build_child_env_claude_lane_keeps_only_allowlisted_keys() -> None:
    """Every key the claude child gets is on the allowlist; the token is not."""
    env = build_child_env(_CLAUDE_LANE, base_env=_SEEDED_ENV)
    assert _outside_allowlist(env) == []
    assert _OPERATOR_TOKEN_KEY not in env


def test_build_child_env_claude_lane_drops_operator_session_wiring() -> None:
    """The operator's session id, socket and entrypoint never reach the child."""
    env = build_child_env(_CLAUDE_LANE, base_env=_SEEDED_ENV)
    for key in (
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_PID",
        "CLAUDECODE",
    ):
        assert key not in env


def test_build_child_env_claude_lane_keeps_account_auth() -> None:
    """Dropping the session wiring still leaves the child able to authenticate."""
    env = build_child_env(_CLAUDE_LANE, base_env=_SEEDED_ENV)
    assert env["CLAUDE_CONFIG_DIR"] == "/sandbox/agent/.claude"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "placeholder-oauth"  # pragma: allowlist secret
    assert env["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    assert env["PATH"].split(os.pathsep)[0] != "/sandbox/hostile/bin"


def test_build_child_env_claude_lane_empty_parent_yields_floor_only() -> None:
    """An empty parent invents no credential: only the seeded floor remains."""
    env = build_child_env(_CLAUDE_LANE, base_env={})
    assert _outside_allowlist(env) == []
    assert {"PATH", "LANG"} <= set(env)
    assert not any(key.startswith(("CLAUDE_", "ANTHROPIC_")) for key in env)


def test_build_child_env_unknown_lane_raises() -> None:
    """A lane the scrub does not know is refused rather than defaulted open."""
    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_child_env("claude", base_env=_SEEDED_ENV)


def test_outside_allowlist_fires_on_the_prefix_family_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate-fire proof: the old ``CLAUDE_*`` family lets the token through.

    Restores the prefix family the claude lane used to admit and shows the
    oracle above reports the seeded operator token, so the green assertions
    rest on the narrowed allowlist and not on a check that cannot fail.
    """
    monkeypatch.setattr(env_scrub, "_CLAUDE_AUTH_PREFIXES", ("CLAUDE_", "ANTHROPIC_"))
    env = build_child_env(_CLAUDE_LANE, base_env=_SEEDED_ENV)
    assert _OPERATOR_TOKEN_KEY in _outside_allowlist(env)
