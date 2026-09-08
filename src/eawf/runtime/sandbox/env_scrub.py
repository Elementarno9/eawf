"""Child-environment scrub: build a spawned runtime's env from an allowlist.

The one live agent spawn (``claude -p`` via
:meth:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.spawn_session`)
today inherits the FULL parent environment, carrying any ``AWS_*`` /
``GH_TOKEN`` / ``SSH_AUTH_SOCK`` credential straight into the child. This
module closes that cred-exfil-via-env gap by constructing the child env
from an ALLOWLIST rather than scrubbing a denylist: a denylist fails open
on the next unknown credential variable, so we start from empty (the
``env -i`` floor) and reseed only the minimal shared floor plus the
spawning lane's own auth.

Public API:

- :func:`build_child_env` -- returns the scrubbed env dict to hand to the
  subprocess ``env=`` kwarg.
- :func:`pinned_tmpdir` -- the per-platform ``TMPDIR`` pin, shared with the
  FS jail so the pinned path and the jail's write policy cannot drift.
- :data:`GATE_RUNTIME_LANE` -- the lane for a child that is not an agent CLI
  (a gate command), which therefore needs no vendor credential whatsoever.

The shared floor (every lane) keeps ``HOME``, a PINNED ``PATH`` floor
(never the parent ``PATH``), ``LANG`` / ``LC_*`` (defaulting to
``C.UTF-8`` when absent), and ``TERM``. Each lane adds only its own
account credential family (claude: ``CLAUDE_*`` / ``ANTHROPIC_*``; codex:
``CODEX_HOME`` / ``OPENAI_*``) and drops the cross-lane key. Everything
not on the allowlist -- ``AWS_*``, ``GH_*`` / ``GITHUB_*``, ``SSH_*``,
``KUBECONFIG``, ``EAWF_*`` daemon internals, and any unknown variable --
is absent by construction.

Authoritative Keep/Drop table + auth precedence:
``.ea/local/research/2026-05-30-safety-floor.md`` (section "The floor:
env-scrub + egress proxy").
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Runtime adapter ids mapped onto their auth lane. The claude id matches
#: :attr:`~eawf.runtime.runtimes.claude.adapter.ClaudeAdapter.id`; the
#: codex id mirrors the codex adapter's canonical id.
_CLAUDE_RUNTIME: str = "claude-code"
_CODEX_RUNTIME: str = "codex"
_OPENCODE_RUNTIME: str = "opencode"

#: Lane id for a child that is not an agent CLI at all: the audit-DSL
#: ``command_exit_zero`` gate and the out-of-process close child spawn a
#: repo tool, which needs the shared floor and nothing else. It is a lane
#: rather than a bare ``build_child_env`` call with an empty allowlist so
#: the "which credentials may this child see" answer stays in the one
#: table every other spawn reads.
GATE_RUNTIME_LANE: str = "gate"

#: The PINNED PATH floor. The parent ``PATH`` is deliberately NOT passed
#: through -- a spawned agent gets a fixed, minimal search path so a
#: hostile entry injected into the operator's ``PATH`` cannot reach the
#: child.
_PINNED_PATH: str = "/usr/bin:/bin:/usr/sbin:/sbin"

#: The PINNED TMPDIR, per platform. The parent ``TMPDIR`` (on macOS the
#: Darwin per-user temp ``/private/var/folders/...``) is deliberately NOT
#: passed through: the FS jail confines writes, so a sandboxed CLI that
#: stages runtime sockets / PATH aliases under ``$TMPDIR`` must point at a
#: path the jail policy allows writes to. Pinning it here lets the jail keep
#: write confinement tight (no broad ``/private/var/folders`` allow).
#:
#: The pin is PER-PLATFORM because the paths are not portable: macOS's real
#: temp dir is ``/private/tmp`` (``/tmp`` is a symlink to it), while on Linux
#: ``/private/tmp`` does not exist at all -- pinning it there hands the child
#: a ``$TMPDIR`` that no write can land in.
_PINNED_TMPDIR_DARWIN: str = "/private/tmp"
_PINNED_TMPDIR_POSIX: str = "/tmp"

#: Locale default seeded when the base env carries no ``LANG``.
_DEFAULT_LANG: str = "C.UTF-8"

#: Exact-match floor keys copied verbatim from the base env when present.
#: ``PATH`` is handled separately (pinned, never copied); ``LANG`` is
#: handled separately (defaulted when absent). ``USER`` / ``LOGNAME`` are the
#: POSIX identity vars the macOS keychain login-keychain lookup needs: without
#: them a sandboxed ``claude -p`` cannot resolve its stored OAuth credential and
#: exits "Not logged in" even though the jail permits the keychain. They are the
#: account name, not a secret -- as benign as ``HOME``.
_FLOOR_EXACT_KEYS: frozenset[str] = frozenset({"HOME", "TERM", "USER", "LOGNAME"})

#: Prefix whose every variable is a floor locale carry-through (``LC_ALL``,
#: ``LC_CTYPE``, ...). ``LANG`` itself is seeded separately with a default.
_FLOOR_PREFIXES: tuple[str, ...] = ("LC_",)

#: claude-lane auth: exact-match keys kept when present in the base env.
_CLAUDE_AUTH_EXACT: frozenset[str] = frozenset(
    {
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)

#: claude-lane auth: prefix families kept when present. ``ANTHROPIC_*`` is
#: an optional higher-precedence override, never required; ``CLAUDE_*``
#: covers the on-disk subscription config.
_CLAUDE_AUTH_PREFIXES: tuple[str, ...] = ("CLAUDE_", "ANTHROPIC_")

#: codex-lane auth: exact-match keys kept when present.
_CODEX_AUTH_EXACT: frozenset[str] = frozenset({"CODEX_HOME"})

#: codex-lane auth: prefix families kept when present. ``OPENAI_*`` is the
#: API-key fallback to the ChatGPT-auth ``auth.json`` primary.
_CODEX_AUTH_PREFIXES: tuple[str, ...] = ("OPENAI_",)

#: opencode-lane auth: opencode reads its own credential from its on-disk
#: data-dir store (OAuth-Claude ``auth.json``), not the env, so the lane
#: carries NO exact auth key -- only the ``OPENCODE_*`` family (data-dir
#: override + feature flags). The cross-lane ``ANTHROPIC_*`` / ``OPENAI_*``
#: credentials are dropped by omission.
_OPENCODE_AUTH_EXACT: frozenset[str] = frozenset()
_OPENCODE_AUTH_PREFIXES: tuple[str, ...] = ("OPENCODE_",)

#: gate-lane auth: a gate child runs a repo tool (``pytest`` / ``ruff`` /
#: ``git`` under ``uv run``), never a vendor agent CLI, so it authenticates
#: to nothing and the lane grants NO auth family at all. Every vendor
#: credential in the parent -- ``ANTHROPIC_*``, ``OPENAI_*``,
#: ``CLAUDE_CODE_OAUTH_TOKEN`` -- is dropped by omission alongside the
#: ``AWS_*`` / ``GH_*`` / ``SSH_*`` families the shared floor already drops.
_GATE_AUTH_EXACT: frozenset[str] = frozenset()
_GATE_AUTH_PREFIXES: tuple[str, ...] = ()


def pinned_tmpdir(platform: str) -> str | None:
    """Return the ``TMPDIR`` a jailed child is pinned to on *platform*.

    The single source of truth shared by the env scrub (which seeds the
    child's ``TMPDIR``) and the FS jail (which must permit writes to that
    same path): a pin the jail policy does not allow writes to is a child
    whose every temp write fails, and the two drifting apart is exactly the
    defect this resolver exists to prevent.

    Args:
        platform: The :data:`sys.platform` string to resolve for
            (``"darwin"`` / ``"linux"`` / ``"win32"`` / any other POSIX id).

    Returns:
        ``/private/tmp`` on macOS (its real temp dir; ``/tmp`` is only a
        symlink to it), ``/tmp`` on Linux and every other POSIX platform,
        and ``None`` on Windows -- which has no FS jail and reads ``TEMP`` /
        ``TMP`` rather than the POSIX ``TMPDIR``, so pinning a POSIX path
        there would hand the child a directory that cannot exist.
    """
    if platform == "win32":
        return None
    if platform == "darwin":
        return _PINNED_TMPDIR_DARWIN
    return _PINNED_TMPDIR_POSIX


def _lane_allowlist(runtime: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Return the ``(exact_keys, prefixes)`` auth allowlist for *runtime*.

    Args:
        runtime: The runtime adapter id (``"claude-code"``, ``"codex"``,
            or ``"opencode"``), or :data:`GATE_RUNTIME_LANE` for a
            non-agent gate child.

    Returns:
        A pair of the lane's exact-match auth keys and its prefix
        families. The cross-lane key is absent from both by omission, so
        the claude lane drops ``OPENAI_*`` / ``CODEX_HOME``, the codex
        lane drops ``ANTHROPIC_*`` / ``CLAUDE_*`` / ``CLAUDE_CONFIG_DIR``,
        the opencode lane drops both vendors' API-key families, and the
        gate lane drops every vendor credential.

    Raises:
        ValueError: When *runtime* is not a known auth lane.
    """
    if runtime == _CLAUDE_RUNTIME:
        return _CLAUDE_AUTH_EXACT, _CLAUDE_AUTH_PREFIXES
    if runtime == _CODEX_RUNTIME:
        return _CODEX_AUTH_EXACT, _CODEX_AUTH_PREFIXES
    if runtime == _OPENCODE_RUNTIME:
        return _OPENCODE_AUTH_EXACT, _OPENCODE_AUTH_PREFIXES
    if runtime == GATE_RUNTIME_LANE:
        return _GATE_AUTH_EXACT, _GATE_AUTH_PREFIXES
    raise ValueError(f"unknown runtime lane: {runtime!r}")


def build_child_env(
    runtime: str,
    *,
    base_env: Mapping[str, str] | None = None,
    extra_path_dir: str | None = None,
) -> dict[str, str]:
    """Build a scrubbed child environment for a spawned runtime.

    Constructs the env an ``env -i``-equivalent allowlist would: start
    from empty, seed the shared floor (``HOME``, a pinned ``PATH``,
    ``LANG`` / ``LC_*``, ``TERM``), then add only *runtime*'s own auth
    family. Every variable not on the allowlist -- ``AWS_*``, ``GH_*`` /
    ``GITHUB_*``, ``SSH_*``, ``KUBECONFIG``, ``EAWF_*`` daemon internals,
    the cross-lane credential, and any unknown variable -- is absent by
    construction, so the child can never read a credential the floor did
    not explicitly grant it.

    Args:
        runtime: The runtime adapter id selecting the auth lane. Maps
            ``"claude-code"`` -> claude lane and ``"codex"`` -> codex lane.
            :data:`GATE_RUNTIME_LANE` selects the no-auth lane a gate
            child gets: the shared floor with no vendor credential at all.
        base_env: The source environment to filter. Defaults to
            :data:`os.environ`; tests inject a fake mapping rather than
            mutating the real process environment.
        extra_path_dir: An additional directory PREPENDED to the pinned
            ``PATH`` floor. The spawn passes the resolved CLI binary's own
            directory here so the scrubbed child can still exec a tool
            installed outside the pinned floor (e.g. a Homebrew / npm-global
            prefix); the parent ``PATH`` itself is still never passed
            through. ``None`` (the default) keeps the floor pinned verbatim.

    Returns:
        A fresh ``dict`` of the scrubbed child environment. Always carries
        a pinned ``PATH`` and a ``LANG`` (defaulted to ``C.UTF-8`` when
        the base env has none), even from an empty *base_env*, plus a
        ``TMPDIR`` pinned via :func:`pinned_tmpdir` on every platform that
        has one (all but Windows).

    Raises:
        ValueError: When *runtime* is not a known auth lane.
    """
    source = os.environ if base_env is None else base_env
    auth_exact, auth_prefixes = _lane_allowlist(runtime)

    child: dict[str, str] = {}

    # Floor exact-match keys, copied only when the parent has them.
    for key in _FLOOR_EXACT_KEYS:
        value = source.get(key)
        if value is not None:
            child[key] = value

    # Floor PATH is PINNED -- never the parent PATH. An optional
    # extra_path_dir (the resolved spawn binary's own directory) is
    # PREPENDED so the scrubbed child can still exec a CLI installed outside
    # the pinned floor; the parent PATH is still never passed through.
    if extra_path_dir and extra_path_dir not in _PINNED_PATH.split(os.pathsep):
        child["PATH"] = extra_path_dir + os.pathsep + _PINNED_PATH
    else:
        child["PATH"] = _PINNED_PATH

    # Floor LANG is seeded with a default when the parent has none.
    child["LANG"] = source.get("LANG", _DEFAULT_LANG)

    # Floor TMPDIR is PINNED to a write subpath the jail policy allows on
    # THIS platform -- never the parent per-user temp -- so a sandboxed CLI's
    # temp staging stays inside the FS jail's write confinement. Windows has
    # no jail (and no POSIX TMPDIR), so it gets no pin at all.
    tmpdir = pinned_tmpdir(sys.platform)
    if tmpdir is not None:
        child["TMPDIR"] = tmpdir

    # Floor locale carry-through (LC_*) + the lane's auth families, both
    # matched by prefix.
    keep_prefixes = _FLOOR_PREFIXES + auth_prefixes
    for key, value in source.items():
        if key in child:
            continue
        if key in auth_exact or any(key.startswith(prefix) for prefix in keep_prefixes):
            child[key] = value

    logger.info(
        f"build_child_env runtime={runtime!r} kept={len(child)} "
        f"dropped={max(len(source) - len(child), 0)}"
    )
    return child


def resolve_binary_dir(binary: str) -> str | None:
    """Return the directory of *binary* resolved on the parent PATH.

    Resolved with :func:`shutil.which` against the parent ``PATH`` (before
    the env scrub pins it) so the scrubbed child -- whose ``PATH`` floor is
    minimal (``/usr/bin:/bin:/usr/sbin:/sbin``) -- can still exec a CLI
    installed outside that floor (e.g. a Homebrew ``/opt/homebrew/bin`` or
    npm-global prefix). Pass the result as :func:`build_child_env`'s
    ``extra_path_dir``. The resolution reads only the binary's location,
    never a credential, so it does not weaken the env-scrub floor.

    Without this, a sandboxed spawn of a Homebrew-installed runtime CLI
    (``codex`` / ``claude`` / ``opencode``) fails with
    ``execvp() ... No such file or directory`` because the pinned floor
    excludes the install prefix.

    The directory is the one :func:`shutil.which` found *binary* in -- NOT
    the symlink-resolved target. Homebrew often symlinks ``bin/codex`` to a
    Caskroom file with a DIFFERENT basename
    (``codex-aarch64-apple-darwin``), so ``realpath``'s directory would not
    contain a file named ``codex`` and the child's ``execvp(binary)`` would
    still fail. The PATH entry must hold a file whose name IS *binary*, which
    the ``shutil.which`` hit guarantees; the sandbox's ``(allow file-read*)``
    lets the child follow the symlink to the real target.

    Args:
        binary: The bare CLI binary name (e.g. ``"codex"``).

    Returns:
        The absolute directory holding *binary*, or ``None`` when it does
        not resolve on the parent PATH (the spawn then relies on the pinned
        floor and surfaces a clear FileNotFoundError if the binary is absent).
    """
    resolved = shutil.which(binary)
    if resolved is None:
        return None
    return os.path.dirname(os.path.abspath(resolved))


__all__ = ["GATE_RUNTIME_LANE", "build_child_env", "pinned_tmpdir", "resolve_binary_dir"]
