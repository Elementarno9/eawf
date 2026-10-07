"""Unit tests for :mod:`eawf.runtime.sandbox.jail`.

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

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.sandbox.cwd_guard import CwdGuardError
from eawf.runtime.sandbox.env_scrub import pinned_tmpdir
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


#: The cred-deny entries that are DIRECTORIES on a real host (tmpfs-masked)
#: and the ones that are FILES (masked by a null-device bind instead --
#: ``--tmpfs`` cannot cover a regular file).
_CRED_DENY_FILES: tuple[str, ...] = (".npmrc", ".pypirc")
_CRED_DENY_SUBDIRS: tuple[str, ...] = tuple(
    rel for rel in _CRED_DENY_DIRS if rel not in _CRED_DENY_FILES
)

#: The real-bwrap launch tests run ONLY on Linux with ``bwrap`` actually
#: present: they execute the kernel's namespace/mount enforcement, so they
#: must skip everywhere else (this macOS dev host, a Linux without the
#: binary) instead of failing. The ubuntu-24.04 ``linux-jail`` CI job
#: installs bubblewrap so they EXECUTE there.
_bwrap_unavailable = sys.platform != "linux" or shutil.which("bwrap") is None
_REQUIRE_BWRAP = pytest.mark.skipif(
    _bwrap_unavailable,
    reason="real bubblewrap launch smoke needs Linux + bwrap on PATH",
)


def _materialise_cred_paths(home: Path) -> None:
    """Create every cross-tool cred path under *home* as its real shape.

    The Linux backend masks only what EXISTS (bwrap cannot create a mount
    point inside the read-only root bind), so a test that asserts the masks
    must first put the dirs / files on disk the way a real host has them.
    """
    for rel in _CRED_DENY_SUBDIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    for rel in _CRED_DENY_FILES:
        (home / rel).write_text("placeholder\n", encoding="utf-8")


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
    """Every PRESENT cross-tool cred path is masked inside the Linux jail.

    A cred DIRECTORY is covered by an empty tmpfs; a cred FILE (.npmrc /
    .pypirc) is covered by a bind of the null device, since ``--tmpfs``
    cannot mount over a regular file.

    The device bind must be ``--dev-bind``: ``--ro-bind`` mounts ``nodev``,
    so the mask lands and the launch survives but the read fails EACCES
    instead of returning empty. Asserting the exact flag is the only way an
    argv-shape test can catch that, since both spellings produce an
    identically-shaped argv.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    _materialise_cred_paths(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    tmpfs_targets = [argv[i + 1] for i, token in enumerate(argv) if token == "--tmpfs"]
    null_masked = [
        argv[i + 2]
        for i, token in enumerate(argv)
        if token == "--dev-bind" and argv[i + 1] == "/dev/null"
    ]
    assert not any(
        token == "--ro-bind" and argv[i + 1] == "/dev/null" for i, token in enumerate(argv)
    ), "a read-only bind mounts nodev, so the null-device mask reads EACCES rather than empty"
    for rel in _CRED_DENY_SUBDIRS:
        assert str((tmp_path / rel).resolve()) in tmpfs_targets
    for rel in _CRED_DENY_FILES:
        assert str((tmp_path / rel).resolve()) in null_masked


def test_build_jail_argv_linux_skips_masks_for_absent_cred_dirs(tmp_path: Path) -> None:
    """REL-034: an ABSENT cred dir is not masked -- masking it kills the launch.

    bwrap has to create each mount point, and the read-only root bind makes
    that ``mkdir`` fail with EROFS, so an unconditional mask over a cred dir
    the host does not have aborts bwrap before the child ever runs. Nothing
    under HOME exists here, so no cred mask may be emitted at all.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    home = tmp_path / "empty-home"
    home.mkdir()
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=home)
    tmpfs_targets = [argv[i + 1] for i, token in enumerate(argv) if token == "--tmpfs"]
    for rel in _CRED_DENY_DIRS:
        assert str((home / rel).resolve()) not in tmpfs_targets
    # Only the pinned-TMPDIR tmpfs survives.
    assert tmpfs_targets == [pinned_tmpdir("linux")]


def test_build_jail_argv_linux_mounts_writable_tmpfs_at_pinned_tmpdir(tmp_path: Path) -> None:
    """REL-034: the jail mounts a writable tmpfs at the env scrub's pinned TMPDIR.

    The read-only root bind leaves the host temp dir unwritable, so without
    this the child's pinned ``$TMPDIR`` is a read-only path and every socket
    / PATH alias the CLI stages there fails.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    tmpfs_targets = [argv[i + 1] for i, token in enumerate(argv) if token == "--tmpfs"]
    assert pinned_tmpdir("linux") in tmpfs_targets


def test_build_jail_argv_linux_mounts_tmpfs_before_the_cwd_bind(tmp_path: Path) -> None:
    """The TMPDIR tmpfs precedes the cwd bind so a cwd under /tmp survives it.

    bwrap applies mounts in argv order: a tmpfs mounted at /tmp AFTER the
    worktree bind would shadow a worktree that lives under /tmp (every CI
    checkout in a temp dir), leaving the child confined to an empty tree.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    argv = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path)
    tmpfs_idx = next(
        i for i, token in enumerate(argv) if token == "--tmpfs" and argv[i + 1] == "/tmp"
    )
    bind_idx = next(
        i for i, token in enumerate(argv) if token == "--bind" and argv[i + 1] == str(cwd.resolve())
    )
    assert tmpfs_idx < bind_idx


# ---------------------------------------------------------------------------
# Linux semantics smoke: a REAL bwrap launch on a Linux host
# ---------------------------------------------------------------------------


@_REQUIRE_BWRAP
def test_bwrap_jail_linux_launch_runs_a_trivial_command(tmp_path: Path) -> None:
    """REL-034: a trivial jailed command LAUNCHES and exits 0 on a Linux host.

    The argv-shape tests above cannot see a launch abort: bwrap validated
    the old prefix's mounts at run time and died before the child ran, so
    the jail was dead on Linux while every unit test stayed green. This
    executes the production prefix against real bubblewrap, which is the
    only oracle for "the jail actually launches".
    """
    root, cwd = _repo_with_cwd(tmp_path)
    home = cwd / "home"
    _materialise_cred_paths(home)
    prefix = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=home)
    assert prefix[0] == "bwrap"

    launched = subprocess.run(
        [*prefix, "/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert launched.returncode == 0, (
        f"jailed launch should exit 0; rc={launched.returncode} stderr={launched.stderr!r}"
    )


@_REQUIRE_BWRAP
def test_bwrap_jail_linux_launch_enforces_write_and_cred_policy(tmp_path: Path) -> None:
    """The launched Linux jail confines writes and masks the cred paths.

    Proves the fix did not buy the launch by giving up confinement: a write
    inside the cwd lands on the host, a write outside it never does, the
    pinned ``$TMPDIR`` is writable but private, and both cred shapes (a
    masked DIRECTORY and a masked FILE) read empty inside the jail.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    home = cwd / "home"
    _materialise_cred_paths(home)
    (home / ".aws" / "credentials").write_text("placeholder-cred\n", encoding="utf-8")
    prefix = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="linux", home=home)

    allowed_target = cwd / "allowed.txt"
    denied_target = root / "outside.txt"
    tmp_probe = f"{pinned_tmpdir('linux')}/eawf-jail-probe.txt"

    allowed = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo hi > {allowed_target}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, f"cwd write should pass; stderr={allowed.stderr!r}"
    assert allowed_target.read_text().strip() == "hi"

    subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo nope > {denied_target}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not denied_target.exists(), "a write outside the cwd must never reach the host"

    temp_write = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo tmp-ok > {tmp_probe} && cat {tmp_probe}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert temp_write.returncode == 0, f"pinned TMPDIR must be writable; {temp_write.stderr!r}"
    assert temp_write.stdout.strip() == "tmp-ok"
    assert not Path(tmp_probe).exists(), "the jail's temp is private, not the host's"

    masked_file = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"cat {home / '.npmrc'}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert masked_file.returncode == 0
    assert masked_file.stdout == "", "a masked cred FILE must read as empty"

    masked_dir = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"cat {home / '.aws' / 'credentials'}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert masked_dir.returncode != 0, "a masked cred DIR must hide its contents"
    assert masked_dir.stdout == ""


@_REQUIRE_BWRAP
def test_bwrap_jail_linux_launch_fails_when_masking_an_absent_dir(tmp_path: Path) -> None:
    """Regression guard: the pre-fix unconditional mask aborts the launch.

    Feeding bwrap the SAME shape with a tmpfs over a path that does not
    exist under the read-only root makes it fail to create the mount point
    and exit non-zero before any child runs. This pins WHY the mask had to
    become conditional -- and reds again if the ``exists`` filter is
    dropped.
    """
    absent = "/eawf-absent-cred-dir"
    assert not Path(absent).exists()
    broken = subprocess.run(
        [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            absent,
            "--unshare-pid",
            "--die-with-parent",
            "/bin/true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert broken.returncode != 0, "masking an absent path must abort the launch"
    assert "eawf-absent-cred-dir" in broken.stderr or "Read-only" in broken.stderr


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

    # A denied target OUTSIDE the writable cwd and outside every temp
    # carve-out. tmp_path cannot hold it: under a TMPDIR pinned to the
    # /private/tmp carve-out (as the gate environment pins it) the target
    # would be writable and the check would prove nothing. The per-user
    # Darwin temp is the directory the profile deliberately never opens.
    user_temp = subprocess.run(
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
    ).stdout.strip()
    denied_dir = Path(tempfile.mkdtemp(dir=user_temp))
    denied_target = denied_dir / "blocked.txt"
    allowed_target = cwd / "allowed.txt"

    try:
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
    finally:
        shutil.rmtree(denied_dir, ignore_errors=True)

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


def test_build_seatbelt_profile_allows_dev_write(tmp_path: Path) -> None:
    """The profile permits /dev writes so git + shell /dev/null redirects work.

    Regression for the jailed-git gap: the (deny default) floor blocked a
    /dev/null WRITE, which broke `git commit` in a jailed agent even after the
    own-home carve-out let bash init.
    """
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)
    assert '(allow file-write* (subpath "/dev"))' in profile


@_REQUIRE_SEATBELT
def test_seatbelt_jail_allows_dev_null_write_on_macos(tmp_path: Path) -> None:
    """A REAL jailed spawn can redirect to /dev/null (git + tools need it)."""
    root, cwd = _repo_with_cwd(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    prefix = build_jail_argv(_CLAUDE, cwd=cwd, root=root, platform="darwin", home=home)
    result = subprocess.run(
        [*prefix, "/bin/sh", "-c", "echo hi > /dev/null"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"/dev/null write should pass; stderr={result.stderr!r}"


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


#: The clauses codex needs to synchronize its managed-preference domain.
_PREFERENCE_IPC: tuple[str, ...] = (
    '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") '
    '(global-name "com.apple.cfprefsd.agent"))',
    '(allow ipc-posix-shm-read-data (ipc-posix-name-prefix "apple.cfprefs."))',
)


def test_build_seatbelt_profile_codex_reaches_the_preferences_daemon(tmp_path: Path) -> None:
    """codex refuses every thread unless its managed-preference domain synchronizes."""
    _root, cwd = _repo_with_cwd(tmp_path)
    lines = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path).splitlines()
    for clause in _PREFERENCE_IPC:
        assert lines.count(clause) == 1
    assert not any("user-preference-write" in line for line in lines)


@pytest.mark.parametrize("runtime", [_CLAUDE, _OPENCODE])
def test_build_seatbelt_profile_other_lanes_get_no_preference_ipc(
    runtime: str, tmp_path: Path
) -> None:
    """Only the codex lane is opened to the preferences daemon."""
    _root, cwd = _repo_with_cwd(tmp_path)
    profile = build_seatbelt_profile(cwd=cwd, runtime=runtime, home=tmp_path)
    assert "cfprefs" not in profile


@_REQUIRE_SEATBELT
def test_seatbelt_codex_jail_still_refuses_a_preference_write(tmp_path: Path) -> None:
    """The preferences daemon writes on the child's behalf only if the profile allows it.

    Reaching the daemon must not become a way around the write confinement,
    so a jailed ``defaults write`` to a throwaway domain is refused and the
    host never sees the value.
    """
    root, cwd = _repo_with_cwd(tmp_path)
    prefix = build_jail_argv(_CODEX, cwd=cwd, root=root, platform="darwin")
    domain = f"eawf.jail-probe.{tmp_path.name}"
    try:
        written = subprocess.run(
            [*prefix, "/usr/bin/defaults", "write", domain, "key", "jailed"],
            capture_output=True,
            text=True,
            check=False,
        )
        read_back = subprocess.run(
            ["/usr/bin/defaults", "read", domain, "key"],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        subprocess.run(["/usr/bin/defaults", "delete", domain], capture_output=True, check=False)
    assert written.returncode != 0
    assert read_back.returncode != 0


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
    """Every cross-tool cred dir present on the host is denied on both platforms."""
    root, cwd = _repo_with_cwd(tmp_path)
    _materialise_cred_paths(tmp_path)
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
# The Claude launcher's jail prefix: jailed, or unjailed with a warning
# ---------------------------------------------------------------------------


def test_maybe_jail_argv_jails_when_wrapper_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the wrapper on PATH the child argv is prefixed by the jail."""
    root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "linux")
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(claude_adapter, "_repo_root_for", lambda _path: root)

    argv = claude_adapter._maybe_jail_argv(["claude"], runtime=_CLAUDE, cwd=str(cwd))

    assert argv[0] == "bwrap"
    assert argv[-1] == "claude"


def test_maybe_jail_argv_falls_back_unjailed_when_wrapper_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the wrapper binary the child argv is returned unchanged."""
    root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "linux")
    monkeypatch.setattr(claude_adapter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(claude_adapter, "_repo_root_for", lambda _path: root)

    assert claude_adapter._maybe_jail_argv(["claude"], runtime=_CLAUDE, cwd=str(cwd)) == ["claude"]


def test_maybe_jail_argv_unjailed_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On Windows (no jail parity) the argv is returned unchanged, never raising."""
    _root, cwd = _repo_with_cwd(tmp_path)
    monkeypatch.setattr(claude_adapter.sys, "platform", "win32")

    assert claude_adapter._maybe_jail_argv(["claude"], runtime=_CLAUDE, cwd=str(cwd)) == ["claude"]


# ---------------------------------------------------------------------------
# A private home replaces the operator's codex home as the writable state dir
# ---------------------------------------------------------------------------


def _private_home(cwd: Path, operator: Path | None) -> Path:
    """Return a private home under *cwd*, linking *operator*'s auth file when given."""
    private = cwd / ".eawf-mcp" / "codex-home"
    private.mkdir(parents=True)
    if operator is not None:
        (private / "auth.json").symlink_to(operator / "auth.json")
    return private


def _operator_codex_home(home: Path) -> Path:
    """Return an operator ``~/.codex`` holding an auth file and a config."""
    operator = home / ".codex"
    operator.mkdir(parents=True)
    (operator / "auth.json").write_text("{}\n", encoding="utf-8")
    (operator / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
    return operator


def test_build_seatbelt_profile_private_home_grants_no_operator_home_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    operator = _operator_codex_home(tmp_path)
    private = _private_home(cwd, operator)

    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path, private_home=private)

    operator_dir = str(operator.resolve())
    assert f'(allow file-write* (subpath "{operator_dir}"))' not in profile
    assert f'(allow file-write* (subpath "{private.resolve()}"))' in profile
    assert f'(allow file-write* (literal "{operator_dir}/auth.json"))' in profile
    writes = [line for line in profile.splitlines() if "file-write" in line]
    assert [line for line in writes if operator_dir in line] == [
        f'(allow file-write* (literal "{operator_dir}/auth.json"))'
    ]


def test_build_seatbelt_profile_private_home_without_link_writes_no_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    private = _private_home(cwd, None)

    profile = build_seatbelt_profile(cwd=cwd, runtime=_CODEX, home=tmp_path, private_home=private)

    operator_dir = str((tmp_path / ".codex").resolve())
    writes = [line for line in profile.splitlines() if "file-write" in line]
    assert not [line for line in writes if "(literal " in line and '"/dev/' not in line]
    assert not [line for line in writes if operator_dir in line]


def test_jail_private_home_rw_bound_linux_and_operator_home_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    root, cwd = _repo_with_cwd(tmp_path)
    operator = _operator_codex_home(tmp_path)
    private = _private_home(cwd, operator)

    argv = build_jail_argv(
        _CODEX, cwd=cwd, root=root, platform="linux", home=tmp_path, private_home=private
    )

    binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
    assert str(private) in binds
    assert str((operator / "auth.json").resolve()) in binds
    assert str(operator.resolve()) not in binds


@_REQUIRE_SEATBELT
def test_seatbelt_private_home_blocks_operator_home_writes_on_macos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL jailed spawn can write its private home and the linked auth file only.

    The operator home sits in the per-user Darwin temp, which the profile
    never opens, so a write there proves the private-home grant is the only
    state-dir grant rather than riding on a temp carve-out.
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    root, cwd = _repo_with_cwd(tmp_path)
    user_temp = subprocess.run(
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
    ).stdout.strip()
    home = Path(tempfile.mkdtemp(dir=user_temp)).resolve()
    try:
        operator = _operator_codex_home(home)
        private = _private_home(cwd, operator)
        prefix = build_jail_argv(
            _CODEX, cwd=cwd, root=root, platform="darwin", home=home, private_home=private
        )

        def run(script: str) -> int:
            return subprocess.run(
                [*prefix, "/bin/sh", "-c", script], capture_output=True, check=False
            ).returncode

        config_before = (operator / "config.toml").read_bytes()
        assert run(f"echo x >> {operator / 'config.toml'}") != 0
        assert run(f"echo x > {operator / 'new-file'}") != 0
        assert run(f"echo ok > {private / 'state'}") == 0
        assert run(f"echo refreshed > {private / 'auth.json'}") == 0
        assert (operator / "config.toml").read_bytes() == config_before
        assert not (operator / "new-file").exists()
        assert (operator / "auth.json").read_text().strip() == "refreshed"
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# A native Run writes only its workspace, its isolation directory, the
# canonical repository's admin entry of its worktree and a few device nodes
# ---------------------------------------------------------------------------

#: The device nodes a native Run's seatbelt profile opens for writing.
_NATIVE_DEVICE_WRITES: frozenset[str] = frozenset(
    {
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/zero"))',
        '(allow file-write* (literal "/dev/tty"))',
        '(allow file-write* (literal "/dev/dtracehelper"))',
        '(allow file-write* (literal "/dev/stdout"))',
        '(allow file-write* (literal "/dev/stderr"))',
        '(allow file-write* (subpath "/dev/fd"))',
    }
)

#: The private home each lane's launcher hands the jail.
_PRIVATE_HOME_NAMES: dict[str, str] = {_CLAUDE: "claude-config", _CODEX: "codex-home"}


def _native_home(cwd: Path, runtime: str) -> Path:
    """Return *runtime*'s private home inside the isolation directory under *cwd*."""
    private = cwd / ".eawf-mcp" / _PRIVATE_HOME_NAMES[runtime]
    private.mkdir(parents=True)
    return private


def _write_grants(profile: str) -> set[str]:
    """Return the profile's write-allow clauses."""
    return {line for line in profile.splitlines() if line.startswith("(allow file-write*")}


def _linked_worktree(base: Path) -> tuple[Path, Path, Path]:
    """Return ``(canonical repo, its admin entry, workspace)`` linked as git links them."""
    canon = base / "canon"
    admin = canon / ".git" / "worktrees" / "ws"
    admin.mkdir(parents=True)
    (canon / ".git" / "objects").mkdir()
    (canon / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    workspace = base / "ws"
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{workspace / '.git'}\n", encoding="utf-8")
    return canon, admin, workspace


@pytest.mark.parametrize("runtime", [_CLAUDE, _CODEX])
def test_native_profile_writes_only_workspace_private_home_and_device_nodes(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    _root, cwd = _repo_with_cwd(tmp_path)
    private = _native_home(cwd, runtime)

    profile = build_seatbelt_profile(cwd=cwd, runtime=runtime, home=tmp_path, private_home=private)

    assert _write_grants(profile) == {
        f'(allow file-write* (subpath "{cwd.resolve()}"))',
        f'(allow file-write* (subpath "{private.resolve()}"))',
        *_NATIVE_DEVICE_WRITES,
    }
    # The agent CLI calls its model API directly, so network stays open.
    assert "(allow network*)" in profile


def test_headless_profile_keeps_temp_dev_and_state_dir_writes(tmp_path: Path) -> None:
    """Without a private home the headless spawns keep their temp and state grants."""
    _root, cwd = _repo_with_cwd(tmp_path)

    profile = build_seatbelt_profile(cwd=cwd, runtime=_CLAUDE, home=tmp_path)

    grants = _write_grants(profile)
    assert f'(allow file-write* (subpath "{pinned_tmpdir("darwin")}"))' in grants
    assert '(allow file-write* (subpath "/private/var/tmp"))' in grants
    assert '(allow file-write* (subpath "/dev"))' in grants


def test_native_profile_grants_only_the_worktree_entry_of_the_canonical_repo(
    tmp_path: Path,
) -> None:
    canon, admin, workspace = _linked_worktree(tmp_path)
    private = _native_home(workspace, _CLAUDE)

    profile = build_seatbelt_profile(
        cwd=workspace, runtime=_CLAUDE, home=tmp_path, private_home=private
    )

    canon_grants = [line for line in _write_grants(profile) if str(canon.resolve()) in line]
    assert canon_grants == [f'(allow file-write* (subpath "{admin.resolve()}"))']


def _point_elsewhere(admin: Path, workspace: Path) -> None:
    (admin / "gitdir").write_text(f"{admin.parent / 'other' / '.git'}\n", encoding="utf-8")


def _not_a_pointer(admin: Path, workspace: Path) -> None:
    (workspace / ".git").write_text(f"{admin}\n", encoding="utf-8")


def _undecodable(admin: Path, workspace: Path) -> None:
    (workspace / ".git").write_bytes(b"gitdir: \xff\xfe\n")


def _outside_worktrees(admin: Path, workspace: Path) -> None:
    hooks = admin.parent.parent / "hooks"
    hooks.mkdir()
    (hooks / "gitdir").write_text(f"{workspace / '.git'}\n", encoding="utf-8")
    (workspace / ".git").write_text(f"gitdir: {hooks}\n", encoding="utf-8")


def _no_backlink(admin: Path, workspace: Path) -> None:
    (admin / "gitdir").unlink()


def _plain_repository(admin: Path, workspace: Path) -> None:
    (workspace / ".git").unlink()
    (workspace / ".git").mkdir()


@pytest.mark.parametrize(
    "tamper",
    [
        _point_elsewhere,
        _not_a_pointer,
        _undecodable,
        _outside_worktrees,
        _no_backlink,
        _plain_repository,
    ],
)
def test_native_profile_grants_nothing_in_the_canonical_repo_without_a_backlinked_entry(
    tamper: Callable[[Path, Path], None], tmp_path: Path
) -> None:
    """A ``.git`` pointer a Run could have rewritten opens nothing on its own."""
    canon, admin, workspace = _linked_worktree(tmp_path)
    tamper(admin, workspace)
    private = _native_home(workspace, _CODEX)

    profile = build_seatbelt_profile(
        cwd=workspace, runtime=_CODEX, home=tmp_path, private_home=private
    )

    assert not [line for line in _write_grants(profile) if str(canon.resolve()) in line]


def test_native_linux_argv_has_no_host_temp_and_binds_the_worktree_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    canon, admin, workspace = _linked_worktree(tmp_path)
    private = _native_home(workspace, _CODEX)

    argv = build_jail_argv(
        _CODEX, cwd=workspace, root=workspace, platform="linux", home=tmp_path, private_home=private
    )

    tmpfs_targets = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
    binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
    assert pinned_tmpdir("linux") not in tmpfs_targets
    assert binds == [str(workspace.resolve()), str(private), str(admin.resolve())]
    assert str(canon.resolve()) not in binds
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv


def test_headless_linux_argv_keeps_the_temp_mount(tmp_path: Path) -> None:
    root, cwd = _repo_with_cwd(tmp_path)

    argv = build_jail_argv(_CODEX, cwd=cwd, root=root, platform="linux", home=tmp_path)

    tmpfs_targets = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
    assert pinned_tmpdir("linux") in tmpfs_targets


@pytest.mark.parametrize(
    ("secure_storage", "config_dir", "login_dir"),
    [
        (None, None, ".claude"),
        ("", "configured", "configured"),
        ("pinned", "configured", "pinned"),
    ],
)
def test_native_linux_claude_binds_only_the_stored_login_file(
    secure_storage: str | None,
    config_dir: str | None,
    login_dir: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off macOS the login file itself is bound, never the directory holding it."""
    for name, value in (
        ("CLAUDE_SECURESTORAGE_CONFIG_DIR", secure_storage),
        ("CLAUDE_CONFIG_DIR", config_dir),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, str(tmp_path / value) if value else "")
    login = tmp_path / login_dir / ".credentials.json"
    login.parent.mkdir()
    login.write_text("{}\n", encoding="utf-8")
    root, cwd = _repo_with_cwd(tmp_path)
    private = _native_home(cwd, _CLAUDE)

    argv = build_jail_argv(
        _CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path, private_home=private
    )

    binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
    assert binds == [str(cwd.resolve()), str(private), str(login.resolve())]


def test_native_linux_claude_without_a_login_file_binds_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    root, cwd = _repo_with_cwd(tmp_path)
    private = _native_home(cwd, _CLAUDE)

    argv = build_jail_argv(
        _CLAUDE, cwd=cwd, root=root, platform="linux", home=tmp_path, private_home=private
    )

    binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--bind"]
    assert binds == [str(cwd.resolve()), str(private)]


def _user_temp_dir() -> Path:
    """Return a fresh directory in the per-user Darwin temp no profile opens."""
    user_temp = subprocess.run(
        ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return Path(tempfile.mkdtemp(dir=user_temp)).resolve()


@_REQUIRE_SEATBELT
@pytest.mark.parametrize("runtime", [_CLAUDE, _CODEX])
def test_seatbelt_native_run_refuses_writes_outside_workspace_and_isolation_dir(
    runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL jailed spawn: shared temp, canonical .git, operator home and /dev are closed."""
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    base = _user_temp_dir()
    other = Path(tempfile.mkdtemp(dir="/private/tmp")).resolve()
    var_tmp = Path("/private/var/tmp") / f"{other.name}-refused"
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".codex").mkdir()
        canon, admin, workspace = _linked_worktree(base)
        private = _native_home(workspace, runtime)
        run_tmp = workspace / ".eawf-mcp" / "tmp"
        run_tmp.mkdir()
        prefix = build_jail_argv(
            runtime,
            cwd=workspace,
            root=workspace,
            platform="darwin",
            home=home,
            private_home=private,
        )

        def write(target: Path) -> int:
            return subprocess.run(
                [*prefix, "/bin/sh", "-c", f"echo x >> {target}"], capture_output=True, check=False
            ).returncode

        refused = [
            other / "other-run.txt",
            var_tmp,
            canon / ".git" / "config",
            canon / ".git" / "objects" / "planted",
            canon / ".git" / "HEAD",
            home / ".claude" / "settings.json",
            home / ".codex" / "config.toml",
            Path("/dev/urandom"),
        ]
        allowed = [
            workspace / "edit.txt",
            workspace / ".eawf-mcp" / "binding.json",
            private / "state.json",
            run_tmp / "scratch",
            admin / "index.lock",
            Path("/dev/null"),
        ]
        assert [str(target) for target in refused if write(target) == 0] == []
        assert [str(target) for target in allowed if write(target) != 0] == []
        assert not list(other.iterdir())
        assert not var_tmp.exists()
        assert (canon / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"
        assert not (canon / ".git" / "HEAD").exists()
        assert not list((home / ".claude").iterdir())
        assert not list((home / ".codex").iterdir())
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


@_REQUIRE_SEATBELT
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")
def test_seatbelt_native_run_in_a_real_worktree_cannot_write_the_object_store() -> None:
    """Git's own worktree layout opens its admin entry and nothing else of the repository."""
    base = _user_temp_dir()
    try:
        canon = base / "canon"
        workspace = base / "ws"
        identity = {
            "GIT_AUTHOR_NAME": "jail",
            "GIT_AUTHOR_EMAIL": "jail@example.invalid",
            "GIT_COMMITTER_NAME": "jail",
            "GIT_COMMITTER_EMAIL": "jail@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin",
            "HOME": str(base),
        }

        def git(*args: str, cwd: Path = base) -> str:
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", *args],
                cwd=cwd,
                env=identity,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        git("init", "-q", str(canon))
        tree = git("hash-object", "-t", "tree", "-w", "/dev/null", cwd=canon)
        commit = git("commit-tree", tree, "-m", "init", cwd=canon)
        git("update-ref", "refs/heads/main", commit, cwd=canon)
        git("worktree", "add", "-q", str(workspace), "-b", "run", "main", cwd=canon)
        (workspace / "edit.txt").write_text("edit\n", encoding="utf-8")
        private = _native_home(workspace, _CODEX)
        run_tmp = workspace / ".eawf-mcp" / "tmp"
        run_tmp.mkdir()
        prefix = build_jail_argv(
            _CODEX,
            cwd=workspace,
            root=workspace,
            platform="darwin",
            home=base,
            private_home=private,
        )
        objects = canon / ".git" / "objects"
        objects_before = sorted(path.name for path in objects.rglob("*"))

        def jailed_git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*prefix, "git", "-c", "core.hooksPath=/dev/null", "-C", str(workspace), *args],
                env={**identity, "TMPDIR": str(run_tmp)},
                capture_output=True,
                text=True,
                check=False,
            )

        status = jailed_git("status", "--porcelain")
        added = jailed_git("add", "edit.txt")

        assert status.returncode == 0, status.stderr
        assert "edit.txt" in status.stdout
        assert added.returncode != 0
        assert sorted(path.name for path in objects.rglob("*")) == objects_before
    finally:
        shutil.rmtree(base, ignore_errors=True)


#: Reads one path inside the jail and prints ``ok`` or the refusing errno.
_READ_PROGRAM = """\
import os, sys
try:
    path = sys.argv[1]
    os.listdir(path) if os.path.isdir(path) else open(path, "rb").read()
    print("ok")
except OSError as error:
    print(f"refused {error.errno}")
"""


@_REQUIRE_SEATBELT
@pytest.mark.parametrize("runtime", [_CLAUDE, _CODEX])
def test_seatbelt_native_run_cannot_read_daemon_storage_but_reads_its_lease(
    runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A REAL jailed read of the tree's .ea is refused; the lease leased under it is not."""
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    base = _user_temp_dir()
    try:
        storage = base / "repo" / ".ea"
        storage.mkdir(parents=True)
        (storage / "state.json").write_text("{}", encoding="utf-8")
        workspace = storage / "worktrees" / "ws"
        workspace.mkdir(parents=True)
        (workspace / "edit.txt").write_text("edit\n", encoding="utf-8")
        sibling = storage / "worktrees" / "other"
        sibling.mkdir()
        (sibling / "edit.txt").write_text("other\n", encoding="utf-8")
        wal = base / "runtime" / "wal"
        wal.mkdir(parents=True)
        (wal / "segment").write_text("w", encoding="utf-8")
        private = _native_home(workspace, runtime)
        (private / "state.json").write_text("{}", encoding="utf-8")
        home = base / "home"
        home.mkdir()
        prefix = build_jail_argv(
            runtime,
            cwd=workspace,
            root=workspace,
            platform="darwin",
            home=home,
            private_home=private,
            storage_dirs=(storage, wal),
        )

        def read(target: Path) -> str:
            ran = subprocess.run(
                [*prefix, sys.executable, "-I", "-c", _READ_PROGRAM, str(target)],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            assert ran.returncode == 0, ran.stderr
            return ran.stdout.strip()

        refused = [storage, storage / "state.json", sibling / "edit.txt", wal / "segment"]
        readable = [workspace, workspace / "edit.txt", private / "state.json", wal.parent]
        assert {str(path): read(path) for path in refused} == {
            str(path): "refused 1" for path in refused
        }
        assert {str(path): read(path) for path in readable} == {
            str(path): "ok" for path in readable
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)
