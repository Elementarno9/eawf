"""Unit tests for :mod:`eawf.runtime.sandbox.jail` (P29-I03-W04).

Pin the per-OS filesystem jail + its wiring into the one live ``claude -p``
spawn:

- the Linux argv prefix is ``bwrap`` with the reap-critical
  ``--unshare-pid`` + ``--die-with-parent`` pair, a read-only root bind, a
  read-write bind of the validated cwd -- and crucially NO ``--new-session``
  / ``setsid`` (the pgid-preservation invariant the kill ladder depends on);
- the macOS argv prefix is ``sandbox-exec -p <profile>`` (the inline
  profile-string form, NOT ``-f`` which reads a profile from a FILE path
  and would treat the inline text as a missing filename) whose profile
  denies default, confines writes to the cwd, denies the cross-tool cred
  dirs, and carves out the spawning lane's own credential -- no setsid;
- a cwd outside the repo root is refused via the shared cwd_guard;
- every cross-tool cred dir is denied while the own-cred carve-out survives;
- the Windows gap is surfaced honestly (predicate ``False`` + typed error);
- the live spawn jails when the wrapper is on PATH and falls back unjailed
  (with a warning) when it is absent, keeping ``start_new_session`` + the
  scrubbed ``env=`` intact either way.

Platform is ALWAYS injected (``platform=`` / monkeypatched ``sys.platform``)
so the unit suite runs identically on any host. The unit-level subprocess +
``shutil.which`` are ALWAYS mocked -- no real ``claude`` / ``bwrap`` ever
runs there. ``HOME`` is a ``tmp_path`` (never the real $HOME) so no machine
path leaks into a fixture.

The ONE exception is the macOS-guarded semantics smoke test
:func:`test_seatbelt_jail_executes_real_write_policy_on_macos`, which DOES
run a real ``sandbox-exec`` so the ``-p`` flag fix is proven against the
kernel's seatbelt enforcement (a denied write is actually blocked + an
allowed write actually lands) rather than only by argv shape. It skips on
non-macOS and when ``sandbox-exec`` is absent so CI on Linux stays green.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
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
_OPENCODE: str = "opencode"

#: The real-spawn semantics smoke test runs ONLY on macOS with ``sandbox-exec``
#: actually present -- it exercises the kernel's seatbelt enforcement, so it
#: must skip everywhere else (Linux CI, a macOS without the binary) instead of
#: failing. On THIS darwin machine the binary is present and the test runs.
_seatbelt_unavailable = sys.platform != "darwin" or shutil.which("sandbox-exec") is None
_REQUIRE_SEATBELT = pytest.mark.skipif(
    _seatbelt_unavailable,
    reason="real seatbelt semantics smoke needs macOS + sandbox-exec on PATH",
)

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
# macOS argv: sandbox-exec -p <profile> + profile shape + NO setsid
# ---------------------------------------------------------------------------


def test_build_jail_argv_darwin_is_sandbox_exec_prefix(tmp_path: Path) -> None:
    """The macOS prefix is ``sandbox-exec -p <profile>`` (inline-string form).

    ``-p`` takes the seatbelt profile as an inline STRING argument; ``-f``
    reads it from a FILE path and so would treat the inline profile text as
    a missing filename, failing every real jailed spawn. This pins the
    inline-profile form the seatbelt path actually intends.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="darwin", home=tmp_path)
    assert argv[:2] == ["sandbox-exec", "-p"]
    assert "-f" not in argv  # the file-path form is the defect, never emitted
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


# ---------------------------------------------------------------------------
# macOS semantics smoke: a REAL jailed spawn enforces the write policy
# ---------------------------------------------------------------------------


@_REQUIRE_SEATBELT
def test_seatbelt_jail_executes_real_write_policy_on_macos(tmp_path: Path) -> None:
    """A REAL jailed spawn enforces the seatbelt write policy on macOS.

    This executes sandbox semantics rather than only constructing the
    profile object: it builds the production ``sandbox-exec -p <profile>``
    prefix via :func:`build_jail_argv`, then runs two tiny ``/bin/sh``
    writes under it. The bar:

    - a write to a path OUTSIDE the confined cwd is BLOCKED -- the spawn
      exits non-zero and the target file is never created (the seatbelt
      ``(deny default)`` floor with no write-allow for that subtree); and
    - a write to a path INSIDE the confined cwd PASSES -- the spawn exits
      zero and the file lands (the ``(allow file-write* (subpath <cwd>))``
      carve-out).

    Together these prove the ``-f`` -> ``-p`` flag fix actually reaches the
    kernel's enforcement: with the old ``-f`` form ``sandbox-exec`` would
    have read the inline profile text as a missing filename and failed both
    spawns before any policy ran.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    prefix = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="darwin", home=home)
    # Sanity: this is the inline-profile form the fix emits.
    assert prefix[:2] == ["sandbox-exec", "-p"]

    # A denied target OUTSIDE the writable cwd (and outside the profile's
    # /private/tmp temp carve-outs, since tmp_path resolves elsewhere).
    denied_dir = tmp_path / "outside"
    denied_dir.mkdir()
    denied_target = denied_dir / "blocked.txt"
    allowed_target = cwd / "allowed.txt"

    denied = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo nope > {denied_target}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert denied.returncode != 0, (
        f"denied write should be blocked; got rc=0 stderr={denied.stderr!r}"
    )
    assert not denied_target.exists(), "blocked write must never create the file"

    allowed = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo hi > {allowed_target}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, (
        f"allowed write under cwd should pass; rc={allowed.returncode} stderr={allowed.stderr!r}"
    )
    assert allowed_target.read_text().strip() == "hi"


@_REQUIRE_SEATBELT
def test_seatbelt_jail_old_file_flag_form_would_fail_to_run(tmp_path: Path) -> None:
    """Regression guard: the old ``-f`` form fails the spawn before policy runs.

    Feeding the SAME inline profile text under ``-f`` makes ``sandbox-exec``
    look for a file named by the profile text -- it does not exist, so the
    spawn exits non-zero and reports the profile text as a missing path. This
    pins WHY the flag had to change: ``-f`` never reaches enforcement at all.
    """
    _root, cwd = _repo_with_cwd(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=home)
    broken = subprocess.run(
        ["sandbox-exec", "-f", profile, "/usr/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert broken.returncode != 0
    # ``-f`` resolved the profile text AS a path: depending on its length the
    # OS reports either "No such file or directory" or "File name too long" --
    # both prove the spawn failed before any seatbelt policy ran.
    assert "No such file or directory" in broken.stderr or "File name too long" in broken.stderr


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


def test_build_seatbelt_profile_claude_carves_out_state_dir_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS claude lane permits WRITE under ~/.claude (session-env scratch).

    Regression for the EPERM bash gap: Claude Code's Bash tool stages its
    session-env + shell-snapshot under ~/.claude at init; without this
    write-allow the whole Bash lane dies with EPERM.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    claude_dir = str((tmp_path / ".claude").resolve())
    assert f'(allow file-write* (subpath "{claude_dir}"))' in profile


def test_build_seatbelt_profile_claude_state_dir_respects_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$CLAUDE_CONFIG_DIR redirects the claude write carve-out."""
    override = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(override))
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    assert f'(allow file-write* (subpath "{override}"))' in profile
    # The default ~/.claude is NOT write-allowed when the override is set.
    default_dir = str((tmp_path / ".claude").resolve())
    assert f'(allow file-write* (subpath "{default_dir}"))' not in profile


def test_jail_claude_state_dir_rw_bound_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux claude lane rw-binds ~/.claude so its Bash/exec lane can write."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root, cwd = _repo_with_cwd(tmp_path)
    (tmp_path / ".claude").mkdir()
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    claude_dir = str((tmp_path / ".claude").resolve())
    # A --bind (read-write, not --ro-bind) pair for the claude state dir.
    bind_pairs = [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind" and argv[i + 1] == claude_dir
    ]
    assert claude_dir in bind_pairs


def test_jail_codex_state_dir_rw_bound_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux codex lane rw-binds $CODEX_HOME (shell-scratch parity with macOS)."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    root, cwd = _repo_with_cwd(tmp_path)
    (tmp_path / ".codex").mkdir()
    argv = build_jail_argv(_CODEX, cwd=cwd, root=root, platform="linux", home=tmp_path)
    codex_dir = str((tmp_path / ".codex").resolve())
    bind_pairs = [
        argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind" and argv[i + 1] == codex_dir
    ]
    assert codex_dir in bind_pairs


def test_build_seatbelt_profile_codex_carves_out_own_cred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex lane permits read of ``$CODEX_HOME/auth.json``'s dir."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path)
    codex_dir = str((tmp_path / ".codex").resolve())
    assert f'(allow file-read* (subpath "{codex_dir}"))' in profile


def test_build_seatbelt_profile_opencode_carves_out_own_cred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode lane permits read of its data-dir ``auth.json``'s dir."""
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_OPENCODE, home=tmp_path)
    opencode_dir = str((tmp_path / ".local" / "share" / "opencode").resolve())
    assert f'(allow file-read* (subpath "{opencode_dir}"))' in profile


def test_build_seatbelt_profile_egress_tls_floor_w37(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W37: shared floor permits direct egress + keychain/trust mach for TLS.

    A spawned runtime CLI reaches its model API over direct HTTPS (eawf's UDS
    egress proxy is un-wired + speaks a protocol third-party CLIs can't use),
    and TLS cert validation needs securityd + trustd. The codex lane also gets
    WRITE to its own ``$CODEX_HOME`` (app-server runtime state), while the broad
    Darwin per-user temp stays DENIED -- temp staging is redirected to the
    pinned ``$TMPDIR`` (an allowed write subpath).
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path)
    assert "(allow network*)" in profile
    assert "(allow system-socket)" in profile
    assert 'mach-lookup (global-name "com.apple.securityd")' in profile
    assert 'mach-lookup (global-name "com.apple.trustd")' in profile
    codex_dir = str((tmp_path / ".codex").resolve())
    assert f'(allow file-write* (subpath "{codex_dir}"))' in profile
    # Write confinement stays tight: the broad Darwin per-user temp root is
    # NOT write-allowed (temp staging is redirected via the pinned TMPDIR).
    # Match the exact broad-root line so the deeper cwd allow (which, in this
    # test, resolves under /private/var/folders) does not false-match.
    assert '(allow file-write* (subpath "/private/var/folders"))' not in profile


def test_build_seatbelt_profile_unknown_runtime_raises(tmp_path: Path) -> None:
    """An unknown runtime lane raises ValueError naming the value."""
    _root, cwd = _repo_with_cwd(tmp_path)
    with pytest.raises(ValueError, match="unknown runtime lane"):
        build_seatbelt_profile(cwd=cwd, runtime="gemini-cli", home=tmp_path)


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
        jail_command(
            ["x"], runtime="gemini-cli", cwd=cwd, root=root, platform="linux", home=tmp_path
        )


def test_jail_command_opencode_lane_jails(tmp_path: Path) -> None:
    """The opencode lane is a known lane: jail_command wraps its argv (no raise)."""
    root, cwd = _repo_with_cwd(tmp_path)
    jailed = jail_command(
        ["opencode", "run"],
        runtime=_OPENCODE,
        cwd=cwd,
        root=root,
        platform="linux",
        home=tmp_path,
    )
    assert jailed[0] == "bwrap"
    assert jailed[-2:] == ["opencode", "run"]


# ---------------------------------------------------------------------------
# Wiring: the live spawn jails (or falls back) without losing env / session
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


_ENVELOPE: bytes = b'{"result":"ok","session_id":"s","usage":{"input_tokens":1,"output_tokens":1}}'


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Patch ``create_subprocess_exec`` to capture argv + kwargs, no real spawn."""
    proc = _FakeProcess(stdout=_ENVELOPE, returncode=0)

    async def _fake_exec(*argv: str, **kwargs: object) -> _FakeProcess:
        captured["argv"] = list(argv)
        captured.update(kwargs)
        proc.open_streams()
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
    # The faked win32 platform makes the real ``shutil.which`` (in
    # resolve_binary_dir) take shutil's Windows branch, which needs ``_winapi``
    # (absent off-Windows). Binary resolution isn't under test here, so stub it.
    monkeypatch.setattr(claude_adapter, "resolve_binary_dir", lambda _binary: None)

    captured: dict[str, object] = {}
    _patch_spawn(monkeypatch, captured)

    adapter = ClaudeAdapter()
    result = asyncio.run(adapter.spawn_session("p", model="m", cwd=str(cwd)))
    assert result.text == "ok"
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] == "claude"
