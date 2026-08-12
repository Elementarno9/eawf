"""Unit tests for :mod:`eawf.runtime.sandbox.env_scrub`.

Pin the env-scrub allowlist + its wiring into the one live ``claude -p``
spawn:

- the shared floor is always seeded (HOME / pinned PATH / LANG / TERM),
  and the pinned PATH is NOT the parent PATH;
- each lane keeps only its own auth family and drops the cross-lane key;
- credential families never on the allowlist (``AWS_*`` / ``GH_*`` /
  ``SSH_*`` / ``KUBECONFIG`` / ``EAWF_*`` / unknown) never survive;
- the live spawn passes the scrubbed env into ``create_subprocess_exec``.

The wiring test ALWAYS mocks the subprocess -- it never spawns a real
``claude`` process (no network / auth / cost). It patches
:func:`asyncio.create_subprocess_exec` to capture the ``env=`` kwarg.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir

_PINNED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

#: A base env seeded with one credential from every family the floor must
#: drop, plus both lanes' auth so cross-lane drops are observable, plus a
#: realistic floor.
_FULL_BASE_ENV: dict[str, str] = {
    "HOME": "/sandbox/agent",
    "PATH": "/opt/evil/bin:/usr/local/bin:/usr/bin:/bin",
    "LANG": "en_US.UTF-8",
    "LC_CTYPE": "en_US.UTF-8",
    "TERM": "xterm-256color",
    "USER": "agent",
    "LOGNAME": "agent",
    # claude-lane auth (placeholder, non-secret values)
    "ANTHROPIC_API_KEY": "placeholder-anthropic",  # pragma: allowlist secret
    "CLAUDE_CONFIG_DIR": "/sandbox/agent/.claude",
    "CLAUDE_CODE_OAUTH_TOKEN": "placeholder-oauth",  # pragma: allowlist secret
    # codex-lane auth (placeholder, non-secret values)
    "OPENAI_API_KEY": "placeholder-openai",  # pragma: allowlist secret
    "CODEX_HOME": "/sandbox/agent/.codex",
    # opencode-lane non-secret feature flag (OPENCODE_* family is kept)
    "OPENCODE_ENABLE_EXA": "1",
    # must-drop credential families (placeholder, non-secret values)
    "AWS_SECRET_ACCESS_KEY": "placeholder-aws",  # pragma: allowlist secret
    "AWS_SESSION_TOKEN": "placeholder-aws-session",
    "GH_TOKEN": "placeholder-gh",  # pragma: allowlist secret
    "GITHUB_TOKEN": "placeholder-github",  # pragma: allowlist secret
    "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
    "KUBECONFIG": "/sandbox/agent/.kube/config",
    "EAWF_DAEMONLESS": "1",
    "UNKNOWN_VAR": "leak-me",
}


# ---------------------------------------------------------------------------
# Floor: always seeded, PATH pinned (not the parent PATH)
# ---------------------------------------------------------------------------


def test_build_child_env_seeds_floor() -> None:
    """The shared floor (HOME / PATH / LANG / TERM) is present on the child."""
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert env["HOME"] == "/sandbox/agent"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["TERM"] == "xterm-256color"
    assert "PATH" in env


def test_build_child_env_keeps_user_identity_for_keychain_auth() -> None:
    """USER / LOGNAME ride the floor: the macOS keychain login lookup needs them.

    Without these a sandboxed ``claude -p`` cannot resolve its stored OAuth
    credential from the login keychain and exits "Not logged in", even though the
    jail permits the keychain itself. They are the account name, not a secret.
    """
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert env["USER"] == "agent"
    assert env["LOGNAME"] == "agent"


def test_build_child_env_user_identity_absent_when_base_lacks_it() -> None:
    """The floor copies USER / LOGNAME only when present -- never fabricated."""
    base = {k: v for k, v in _FULL_BASE_ENV.items() if k not in {"USER", "LOGNAME"}}
    env = build_child_env("codex", base_env=base)
    assert "USER" not in env
    assert "LOGNAME" not in env


def test_build_child_env_pins_path_not_parent() -> None:
    """PATH is pinned to the fixed floor, NOT the parent PATH."""
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert env["PATH"] == _PINNED_PATH
    assert env["PATH"] != _FULL_BASE_ENV["PATH"]
    assert "/opt/evil/bin" not in env["PATH"]


def test_build_child_env_carries_lc_locale_vars() -> None:
    """``LC_*`` locale carry-through is kept (floor locale family)."""
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert env["LC_CTYPE"] == "en_US.UTF-8"


# ---------------------------------------------------------------------------
# claude lane: keep claude auth, drop codex auth
# ---------------------------------------------------------------------------


def test_build_child_env_claude_lane_keeps_claude_auth() -> None:
    """claude lane keeps ANTHROPIC_* + CLAUDE_* auth."""
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert env["ANTHROPIC_API_KEY"] == "placeholder-anthropic"  # pragma: allowlist secret
    assert env["CLAUDE_CONFIG_DIR"] == "/sandbox/agent/.claude"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "placeholder-oauth"


def test_build_child_env_claude_lane_drops_codex_auth() -> None:
    """claude lane drops the cross-lane codex auth (OPENAI_* / CODEX_HOME)."""
    env = build_child_env("claude-code", base_env=_FULL_BASE_ENV)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_HOME" not in env


# ---------------------------------------------------------------------------
# codex lane: keep codex auth, drop claude auth
# ---------------------------------------------------------------------------


def test_build_child_env_codex_lane_keeps_codex_auth() -> None:
    """codex lane keeps CODEX_HOME + OPENAI_* auth."""
    env = build_child_env("codex", base_env=_FULL_BASE_ENV)
    assert env["CODEX_HOME"] == "/sandbox/agent/.codex"
    assert env["OPENAI_API_KEY"] == "placeholder-openai"  # pragma: allowlist secret


def test_build_child_env_codex_lane_drops_claude_auth() -> None:
    """codex lane drops the cross-lane claude auth (ANTHROPIC_* / CLAUDE_*)."""
    env = build_child_env("codex", base_env=_FULL_BASE_ENV)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ---------------------------------------------------------------------------
# opencode lane: keep OPENCODE_* family, drop BOTH vendors' API-key auth
# ---------------------------------------------------------------------------


def test_build_child_env_opencode_lane_keeps_opencode_family() -> None:
    """opencode lane keeps the OPENCODE_* family (data-dir override + flags)."""
    env = build_child_env("opencode", base_env=_FULL_BASE_ENV)
    assert env["OPENCODE_ENABLE_EXA"] == "1"


def test_build_child_env_opencode_lane_drops_both_vendor_auth() -> None:
    """opencode reads creds from its on-disk store, so both vendor auth keys drop."""
    env = build_child_env("opencode", base_env=_FULL_BASE_ENV)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_HOME" not in env


# ---------------------------------------------------------------------------
# extra_path_dir: PREPENDED to the pinned floor, parent PATH still dropped
# ---------------------------------------------------------------------------


def test_build_child_env_extra_path_dir_prepended_to_pinned_floor() -> None:
    """extra_path_dir prepends to the pinned PATH; the parent PATH never leaks."""
    env = build_child_env("opencode", base_env=_FULL_BASE_ENV, extra_path_dir="/opt/homebrew/bin")
    assert env["PATH"] == "/opt/homebrew/bin:" + _PINNED_PATH
    assert "/opt/evil/bin" not in env["PATH"]


def test_build_child_env_extra_path_dir_already_in_floor_not_duplicated() -> None:
    """A dir already in the pinned floor is not prepended again (no duplicate)."""
    env = build_child_env("codex", base_env=_FULL_BASE_ENV, extra_path_dir="/usr/bin")
    assert env["PATH"] == _PINNED_PATH


# ---------------------------------------------------------------------------
# resolve_binary_dir: the dir feeding extra_path_dir for a Homebrew/out-of-floor CLI
# ---------------------------------------------------------------------------


def test_resolve_binary_dir_returns_which_dir_not_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dir is the ``shutil.which`` hit's dir, NOT the symlink target's.

    Regression for the codex spawn-PATH bug: Homebrew symlinks ``bin/codex``
    to a Caskroom file with a DIFFERENT basename
    (``codex-aarch64-apple-darwin``), so resolving the symlink target would
    yield a directory with no file named ``codex`` -- the child's
    ``execvp("codex")`` would still fail with "No such file or directory".
    The PATH entry must hold a file whose name IS the binary, which the
    ``which`` hit (the symlink's own dir) guarantees.
    """
    target_dir = tmp_path / "caskroom"
    target_dir.mkdir()
    real = target_dir / "mytool-aarch64-apple-darwin"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    real.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mytool").symlink_to(real)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{_PINNED_PATH}")

    resolved = resolve_binary_dir("mytool")
    assert resolved == str(bin_dir)  # the symlink's own dir...
    assert (Path(resolved) / "mytool").exists()  # ...which holds a file named 'mytool'
    assert resolved != str(target_dir)  # NOT the renamed Caskroom target's dir


def test_resolve_binary_dir_none_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binary not on PATH resolves to None (spawn falls back to the pinned floor)."""
    monkeypatch.setenv("PATH", _PINNED_PATH)
    assert resolve_binary_dir("no-such-eawf-binary-xyz") is None


# ---------------------------------------------------------------------------
# TMPDIR: pinned to an allowed write subpath, never the parent Darwin temp
# ---------------------------------------------------------------------------


def test_build_child_env_pins_tmpdir_to_allowed_temp() -> None:
    """TMPDIR is pinned to /private/tmp; the parent Darwin per-user temp is dropped.

    W37: a sandboxed CLI stages runtime sockets / PATH aliases under $TMPDIR;
    the FS jail confines writes, so TMPDIR must point at an allowed subpath
    (keeps the jail from having to open the broad /private/var/folders tree).
    """
    base = {**_FULL_BASE_ENV, "TMPDIR": "/var/folders/xx/abc/T/"}
    env = build_child_env("codex", base_env=base)
    assert env["TMPDIR"] == "/private/tmp"


# ---------------------------------------------------------------------------
# Credential families dropped on EVERY lane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime", ["claude-code", "codex", "opencode"])
def test_build_child_env_drops_all_credential_families(runtime: str) -> None:
    """No must-drop credential family survives on either lane."""
    env = build_child_env(runtime, base_env=_FULL_BASE_ENV)
    for dropped in (
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "KUBECONFIG",
        "EAWF_DAEMONLESS",
        "UNKNOWN_VAR",
    ):
        assert dropped not in env


# ---------------------------------------------------------------------------
# Boundary: empty base env still seeds a usable floor
# ---------------------------------------------------------------------------


def test_build_child_env_empty_base_seeds_floor() -> None:
    """An empty base env still yields a pinned PATH + defaulted LANG, no crash."""
    env = build_child_env("claude-code", base_env={})
    assert env["PATH"] == _PINNED_PATH
    assert env["LANG"] == "C.UTF-8"
    # No floor key the parent never had (HOME / TERM) is invented.
    assert "HOME" not in env
    assert "TERM" not in env


def test_build_child_env_missing_lang_defaults() -> None:
    """A base env with no LANG defaults LANG to C.UTF-8."""
    env = build_child_env("codex", base_env={"HOME": "/h"})
    assert env["LANG"] == "C.UTF-8"


# ---------------------------------------------------------------------------
# Error path: unknown runtime lane
# ---------------------------------------------------------------------------


def test_build_child_env_unknown_runtime_raises() -> None:
    """An unknown runtime lane raises ValueError naming the value."""
    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_child_env("gemini-cli", base_env=_FULL_BASE_ENV)


# ---------------------------------------------------------------------------
# Wiring: the live ``claude -p`` spawn passes the scrubbed env (mocked)
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`.

    The claude adapter drains ``.stdout`` / ``.stderr`` incrementally;
    :meth:`open_streams` (called from the in-loop factory) populates the
    StreamReaders with the canned envelope.
    """

    def __init__(self, *, stdout: bytes, returncode: int, pid: int = 4321) -> None:
        self._stdout = stdout
        self.returncode: int | None = returncode
        self.pid = pid
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None

    def open_streams(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(self._stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    def kill(self) -> None:  # pragma: no cover - not exercised on the happy path
        pass

    async def wait(self) -> int:  # pragma: no cover - not exercised on happy path
        return self.returncode if self.returncode is not None else -1


def test_spawn_session_passes_scrubbed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live spawn passes a scrubbed, non-None env with no cred families."""
    # The parent env carries credentials the child must never see.
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "placeholder-aws")  # pragma: allowlist secret
    monkeypatch.setenv("GH_TOKEN", "placeholder-gh")  # pragma: allowlist secret
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-github")  # pragma: allowlist secret
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh.sock")
    monkeypatch.setenv("KUBECONFIG", "/sandbox/agent/.kube/config")

    envelope = b'{"result":"ok","session_id":"s","usage":{"input_tokens":1,"output_tokens":1}}'
    proc = _FakeProcess(stdout=envelope, returncode=0)
    captured: dict[str, object] = {}

    async def _fake_exec(*_argv: str, **kwargs: object) -> _FakeProcess:
        captured.update(kwargs)
        proc.open_streams()
        return proc

    monkeypatch.setattr(claude_adapter.asyncio, "create_subprocess_exec", _fake_exec)
    # Pin the binary resolution to "absent" so PATH is the pinned floor verbatim
    # regardless of whether a `claude` CLI is installed on the test host (the
    # extra_path_dir prepend is covered by the resolve_binary_dir tests above).
    monkeypatch.setattr(claude_adapter, "resolve_binary_dir", lambda _binary: None)

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("p", model="m"))
    assert result.text == "ok"

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == _PINNED_PATH
    for key in env:
        assert not key.startswith(("AWS_", "GH_", "GITHUB_", "SSH_", "KUBECONFIG"))
