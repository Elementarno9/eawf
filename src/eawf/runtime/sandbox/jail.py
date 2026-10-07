"""Per-OS filesystem container confining a spawned agent to its worktree.

A spawned agent's child process (``claude -p`` / codex) must be confined
to its worktree -- read-only everywhere, writable ONLY under its cwd --
and denied read of cross-tool credential dirs (``~/.aws``, ``~/.ssh``,
...), while still being permitted to read its OWN account credential. This
module builds the OS-native container around that child:

- **Linux** -- a ``bwrap`` (bubblewrap) argv PREFIX: a read-only bind of
  the root, a writable tmpfs at the pinned ``$TMPDIR``, a single read-write
  bind of the validated cwd, ``--unshare-pid`` with ``--die-with-parent``
  (BOTH required -- bubblewrap issue #529), and masks over the cross-tool
  credential dirs that are PRESENT on the host (bwrap cannot create a
  missing mount point inside the read-only root, so masking an absent path
  aborts the launch).
- **macOS** -- a ``sandbox-exec -p <profile>`` argv PREFIX over a generated
  seatbelt profile (``(deny default)`` + ``(allow process*)`` + a
  read allowlist that denies the cross-tool cred dirs + an
  ``(allow file-write* (subpath CWD))`` clause). The profile rides INLINE
  in the argv, so the flag is ``-p`` (inline-profile-string form), NOT
  ``-f`` (file-path form): ``-f`` reads a profile FROM a file and so treats
  the inline text as a missing filename, failing the spawn before it runs.

The module returns an argv PREFIX, never a launched process: the daemon
stays the SOLE session-setter. The spawn keeps
``create_subprocess_exec(*jailed_argv, env=build_child_env(runtime),
cwd=cwd, start_new_session=True)`` -- the wrapper becomes ``argv[0]``, the
daemon's ``start_new_session=True`` setsid lands on the wrapper, the
wrapper is the group leader, ``on_spawn(pid)`` returns its pid, and
``cancel_process_group(os.getpgid(pid))`` reaps the whole tree UNCHANGED.

The single load-bearing correctness invariant: the wrapper MUST NOT create
a new session / process group of its own -- so this module emits NO
``--new-session`` (bwrap) and wraps in NO second ``setsid``. The one
session is the daemon's; a regression here silently leaks runaway process
trees out of the kill ladder's reach. See
``.ea/local/research/2026-06-02-s-jail-0-srt-double-fork.md`` for the
spike that proved the reap depends on it.

Windows gap (honest, decision-ready -- it does NOT block the build):
Windows has no bubblewrap / seatbelt parity. We do NOT attempt a Windows
token-jail here. Callers branch on :func:`jail_supported` (``False`` on
Windows) or catch :class:`JailUnavailableOnWindowsError`; the
operator-facing Windows policy is env-scrub + ACL write-scope +
offline-by-default, or WSL2. This mirrors the egress proxy's Windows-gap
pattern (:mod:`eawf.runtime.sandbox.egress_proxy`).

Authoritative spec:
``.ea/local/research/2026-05-30-safety-floor.md`` (the "denyRead carve-out"
+ "Platform matrix" sections) and the W03 spike verdict
``.ea/local/research/2026-06-02-s-jail-0-srt-double-fork.md``.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from eawf.runtime.sandbox.cwd_guard import assert_cwd_inside
from eawf.runtime.sandbox.egress_proxy import SandboxError
from eawf.runtime.sandbox.env_scrub import pinned_tmpdir

logger = logging.getLogger(__name__)

#: The auth lanes a jail can be built for, keyed on the runtime adapter id
#: (matching :data:`~eawf.runtime.sandbox.env_scrub._CLAUDE_RUNTIME` /
#: ``_CODEX_RUNTIME`` / ``_OPENCODE_RUNTIME``). An unknown runtime is a
#: fail-fast ValueError.
_CLAUDE_RUNTIME: str = "claude-code"
_CODEX_RUNTIME: str = "codex"
_OPENCODE_RUNTIME: str = "opencode"
_KNOWN_RUNTIMES: frozenset[str] = frozenset({_CLAUDE_RUNTIME, _CODEX_RUNTIME, _OPENCODE_RUNTIME})

#: Cross-tool credential directories the jail DENIES read of, named
#: relative to ``$HOME`` (the ``~`` is expanded against the child's HOME at
#: argv-build time). These are the credential stores of OTHER tools the
#: agent has no business reading -- denying them blocks cred-exfil even if
#: the agent is prompt-injected. The agent's OWN account credential is
#: carved back out below so the agent can still authenticate.
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

#: Per-lane OWN-credential carve-out: the ONE $HOME-relative path the jail
#: must still permit read of so the spawning agent can authenticate. On
#: Linux/Windows the claude subscription credential is an on-disk file; on
#: macOS it lives in the encrypted Keychain instead (handled separately in
#: the seatbelt profile, see ``_KEYCHAIN_READ_SUBPATHS``).
_CLAUDE_OWN_CRED: str = ".claude/.credentials.json"  # pragma: allowlist secret

#: macOS Keychain read subpaths the seatbelt profile permits so the claude
#: subscription credential (Keychain item ``claude-code``) is reachable
#: while the cross-tool $HOME cred dirs stay denied. The user keychain
#: directory holds the keychain DB; ``/private/var/db/mds`` backs the
#: keychain/securityd metadata lookups.
_KEYCHAIN_READ_SUBPATHS: tuple[str, ...] = ("Library/Keychains",)

#: The empty-file mask source for the Linux backend. ``--tmpfs`` can only
#: cover a DIRECTORY, so the file-shaped entries of :data:`_CRED_DENY_DIRS`
#: (``.npmrc`` / ``.pypirc``) are masked by binding the null device over
#: them instead -- they then read as empty inside the jail rather than
#: aborting the launch with a "not a directory" mount failure.
_NULL_DEVICE: str = "/dev/null"

#: The bwrap flag the null-device mask MUST use. ``--ro-bind`` mounts
#: ``nodev``, so a device node bound through it cannot be opened: the mask
#: lands, the launch survives, and the read fails EACCES instead of
#: returning empty. ``--dev-bind`` is the documented variant that permits
#: device access. That distinction is invisible to an argv-shape assertion
#: and only appears against a real kernel, which is why the mask read as
#: empty in every unit test and denied on the first real-bwrap run.
#:
#: The bind is read-write because bwrap offers no read-only device bind.
#: That grants nothing: a write to ``/dev/null`` is discarded by
#: definition, and the host credential stays shadowed underneath.
_NULL_DEVICE_BIND_FLAG: str = "--dev-bind"

#: The device nodes a native Run may open for writing under the seatbelt
#: profile, in place of the whole of ``/dev``. Shells, git and node redirect
#: to the null and zero devices, ``/dev/tty`` is the controlling terminal
#: (the Run has none, so opening it fails rather than reaching a terminal),
#: ``/dev/fd`` names the process's own descriptors, and the dtrace helper is
#: opened by every node process at start. Every other node -- other
#: terminals, disks, ``/dev/random`` writes -- stays refused.
_DEVICE_WRITE_LITERALS: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/stdout",
    "/dev/stderr",
)
_DEVICE_WRITE_SUBPATHS: tuple[str, ...] = ("/dev/fd",)


class JailUnavailableOnWindowsError(SandboxError):
    """Raised when an OS filesystem jail is requested on Windows.

    Windows has no bubblewrap / seatbelt parity, so this module builds no
    Windows container. Callers that cannot branch on :func:`jail_supported`
    catch this to fall back to the env-scrub + ACL write-scope +
    offline-by-default policy (or WSL2).
    """


def jail_supported(platform: str | None = None) -> bool:
    """Return ``True`` when an OS filesystem jail can be built here.

    POSIX (Linux / macOS) has a bubblewrap / seatbelt container; Windows
    does not. The spawn seam checks this (alongside the wrapper binary
    resolving on PATH) before prefixing the jail so a Windows host degrades
    to the documented offline-default policy instead of crashing.

    Args:
        platform: Platform string to test, injectable for tests. Defaults
            to :data:`sys.platform`; ``"win32"`` (or :data:`os.name` ==
            ``"nt"`` when *platform* is ``None``) is unsupported.

    Returns:
        ``True`` on Linux / macOS, ``False`` on Windows.
    """
    if platform is not None:
        return platform != "win32"
    return os.name != "nt" and sys.platform != "win32"


def _assert_known_runtime(runtime: str) -> None:
    """Reject an unknown runtime lane before building any argv.

    Args:
        runtime: The runtime adapter id selecting the auth lane.

    Raises:
        ValueError: When *runtime* is not a known auth lane.
    """
    if runtime not in _KNOWN_RUNTIMES:
        raise ValueError(f"unknown runtime lane: {runtime!r}")


def _own_cred_abspath(runtime: str, *, home: Path) -> Path:
    """Return the $HOME-relative own-credential path for *runtime*.

    Args:
        runtime: The runtime adapter id (``"claude-code"`` / ``"codex"``).
        home: The child's HOME the credential is resolved against.

    Returns:
        The absolute own-credential path. For codex this is
        ``$CODEX_HOME/auth.json`` resolved from the environment when set,
        else ``~/.codex/auth.json``; for opencode it is the data-dir
        ``auth.json`` (``$XDG_DATA_HOME/opencode/auth.json`` when set, else
        ``~/.local/share/opencode/auth.json``); for claude it is
        ``.credentials.json`` in the stored-login directory --
        ``$CLAUDE_SECURESTORAGE_CONFIG_DIR`` when non-empty, else
        ``$CLAUDE_CONFIG_DIR``, else ``~/.claude`` (the macOS Keychain
        carve-out is applied separately in the seatbelt profile).
    """
    if runtime == _CODEX_RUNTIME:
        codex_home = os.environ.get("CODEX_HOME")
        base = Path(codex_home) if codex_home else home / ".codex"
        return base / "auth.json"
    if runtime == _OPENCODE_RUNTIME:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data) if xdg_data else home / ".local" / "share"
        return base / "opencode" / "auth.json"
    configured = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR") or os.environ.get(
        "CLAUDE_CONFIG_DIR"
    )
    if configured:
        return Path(configured) / Path(_CLAUDE_OWN_CRED).name
    return home / _CLAUDE_OWN_CRED


def _own_state_dir(runtime: str, *, home: Path) -> Path:
    """Return the writable state dir the runtime stages session scratch under.

    Every lane's CLI writes session scratch -- shell snapshots, session-env,
    sockets, PATH aliases -- under its own state dir at init, so that dir
    must be READ-WRITE inside the jail or the tool's Bash/exec lane dies with
    EPERM. For claude this is ``$CLAUDE_CONFIG_DIR`` when set else
    ``~/.claude`` (Claude Code's Bash tool stages its session-env +
    shell-snapshot there); for codex it is ``$CODEX_HOME`` (else ``~/.codex``)
    and for opencode its data dir -- both the parent of the own-cred file.

    Args:
        runtime: The runtime adapter id selecting the lane.
        home: The child's HOME the state dir resolves against.

    Returns:
        The absolute writable state dir for *runtime*.
    """
    if runtime == _CLAUDE_RUNTIME:
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(override) if override else home / ".claude"
    return _own_cred_abspath(runtime, home=home).parent


def _linked_credential(private_home: Path, *, runtime: str, home: Path) -> Path | None:
    """Return the file the private home's credential link resolves to, or ``None``.

    The link is the one operator file a private home carries. It stays
    writable through the link because a token refresh rotates the stored
    refresh token: a refresh that could not be written back would leave
    the operator holding a token the provider already retired.

    Args:
        private_home: The Run's own state dir.
        runtime: The runtime adapter id naming the credential file.
        home: The child's HOME the operator's credential resolves against.

    Returns:
        The resolved link target, or ``None`` when the private home holds
        no credential link.
    """
    link = private_home / _own_cred_abspath(runtime, home=home).name
    return link.resolve(strict=False) if link.is_symlink() else None


def _worktree_admin_dir(cwd: Path) -> Path | None:
    """Return the canonical repository's admin entry for the worktree at *cwd*.

    Git keeps a linked worktree's index, ``HEAD`` and locks under the main
    repository's ``.git/worktrees/<name>``, outside the worktree itself, so
    git in the worktree cannot work unless that one directory is writable.
    The ``.git`` file naming it is inside the workspace and so is writable
    to an earlier Run: the entry is trusted only when git's own back-link,
    ``<entry>/gitdir``, names this worktree's ``.git`` file, which a Run
    cannot arrange for any entry but its own.

    Args:
        cwd: The leased workspace.

    Returns:
        The admin entry, or ``None`` when *cwd* is not a linked worktree or
        the entry does not link back to it.
    """
    dot_git = cwd / ".git"
    if not dot_git.is_file():
        return None
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return None
        admin = (cwd / pointer.removeprefix("gitdir:").strip()).resolve(strict=False)
        backlink = (admin / "gitdir").read_text(encoding="utf-8").strip()
    except OSError, UnicodeDecodeError:
        return None
    if admin.parent.name != "worktrees" or not admin.is_dir():
        return None
    if Path(backlink).resolve(strict=False) != dot_git.resolve(strict=False):
        return None
    return admin


def _storage_read_carve_outs(storage_dirs: Sequence[Path], *, kept: Sequence[Path]) -> list[str]:
    """Return the seatbelt rules re-opening reads a storage deny would close.

    A leased worktree lives under the tree's ``.ea``, so the deny over the
    daemon storage would also close the workspace and everything in it.
    Seatbelt lets the later rule win, so each kept directory under a denied
    one is re-allowed after the deny, and the directories between them get
    their metadata back: resolving the workspace path stats every one.

    Args:
        storage_dirs: The resolved directories whose reads are denied.
        kept: The resolved directories the Run must still read.

    Returns:
        The allow rules, empty when no kept directory lies under a denied one.
    """
    rules: list[str] = []
    for storage in storage_dirs:
        for keep in kept:
            if keep != storage and not keep.is_relative_to(storage):
                continue
            between = [keep, *keep.parents]
            rules.extend(
                f'(allow file-read-metadata (literal "{step}"))'
                for step in between[: between.index(storage) + 1]
                if step != keep
            )
            rules.append(f'(allow file-read* (subpath "{keep}"))')
    return list(dict.fromkeys(rules))


def build_seatbelt_profile(
    *,
    cwd: Path,
    runtime: str,
    home: Path | None = None,
    private_home: Path | None = None,
    storage_dirs: Sequence[Path] = (),
) -> str:
    """Build the macOS seatbelt profile text for the jailed child.

    The profile is ``(deny default)`` with: ``(allow process*)`` (the
    child must still exec); a broad ``(allow file-read*)`` narrowed by
    explicit ``(deny file-read* (subpath <cred-dir>))`` clauses over the
    cross-tool credential dirs; the per-lane OWN-credential carve-out
    (claude: the encrypted Keychain read subpaths, NOT a $HOME file; codex:
    ``$CODEX_HOME/auth.json``); and a single
    ``(allow file-write* (subpath <cwd>))`` clause confining writes to the
    worktree. It emits NO setsid / session directive -- ``sandbox-exec``
    exec's in-place, preserving the daemon-set process group.

    Args:
        cwd: The validated worktree directory writes are confined to.
        runtime: The runtime adapter id selecting the own-cred carve-out.
        home: The child's HOME the cred dirs resolve against. Defaults to
            :func:`pathlib.Path.home`.
        private_home: The Run's own state dir inside its isolation
            directory. Given, the profile is a native Run's: writes reach
            only *cwd*, this dir, the target of its credential link, the
            canonical repository's admin entry for a worktree *cwd* and a
            fixed set of device nodes -- no temp area, no other ``/dev``
            node and no operator state dir. ``None`` keeps the operator's
            state dir, the shared temp areas and all of ``/dev`` writable
            for the headless spawns that still stage there.
        storage_dirs: The daemon's storage directories a native Run may
            not read. Each is denied, then *cwd* and *private_home* are
            re-opened when they lie under one.

    Returns:
        The seatbelt profile source text.

    Raises:
        ValueError: When *runtime* is not a known auth lane.
    """
    _assert_known_runtime(runtime)
    home_dir = home if home is not None else Path.home()
    cwd_abs = cwd.resolve(strict=False)
    denied_storage = [path.resolve(strict=False) for path in storage_dirs]

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        # The child must still exec the inner runtime + its tools.
        "(allow process-exec)",
        "(allow process-fork)",
        # System libraries / dyld need broad read; the cred-dir denies
        # below subtract the sensitive subtrees.
        "(allow file-read*)",
        "(allow sysctl-read)",
    ]

    # Deny read of every cross-tool credential dir.
    for rel in _CRED_DENY_DIRS:
        cred_path = (home_dir / rel).resolve(strict=False)
        lines.append(f'(deny file-read* (subpath "{cred_path}"))')

    # The daemon's own state is the daemon's to read; a Run reaches it only
    # through its semantic tools.
    lines.extend(f'(deny file-read* (subpath "{path}"))' for path in denied_storage)
    kept = [cwd_abs] if private_home is None else [cwd_abs, private_home.resolve(strict=False)]
    lines.extend(_storage_read_carve_outs(denied_storage, kept=kept))

    # Shared network + TLS floor. A spawned runtime CLI reaches its model API
    # over direct HTTPS: eawf's UDS egress proxy speaks a custom CONNECT
    # protocol third-party CLIs (codex / claude) cannot use AND is started by
    # no spawn path, so the jail itself must permit outbound network. This
    # seatbelt therefore restricts NO destination: outbound network is
    # unrestricted until the provider-native sandbox lands, and the jail's
    # teeth are the cred-read denies above plus the write confinement below
    # (the agent still cannot read another tool's credentials or write outside
    # its tree). TLS server-cert validation needs the keychain / trust daemons
    # (securityd + trustd); without them the handshake fails with
    # errSecNoKeychain (-25291). The CLIs stage their runtime sockets + PATH
    # aliases under ``$TMPDIR``, which the env scrub pins to ``/private/tmp``
    # (an allowed write subpath for a headless spawn; a native Run's TMPDIR
    # is in its isolation directory instead) so the broad Darwin per-user
    # temp (/private/var/folders) never has to be opened -- keeping write
    # confinement tight.
    lines.append("(allow network*)")
    lines.append("(allow system-socket)")
    lines.append('(allow mach-lookup (global-name "com.apple.SecurityServer"))')
    lines.append('(allow mach-lookup (global-name "com.apple.securityd"))')
    lines.append('(allow mach-lookup (global-name "com.apple.trustd"))')
    lines.append('(allow mach-lookup (global-name "com.apple.trustd.agent"))')

    # Own-credential carve-out so the agent can still authenticate.
    if runtime == _CLAUDE_RUNTIME:
        # macOS divergence: the claude subscription credential is in the
        # encrypted Keychain, not a $HOME file. Permit the Keychain read
        # subpaths while the cred dirs stay denied above (the keychain IPC
        # mach-lookups are in the shared TLS floor).
        for rel in _KEYCHAIN_READ_SUBPATHS:
            keychain_path = (home_dir / rel).resolve(strict=False)
            lines.append(f'(allow file-read* (subpath "{keychain_path}"))')
    else:
        # codex / opencode keep their own-cred file readable (broad read
        # already covers it, but the parent dir stays outside the cred denies).
        own_cred = _own_cred_abspath(runtime, home=home_dir)
        lines.append(f'(allow file-read* (subpath "{own_cred.parent}"))')

    if runtime == _CODEX_RUNTIME:
        # codex reads administrator policy from forced macOS preferences and
        # refuses to load any configuration when CFPreferencesAppSynchronize
        # fails, which it does under (deny default): the call needs the
        # preferences daemon and its read-only shared-memory cache. Reads
        # gain nothing the broad file-read above does not already give,
        # since the preference plists are ordinary files; writes stay
        # refused, because cfprefsd still checks user-preference-write
        # against this profile before writing on the child's behalf.
        lines.append(
            '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") '
            '(global-name "com.apple.cfprefsd.agent"))'
        )
        lines.append('(allow ipc-posix-shm-read-data (ipc-posix-name-prefix "apple.cfprefs."))')

    # Confine writes to the worktree cwd; still no broad $HOME or
    # /private/var/folders write.
    lines.append(f'(allow file-write* (subpath "{cwd_abs}"))')

    if private_home is not None:
        # A native Run: its TMPDIR and its state dirs live in its isolation
        # directory, so the shared temp areas, the operator's state dir and
        # the rest of /dev stay closed. Network stays open: the agent CLI
        # calls its model API directly.
        lines.append(f'(allow file-write* (subpath "{private_home.resolve(strict=False)}"))')
        linked = _linked_credential(private_home, runtime=runtime, home=home_dir)
        if linked is not None:
            lines.append(f'(allow file-write* (literal "{linked}"))')
        admin = _worktree_admin_dir(cwd_abs)
        if admin is not None:
            lines.append(f'(allow file-write* (subpath "{admin}"))')
        lines.extend(f'(allow file-write* (literal "{node}"))' for node in _DEVICE_WRITE_LITERALS)
        lines.extend(f'(allow file-write* (subpath "{node}"))' for node in _DEVICE_WRITE_SUBPATHS)
        return "\n".join(lines) + "\n"

    # Every lane stages session scratch (shell snapshots, session-env,
    # sockets, PATH aliases) under its own state dir, so that dir must be
    # WRITABLE. For claude this is ~/.claude (or $CLAUDE_CONFIG_DIR): Claude
    # Code's Bash tool writes its session-env + shell-snapshot there at init,
    # and without this the whole Bash lane dies with EPERM -- a filesystem
    # WRITE gap, not exec/PATH. codex/opencode need write under $CODEX_HOME /
    # the data dir for their app-server runtime state.
    state_dir = _own_state_dir(runtime, home=home_dir)
    lines.append(f'(allow file-write* (subpath "{state_dir}"))')

    # The temp areas the env scrub pins a headless spawn's TMPDIR to.
    lines.append(f'(allow file-write* (subpath "{pinned_tmpdir("darwin")}"))')
    lines.append('(allow file-write* (subpath "/private/var/tmp"))')

    # Device nodes: git + countless shell tools redirect to /dev/null (and read
    # /dev/random etc.); the (deny default) floor blocks a /dev/null WRITE, which
    # surfaced as a "/dev/null permission issue" that broke `git commit` in a
    # jailed agent even after the own-home carve-out let bash init.
    lines.append('(allow file-write* (subpath "/dev"))')

    return "\n".join(lines) + "\n"


def _native_run_binds(cwd: Path, *, runtime: str, home: Path, private_home: Path) -> list[str]:
    """Return the bubblewrap read-write binds a native Run gets beside its workspace.

    Args:
        cwd: The resolved leased workspace.
        runtime: The runtime adapter id naming the credential.
        home: The child's HOME the operator's credential resolves against.
        private_home: The Run's own state dir.

    Returns:
        ``--bind`` triples for the private home, the target of its
        credential link, the claude login file and the canonical
        repository's admin entry of a worktree *cwd*, each when present.
    """
    binds = ["--bind", os.fspath(private_home), os.fspath(private_home)]
    linked = _linked_credential(private_home, runtime=runtime, home=home)
    if linked is not None and linked.exists():
        binds += ["--bind", os.fspath(linked), os.fspath(linked)]
    if runtime == _CLAUDE_RUNTIME:
        # Off macOS the claude login is a file, which Claude Code opens with
        # O_NOFOLLOW and so cannot reach through a link the way codex reaches
        # auth.json; the file itself is bound writable for a token refresh
        # to land, and nothing else beside it.
        stored_login = _own_cred_abspath(runtime, home=home).resolve(strict=False)
        if stored_login.is_file():
            binds += ["--bind", os.fspath(stored_login), os.fspath(stored_login)]
    admin = _worktree_admin_dir(cwd)
    if admin is not None:
        binds += ["--bind", os.fspath(admin), os.fspath(admin)]
    return binds


def _build_linux_argv(
    *,
    cwd: Path,
    runtime: str,
    home: Path,
    private_home: Path | None = None,
    storage_dirs: Sequence[Path] = (),
) -> list[str]:
    """Build the bubblewrap argv prefix for the jailed child.

    Read-only-binds the root, mounts a writable tmpfs at the pinned
    ``$TMPDIR`` (headless spawns only; a native Run, named by
    *private_home*, gets no host temp), read-write-binds the validated cwd
    (plus, for a native Run, its private home, its credential and the
    canonical repository's admin entry of a worktree cwd), masks the cross-tool
    cred dirs, and sets ``--unshare-pid`` + ``--die-with-parent`` TOGETHER
    (both required for the reap to cascade into grandchildren -- bubblewrap
    #529). Emits NO ``--new-session`` so bwrap stays in the daemon-set
    process group (the outer setsid already covers TIOCSTI).

    Two ordering / shape constraints keep bwrap from ABORTING the launch
    (it exits non-zero before the child ever runs, so a violation leaves a
    dead jail rather than a weak one):

    - every mount point must already exist, because the read-only root bind
      leaves bwrap unable to ``mkdir`` one. So a cred dir is masked only
      when it is present on the host, and the mask shape follows the
      entry's type: ``--tmpfs`` over a directory, a null-device
      ``--dev-bind`` over a file (see :data:`_NULL_DEVICE_BIND_FLAG` for
      why the read-only bind cannot carry a device node).
    - the ``$TMPDIR`` tmpfs is mounted BEFORE the cwd bind, so a worktree
      that happens to live under the temp root is not shadowed by it.
    """
    cwd_abs = cwd.resolve(strict=False)
    # The one source of truth the env scrub also seeds TMPDIR from, so the
    # child's $TMPDIR and the jail's writable temp mount are the same path
    # by construction. Linux always has a pin; the None (Windows) case
    # cannot reach here because the caller already refused Windows.
    tmpdir = pinned_tmpdir("linux")
    # Read-only view of the whole root, with a private /dev holding only
    # the standard nodes...
    argv: list[str] = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if private_home is None:
        # ...a private writable temp at the pinned $TMPDIR (the read-only
        # root leaves the real temp dir unwritable, which strands every
        # temp write the sandboxed CLI stages there). A native Run's
        # TMPDIR is in its isolation directory, so it gets none and the
        # host temp stays read-only to it.
        argv += ["--tmpfs", str(tmpdir)]
    # The daemon's storage is masked by an empty tmpfs before the cwd bind,
    # so a worktree leased under the tree's .ea is bound back on top of the
    # mask. An absent directory is skipped: its mount point cannot be made
    # inside the read-only root.
    for storage in storage_dirs:
        if storage.is_dir():
            argv += ["--tmpfs", os.fspath(storage.resolve(strict=False))]
    argv += [
        # ...and a single writable bind of the worktree cwd, AFTER the
        # tmpfs so a cwd under the temp root survives it.
        "--bind",
        os.fspath(cwd_abs),
        os.fspath(cwd_abs),
        # Reap-critical pair: BOTH required (bubblewrap #529).
        "--unshare-pid",
        "--die-with-parent",
    ]

    # Mask each cross-tool cred dir that EXISTS so its contents are
    # unreadable inside the jail. An absent path is skipped: bwrap would
    # have to create the mount point inside the read-only root bind, which
    # fails with EROFS and kills the launch outright.
    for rel in _CRED_DENY_DIRS:
        cred_path = (home / rel).resolve(strict=False)
        if cred_path.is_dir():
            argv += ["--tmpfs", os.fspath(cred_path)]
        elif cred_path.is_file():
            argv += [_NULL_DEVICE_BIND_FLAG, _NULL_DEVICE, os.fspath(cred_path)]

    # Re-expose the agent's OWN state dir READ-WRITE after the tmpfs masks so
    # the carve-out wins over the deny: the runtime stages session scratch
    # (shell snapshots, session-env, sockets, PATH aliases) there and its
    # Bash/exec lane dies with EPERM without write. claude -> ~/.claude (or
    # $CLAUDE_CONFIG_DIR); codex -> $CODEX_HOME; opencode -> its data dir.
    # None sit under a masked cred dir on the default layout. The rw bind also
    # re-exposes the own-cred file inside that dir, so no separate ro-bind is
    # needed.
    if private_home is not None:
        return argv + _native_run_binds(
            cwd_abs, runtime=runtime, home=home, private_home=private_home
        )
    state_dir = _own_state_dir(runtime, home=home)
    if state_dir.exists():
        argv += ["--bind", os.fspath(state_dir), os.fspath(state_dir)]

    return argv


def build_jail_argv(
    runtime: str,
    *,
    cwd: Path,
    root: Path,
    platform: str | None = None,
    home: Path | None = None,
    private_home: Path | None = None,
    storage_dirs: Sequence[Path] = (),
) -> list[str]:
    """Build the OS jail argv PREFIX confining a child to *cwd*.

    The returned list is prepended to the child's own argv; the daemon
    execs ``*(prefix + argv)`` with ``start_new_session=True`` so the
    wrapper inherits the daemon-set session and stays the group leader. The
    prefix NEVER creates a session of its own (no ``--new-session``, no
    ``setsid``).

    *cwd* is validated to resolve inside *root* via
    :func:`~eawf.runtime.sandbox.cwd_guard.assert_cwd_inside` (reusing the
    canonical containment predicate, not a re-implementation) so a cwd that
    escapes the repo root is refused before any argv is emitted.

    Args:
        runtime: The runtime adapter id selecting the own-cred carve-out
            (``"claude-code"`` / ``"codex"``).
        cwd: The worktree directory the child is confined to (read-write);
            everything else is read-only. Must exist and resolve inside
            *root*.
        root: The repo root *cwd* must sit inside.
        platform: Platform string, injectable for tests. Defaults to
            :data:`sys.platform`; ``"linux"`` -> bwrap, ``"darwin"`` ->
            sandbox-exec, ``"win32"`` -> raise.
        home: The child's HOME the cred dirs resolve against. Defaults to
            :func:`pathlib.Path.home`.
        private_home: The Run's own state dir, made writable in place of
            the operator's, which then gets no write grant at all. Given,
            the jail is a native Run's, which also closes the host temp
            areas and every device node a native Run does not use (see
            :func:`build_seatbelt_profile`).
        storage_dirs: The daemon's storage directories a native Run may
            not read; *cwd* and *private_home* stay readable beneath them.

    Returns:
        The argv prefix (``["bwrap", ...]`` on Linux, ``["sandbox-exec",
        "-p", <profile-text>]`` on macOS -- the inline-profile-string form,
        since the profile rides in the argv rather than on disk).

    Raises:
        JailUnavailableOnWindowsError: On Windows -- no FS-jail parity.
        ValueError: When *runtime* is not a known auth lane.
        CwdGuardError: When *cwd* resolves outside *root*.
        FileNotFoundError: When *cwd* does not exist (a jail must not
            confine a child to a path that is not there).
    """
    _assert_known_runtime(runtime)
    resolved_platform = platform if platform is not None else sys.platform
    if not jail_supported(resolved_platform):
        raise JailUnavailableOnWindowsError(
            f"filesystem jail unavailable on windows: platform={resolved_platform!r}; "
            "use env-scrub + acl write-scope + offline-default or wsl2"
        )

    assert_cwd_inside(cwd, root=root)
    if not cwd.exists():
        raise FileNotFoundError(f"jail cwd does not exist: {cwd!r}")

    home_dir = home if home is not None else Path.home()

    if resolved_platform == "darwin":
        profile = build_seatbelt_profile(
            cwd=cwd,
            runtime=runtime,
            home=home_dir,
            private_home=private_home,
            storage_dirs=storage_dirs,
        )
        logger.info(
            f"build_jail_argv runtime={runtime!r} platform={resolved_platform!r} "
            f"wrapper=sandbox-exec cwd={cwd.resolve(strict=False)!s}"
        )
        # Inline-profile form: ``-p`` takes the seatbelt source as a string
        # argument. ``-f`` would read it as a FILE path and fail the spawn.
        return ["sandbox-exec", "-p", profile]

    # Linux (the only remaining supported platform).
    argv = _build_linux_argv(
        cwd=cwd,
        runtime=runtime,
        home=home_dir,
        private_home=private_home,
        storage_dirs=storage_dirs,
    )
    logger.info(
        f"build_jail_argv runtime={runtime!r} platform={resolved_platform!r} "
        f"wrapper=bwrap cwd={cwd.resolve(strict=False)!s}"
    )
    return argv


def jail_command(
    argv: Sequence[str],
    *,
    runtime: str,
    cwd: Path,
    root: Path,
    platform: str | None = None,
    home: Path | None = None,
    private_home: Path | None = None,
    storage_dirs: Sequence[Path] = (),
) -> list[str]:
    """Return *argv* prefixed with the OS jail wrapper.

    Thin composition the spawn seam calls: builds the jail prefix and
    prepends it to the child's argv. The result is the exact argv vector
    the daemon execs (still under its own ``start_new_session=True``).

    Args:
        argv: The child's own argv (e.g. ``["claude", "-p", ...]``). Must
            be non-empty -- there is nothing to jail otherwise.
        runtime: The runtime adapter id selecting the own-cred carve-out.
        cwd: The worktree the child is confined to.
        root: The repo root *cwd* must sit inside.
        platform: Platform string, injectable for tests.
        home: The child's HOME the cred dirs resolve against.
        private_home: The Run's own state dir, writable in place of the
            operator's; given, the jail is a native Run's.
        storage_dirs: The daemon's storage directories a native Run may
            not read.

    Returns:
        ``build_jail_argv(...) + list(argv)``.

    Raises:
        ValueError: When *argv* is empty, or *runtime* is unknown.
        JailUnavailableOnWindowsError: On Windows.
        CwdGuardError: When *cwd* resolves outside *root*.
        FileNotFoundError: When *cwd* does not exist.
    """
    if not argv:
        raise ValueError("argv must be non-empty to jail")
    prefix = build_jail_argv(
        runtime,
        cwd=cwd,
        root=root,
        platform=platform,
        home=home,
        private_home=private_home,
        storage_dirs=storage_dirs,
    )
    return prefix + list(argv)


__all__ = [
    "JailUnavailableOnWindowsError",
    "build_jail_argv",
    "build_seatbelt_profile",
    "jail_command",
    "jail_supported",
]
