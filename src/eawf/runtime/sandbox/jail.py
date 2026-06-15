"""Per-OS filesystem container confining a spawned agent to its worktree.

A spawned agent's child process (``claude -p`` / codex) must be confined
to its worktree -- read-only everywhere, writable ONLY under its cwd --
and denied read of cross-tool credential dirs (``~/.aws``, ``~/.ssh``,
...), while still being permitted to read its OWN account credential. This
module builds the OS-native container around that child:

- **Linux** -- a ``bwrap`` (bubblewrap) argv PREFIX: a read-only bind of
  the root, a single read-write bind of the validated cwd, ``--unshare-pid``
  with ``--die-with-parent`` (BOTH required -- bubblewrap issue #529), and
  tmpfs masks over the cross-tool credential dirs.
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
        ``~/.claude/.credentials.json`` (the macOS Keychain carve-out is
        applied separately in the seatbelt profile).
    """
    if runtime == _CODEX_RUNTIME:
        codex_home = os.environ.get("CODEX_HOME")
        base = Path(codex_home) if codex_home else home / ".codex"
        return base / "auth.json"
    if runtime == _OPENCODE_RUNTIME:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg_data) if xdg_data else home / ".local" / "share"
        return base / "opencode" / "auth.json"
    return home / _CLAUDE_OWN_CRED


def build_seatbelt_profile(*, cwd: Path, runtime: str, home: Path | None = None) -> str:
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

    Returns:
        The seatbelt profile source text.

    Raises:
        ValueError: When *runtime* is not a known auth lane.
    """
    _assert_known_runtime(runtime)
    home_dir = home if home is not None else Path.home()
    cwd_abs = cwd.resolve(strict=False)

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

    # Shared egress + TLS floor (P30-I20-W37). A spawned runtime CLI reaches
    # its model API over direct HTTPS: eawf's UDS egress proxy speaks a custom
    # CONNECT protocol third-party CLIs (codex / claude) cannot use AND is not
    # wired into the spawn, so the jail itself must permit outbound network.
    # TLS server-cert validation needs the keychain / trust daemons (securityd
    # + trustd); without them the handshake fails with errSecNoKeychain
    # (-25291). The CLIs stage their runtime sockets + PATH aliases under
    # ``$TMPDIR``, which the env scrub pins to ``/private/tmp`` (an allowed
    # write subpath) so the broad Darwin per-user temp (/private/var/folders)
    # never has to be opened -- keeping write confinement tight. Host-scoped
    # egress classification for third-party CLIs (a standard HTTP-CONNECT proxy
    # in front of classify_egress) is the P31 follow-up; until it lands this
    # trades the (idle, un-wired) egress allow/deny for a functioning spawn,
    # while the cred-read denies above + the write confinement below stay
    # intact (the agent still cannot read another tool's credentials).
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
        # codex / opencode keep their own-cred file. Their app-server stages
        # runtime state (socket, sessions, PATH aliases) under that home, so
        # it needs WRITE there too -- not just the auth.json READ.
        own_cred = _own_cred_abspath(runtime, home=home_dir)
        lines.append(f'(allow file-read* (subpath "{own_cred.parent}"))')
        lines.append(f'(allow file-write* (subpath "{own_cred.parent}"))')

    # Confine writes to the worktree cwd + the pinned-TMPDIR temp areas; still
    # no broad $HOME or /private/var/folders write.
    lines.append(f'(allow file-write* (subpath "{cwd_abs}"))')
    lines.append('(allow file-write* (subpath "/private/tmp"))')
    lines.append('(allow file-write* (subpath "/private/var/tmp"))')

    return "\n".join(lines) + "\n"


def _build_linux_argv(*, cwd: Path, runtime: str, home: Path) -> list[str]:
    """Build the bubblewrap argv prefix for the jailed child.

    Read-only-binds the root, read-write-binds the validated cwd, masks
    the cross-tool cred dirs with tmpfs, and sets ``--unshare-pid`` +
    ``--die-with-parent`` TOGETHER (both required for the reap to cascade
    into grandchildren -- bubblewrap #529). Emits NO ``--new-session`` so
    bwrap stays in the daemon-set process group (the outer setsid already
    covers TIOCSTI).
    """
    cwd_abs = cwd.resolve(strict=False)
    argv: list[str] = [
        "bwrap",
        # Read-only view of the whole root...
        "--ro-bind",
        "/",
        "/",
        # ...with a single writable bind of the worktree cwd.
        "--bind",
        os.fspath(cwd_abs),
        os.fspath(cwd_abs),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        # Reap-critical pair: BOTH required (bubblewrap #529).
        "--unshare-pid",
        "--die-with-parent",
    ]

    # Mask each cross-tool cred dir with an empty tmpfs so its contents are
    # unreadable inside the jail.
    for rel in _CRED_DENY_DIRS:
        cred_path = (home / rel).resolve(strict=False)
        argv += ["--tmpfs", os.fspath(cred_path)]

    # Re-expose the agent's OWN credential read-only after the tmpfs masks,
    # so the carve-out wins over the deny. claude's cred file lives under
    # ~/.claude (NOT one of the masked dirs) so it survives unmasked;
    # codex's $CODEX_HOME/auth.json likewise. No extra bind is needed for
    # claude here, but a codex $CODEX_HOME that the operator placed under a
    # masked dir would need re-binding -- the default locations are not
    # masked, so the common path is already covered.
    own_cred = _own_cred_abspath(runtime, home=home)
    if own_cred.exists():
        argv += ["--ro-bind", os.fspath(own_cred), os.fspath(own_cred)]

    return argv


def build_jail_argv(
    runtime: str,
    *,
    cwd: Path,
    root: Path,
    platform: str | None = None,
    home: Path | None = None,
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
        profile = build_seatbelt_profile(cwd=cwd, runtime=runtime, home=home_dir)
        logger.info(
            f"build_jail_argv runtime={runtime!r} platform={resolved_platform!r} "
            f"wrapper=sandbox-exec cwd={cwd.resolve(strict=False)!s}"
        )
        # Inline-profile form: ``-p`` takes the seatbelt source as a string
        # argument. ``-f`` would read it as a FILE path and fail the spawn.
        return ["sandbox-exec", "-p", profile]

    # Linux (the only remaining supported platform).
    argv = _build_linux_argv(cwd=cwd, runtime=runtime, home=home_dir)
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
    )
    return prefix + list(argv)


__all__ = [
    "JailUnavailableOnWindowsError",
    "build_jail_argv",
    "build_seatbelt_profile",
    "jail_command",
    "jail_supported",
]
