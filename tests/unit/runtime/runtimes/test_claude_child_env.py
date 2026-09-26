"""The claude child sees only the scrub allowlist, never the operator's session.

A daemon started from inside an operator's own Claude Code session carries
that session's ``CLAUDE_CODE_*`` variables -- its session id, its messaging
socket and the token that authenticates to it. A claude child handed them
attaches to the operator's session instead of running as a clean install,
so the claude lane admits ``CLAUDE_*`` keys by exact match only.

The allowlist below is written out independently of the module constants so
the oracle cannot drift together with the code it checks. The spawn tests
start a real (fake) ``claude`` executable that records the environment it
was actually given; no model is ever reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
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

#: Keys the platform's process launch adds to every child on its own:
#: CoreFoundation seeds ``__CF_USER_TEXT_ENCODING`` into a darwin process
#: that the spawn never handed one.
_PLATFORM_INJECTED: frozenset[str] = frozenset({"__CF_USER_TEXT_ENCODING"})

#: Keys the managed-run isolation lays over the scrubbed env on every spawn:
#: where the stored login is read from and the two instruction switches.
_MANAGED_OVERLAY: frozenset[str] = frozenset(
    {
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    }
)

_RESULT_LINE: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "session_id": "sess-child",
    "result": "ok",
    "total_cost_usd": 0.0,
    "usage": {"input_tokens": 1, "output_tokens": 1},
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


def _write_fake_claude(directory: Path) -> Path:
    """Write a fake ``claude`` that records its environment and exits cleanly.

    The fake is a Python script rather than a shell script because a shell
    exports variables of its own (``PWD``, ``SHLVL``) that the spawn never
    passed. It dumps ``os.environ`` as JSON into its working directory (the
    scrubbed env carries no path it could be told through) and prints one
    stream-json ``result`` line so the spawn parses a successful turn.
    """
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "with open('child-env.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(dict(os.environ), handle)\n"
        f"print({json.dumps(json.dumps(_RESULT_LINE))})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_spawn_session_claude_child_receives_only_scrubbed_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The env a real child process receives is the scrubbed one.

    The OS jail is bypassed so the test observes the env seam alone on any
    POSIX host; the jail has its own coverage.
    """
    for key, value in _SEEDED_ENV.items():
        if key != "PATH":
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        claude_adapter,
        "_maybe_jail_argv",
        lambda argv, *, runtime, cwd, session="", sink=None: argv,
    )
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(_write_fake_claude(tmp_path))

    result = asyncio.run(
        adapter.spawn_session("prompt", model="placeholder-model", cwd=str(tmp_path), timeout=30.0)
    )

    recorded = json.loads((tmp_path / "child-env.json").read_text(encoding="utf-8"))
    assert set(recorded) >= _MANAGED_OVERLAY
    child_env = {
        key: value
        for key, value in recorded.items()
        if key not in _PLATFORM_INJECTED | _MANAGED_OVERLAY
    }
    assert result.session_id == "sess-child"
    assert _outside_allowlist(child_env) == []
    assert _OPERATOR_TOKEN_KEY not in child_env
    assert "GH_TOKEN" not in child_env
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == "placeholder-oauth"  # pragma: allowlist secret
