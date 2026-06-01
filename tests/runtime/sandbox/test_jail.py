"""Unit tests for :mod:`eawf.runtime.sandbox.jail` (P29-I03-W04).

Pin the per-OS filesystem jail + its wiring into the one live ``claude -p``
spawn:

- the Linux argv prefix is ``bwrap`` with the reap-critical
  ``--unshare-pid`` + ``--die-with-parent`` pair, a read-only root bind, a
  read-write bind of the validated cwd -- and crucially NO ``--new-session``
  / ``setsid`` (the pgid-preservation invariant the kill ladder depends on);
- the macOS argv prefix is ``sandbox-exec -f <profile>`` whose profile
  denies default, confines writes to the cwd, denies the cross-tool cred
  dirs, and carves out the spawning lane's own credential -- no setsid;
- a cwd outside the repo root is refused via the shared cwd_guard;
- every cross-tool cred dir is denied while the own-cred carve-out survives;
- the Windows gap is surfaced honestly (predicate ``False`` + typed error);
- the live spawn jails when the wrapper is on PATH and falls back unjailed
  (with a warning) when it is absent, keeping ``start_new_session`` + the
  scrubbed ``env=`` intact either way.

Platform is ALWAYS injected (``platform=`` / monkeypatched ``sys.platform``)
so the suite runs identically on any host. The subprocess + ``shutil.which``
are ALWAYS mocked -- no real ``claude`` / ``bwrap`` / ``sandbox-exec`` ever
runs. ``HOME`` is a ``tmp_path`` (never the real $HOME) so no machine path
leaks into a fixture.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.sandbox.cwd_guard import CwdGuardError
from eawf.runtime.sandbox.jail import (
    JailUnavailableOnWindowsError,
    build_jail_argv,
    build_seatbelt_profile,
    jail_command,
    jail_supported,
)

_CLAUDE: str = "claude-code"
_CODEX: str = "codex"

#: The cross-tool credential dirs the jail must deny read of (every one of
#: these, on both lanes).
_CRED_DENY_DIRS: tuple[str, ...] = (
    ".aws",
    ".ssh",
    ".config/gh",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".docker",
    ".gnupg",
)


def _repo_with_cwd(tmp_path: Path) -> tuple[Path, Path]:
    """Return a ``(root, cwd)`` pair: an existing repo root + child cwd.

    The cwd sits inside the root and is materialised on disk so the jail's
    existence check passes.
    """
    root = tmp_path / "repo"
    cwd = root / "worktree"
    cwd.mkdir(parents=True)
    return root, cwd


# ---------------------------------------------------------------------------
# platform gate: mac/linux supported, win32 not
# ---------------------------------------------------------------------------


def test_jail_supported_true_on_linux() -> None:
    """Linux supports the FS jail."""
    assert jail_supported(platform="linux") is True


def test_jail_supported_true_on_darwin() -> None:
    """macOS supports the FS jail."""
    assert jail_supported(platform="darwin") is True


def test_jail_supported_false_on_win32() -> None:
    """Windows has no bubblewrap / seatbelt parity."""
    assert jail_supported(platform="win32") is False


def test_build_jail_argv_windows_raises(tmp_path: Path) -> None:
    """The launch helper raises the typed Windows-gap error on win32."""
    root, cwd = _repo_with_cwd(tmp_path)
    with pytest.raises(
        JailUnavailableOnWindowsError, match="filesystem jail unavailable on windows"
    ):
        build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="win32", home=tmp_path)


# ---------------------------------------------------------------------------
# Linux argv: bwrap prefix + reap-critical flags + NO new session
# ---------------------------------------------------------------------------


def test_build_jail_argv_linux_is_bwrap_prefix(tmp_path: Path) -> None:
    """The Linux prefix is a ``bwrap`` argv with the read-only root bind."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    assert argv[0] == "bwrap"
    # Read-only bind of the root: "--ro-bind", "/", "/".
    assert "--ro-bind" in argv
    ro_idx = argv.index("--ro-bind")
    assert argv[ro_idx + 1] == "/"
    assert argv[ro_idx + 2] == "/"


def test_build_jail_argv_linux_sets_reap_critical_flags(tmp_path: Path) -> None:
    """Both ``--unshare-pid`` and ``--die-with-parent`` are present (bwrap #529)."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv


def test_build_jail_argv_linux_rw_binds_the_cwd(tmp_path: Path) -> None:
    """A ``--bind <cwd> <cwd>`` makes the worktree the one writable subtree."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    cwd_str = str(cwd.resolve())
    assert "--bind" in argv
    bind_idx = argv.index("--bind")
    assert argv[bind_idx + 1] == cwd_str
    assert argv[bind_idx + 2] == cwd_str


def test_build_jail_argv_linux_preserves_pgid_no_new_session(tmp_path: Path) -> None:
    """LOAD-BEARING: the Linux prefix emits NO ``--new-session`` and no setsid.

    The kill ladder reaps by signalling the daemon-set process group; if the
    wrapper started its own session the descendants would escape the reap.
    This is the single most important correctness property of the jail (the
    W03 spike proved the reap depends on it), so it gets an explicit guard.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    assert "--new-session" not in argv
    assert "setsid" not in argv
    assert not any("setsid" in token for token in argv)


def test_build_jail_argv_linux_masks_every_cred_dir(tmp_path: Path) -> None:
    """Every cross-tool cred dir is tmpfs-masked inside the Linux jail."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    joined = " ".join(argv)
    for rel in _CRED_DENY_DIRS:
        masked = str((tmp_path / rel).resolve())
        assert "--tmpfs" in argv
        assert masked in joined


# ---------------------------------------------------------------------------
# macOS argv: sandbox-exec -f <profile> + profile shape + NO setsid
# ---------------------------------------------------------------------------


def test_build_jail_argv_darwin_is_sandbox_exec_prefix(tmp_path: Path) -> None:
    """The macOS prefix is ``sandbox-exec -f <profile>``."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="darwin", home=tmp_path)
    assert argv[:2] == ["sandbox-exec", "-f"]
    assert len(argv) == 3
    profile = argv[2]
    assert "(deny default)" in profile


def test_build_jail_argv_darwin_no_setsid(tmp_path: Path) -> None:
    """LOAD-BEARING: the seatbelt prefix carries no setsid / new-session.

    ``sandbox-exec`` exec's in-place (no fork, no setsid) so the daemon-set
    pgid is preserved; the prefix must not reintroduce a session.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="darwin", home=tmp_path)
    assert not any("setsid" in token for token in argv)
    assert not any("new-session" in token for token in argv)


def test_build_seatbelt_profile_denies_default_and_confines_writes(tmp_path: Path) -> None:
    """The profile denies by default and confines writes to the cwd subpath."""
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    assert "(version 1)" in profile
    assert "(deny default)" in profile
    cwd_str = str(cwd.resolve())
    assert f'(allow file-write* (subpath "{cwd_str}"))' in profile


def test_build_seatbelt_profile_allows_process_exec(tmp_path: Path) -> None:
    """The child must still exec the inner runtime under the profile."""
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    assert "(allow process-exec)" in profile


def test_build_seatbelt_profile_denies_cross_tool_cred_dirs(tmp_path: Path) -> None:
    """Every cross-tool cred dir is denied for read in the profile."""
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    for rel in _CRED_DENY_DIRS:
        cred = str((tmp_path / rel).resolve())
        assert f'(deny file-read* (subpath "{cred}"))' in profile


def test_build_seatbelt_profile_claude_carves_out_keychain(tmp_path: Path) -> None:
    """macOS claude lane permits the Keychain read path (not a $HOME cred file)."""
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    keychain = str((tmp_path / "Library/Keychains").resolve())
    assert f'(allow file-read* (subpath "{keychain}"))' in profile
    assert "com.apple.securityd" in profile


def test_build_seatbelt_profile_codex_carves_out_own_cred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex lane permits read of ``$CODEX_HOME/auth.json``'s dir."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path)
    codex_dir = str((tmp_path / ".codex").resolve())
    assert f'(allow file-read* (subpath "{codex_dir}"))' in profile


def test_build_seatbelt_profile_unknown_runtime_raises(tmp_path: Path) -> None:
    """An unknown runtime lane raises ValueError naming the value."""
    _root, cwd = _repo_with_cwd(tmp_path)
    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_seatbelt_profile(cwd=cwd, runtime="opencode", home=tmp_path)


# ---------------------------------------------------------------------------
# denyRead: cred dirs denied, own-cred carve-out present (both lanes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_jail_denies_every_cross_tool_cred_dir(platform: str, tmp_path: Path) -> None:
    """Every cross-tool cred dir is denied on both platforms."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform=platform, home=tmp_path)
    joined = " ".join(argv)
    for rel in _CRED_DENY_DIRS:
        cred = str((tmp_path / rel).resolve())
        assert cred in joined


def test_jail_claude_own_cred_not_denied_linux(tmp_path: Path) -> None:
    """The claude own-cred dir (~/.claude) is NOT among the masked cred dirs."""
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    claude_cred_dir = str((tmp_path / ".claude").resolve())
    # ~/.claude is not one of the tmpfs-masked cross-tool dirs.
    tmpfs_targets = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
    assert claude_cred_dir not in tmpfs_targets


def test_jail_codex_own_cred_not_denied_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex own-cred ($CODEX_HOME/auth.json) is allowed, not denied."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path)
    codex_dir = str((tmp_path / ".codex").resolve())
    # Allowed for read, and NOT in a deny clause.
    assert f'(allow file-read* (subpath "{codex_dir}"))' in profile
    assert f'(deny file-read* (subpath "{codex_dir}"))' not in profile


# ---------------------------------------------------------------------------
# cwd confinement: validated against the repo root via cwd_guard
# ---------------------------------------------------------------------------


def test_build_jail_argv_cwd_outside_root_raises(tmp_path: Path) -> None:
    """A cwd OUTSIDE the repo root is refused via the shared cwd_guard."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(CwdGuardError, match="resolves outside repo root"):
        build_jail_argv(_CLAUDE, cwd=outside, root=root, platform="linux", home=tmp_path)


def test_build_jail_argv_nonexistent_cwd_raises(tmp_path: Path) -> None:
    """A cwd that does not exist is refused (no confining to a bogus path)."""
    root = tmp_path / "repo"
    root.mkdir()
    missing = root / "ghost"
    with pytest.raises(FileNotFoundError, match="jail cwd does not exist"):
        build_jail_argv(_CLAUDE, cwd=missing, root=root, platform="linux", home=tmp_path)


# ---------------------------------------------------------------------------
# jail_command: composition + boundary / error paths
# ---------------------------------------------------------------------------


def test_jail_command_prefixes_the_child_argv(tmp_path: Path) -> None:
    """``jail_command`` returns the prefix followed by the child argv."""
    root, cwd = _repo_with_cwd(tmp_path)
    child = ["claude", "-p", "hello"]
    out = jail_command(child, runtime=_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    assert out[0] == "bwrap"
    assert out[-3:] == child


def test_jail_command_empty_argv_raises(tmp_path: Path) -> None:
    """An empty child argv is refused (nothing to jail)."""
    root, cwd = _repo_with_cwd(tmp_path)
    with pytest.raises(ValueError, match="argv must be non-empty"):
        jail_command([], runtime=_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)


def test_jail_command_unknown_runtime_raises(tmp_path: Path) -> None:
    """An unknown runtime lane raises ValueError before any argv is built."""
    root, cwd = _repo_with_cwd(tmp_path)
    with pytest.raises(ValueError, match="unknown runtime lane"):
        jail_command(["x"], runtime="opencode", cwd=cwd, root=root, platform="linux", home=tmp_path)


# ---------------------------------------------------------------------------
# Wiring: the live spawn jails (or falls back) without losing env / session
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`."""

    def __init__(self, *, stdout: bytes, returncode: int, pid: int = 4321) -> None:
        self._stdout = stdout
        self.returncode: int | None = returncode
        self.pid = pid

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - not exercised on the happy path
        pass

    async def wait(self) -> int:  # pragma: no cover - not exercised on happy path
        return self.returncode if self.returncode is not None else -1


_ENVELOPE: bytes = b'{"result":"ok","session_id":"s","usage":{"input_tokens":1,"output_tokens":1}}'


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Patch ``create_subprocess_exec`` to capture argv + kwargs, no real spawn."""
    proc = _FakeProcess(stdout=_ENVELOPE, returncode=0)

    async def _fake_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = list(argv)
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(claude_adapter.asyncio, "create_subprocess_exec", _fake_exec)


def test_spawn_session_jails_when_wrapper_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the wrapper is on PATH + supported, argv[0] is the wrapper and
    ``start_new_session`` + the scrubbed env are still passed."""
    root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "linux")
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(claude_adapter, "_repo_root_for", lambda _path: root)

    captured: dict[str, object] = {}
    _patch_spawn(monkeypatch, captured)

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("p", model="m", cwd=str(cwd)))
    assert result.text == "ok"

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "bwrap"
    assert "claude" in argv  # the inner child argv survives the prefix
    # The daemon stays the sole session-setter + env stays scrubbed.
    assert captured["start_new_session"] is True
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_spawn_session_falls_back_unjailed_when_wrapper_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the wrapper binary is absent, argv[0] is ``claude`` (unjailed) and
    a warning is logged; the spawn still passes env + session."""
    root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "linux")
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_adapter, "_repo_root_for", lambda _path: root)

    captured: dict[str, object] = {}
    _patch_spawn(monkeypatch, captured)

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("p", model="m", cwd=str(cwd)))
    assert result.text == "ok"

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "claude"
    assert "bwrap" not in argv
    assert captured["start_new_session"] is True
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_spawn_session_unjailed_on_windows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On Windows (no jail parity) the spawn runs unjailed, never raising."""
    _root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "win32")

    captured: dict[str, object] = {}
    _patch_spawn(monkeypatch, captured)

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("p", model="m", cwd=str(cwd)))
    assert result.text == "ok"
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "claude"
