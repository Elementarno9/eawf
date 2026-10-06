"""Derive a wave's commit SHA from git history via its message wave markers.

The derive step is the fallback path behind the retired ``wave show`` verb:
when ``Wave.commit`` has not been pinned (via ``wave close --commit
<ref>``), the commit message is the durable signal: an ``Eawf-Wave:`` line
anywhere in the body, or the legacy ``[P##-W##]`` subject prefix. Git
history rewrites preserve messages, so the SHA stays discoverable even
after cherry-pick or rebase.

The helpers in this module are intentionally thin subprocess wrappers
that return ``None`` rather than raise when git is unavailable or the
wave has not yet been committed. Callers (renderers, validators) treat
``None`` as "SHA not yet derivable" and degrade gracefully.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Committed home for the operator-acknowledged git/state drift list. Lives at
# ``<repo_root>/.eawf/drift-acks.json`` -- deliberately OUTSIDE ``.ea/`` so the
# daemon's state-authority rule (AGENTS rule 4) is untouched: this file is
# never a lifecycle mutation, it is a reviewer-visible record of "yes, these
# historical closed-wave commit drifts are known and accepted". ``eawf doctor``
# reads it to suppress the acknowledged rows; the retired ``wave ack-drift`` verb appends to
# it. The path is relative to the repo root so it travels with the checkout.
DRIFT_ACKS_DIRNAME: str = ".eawf"
DRIFT_ACKS_FILENAME: str = "drift-acks.json"

_TIMEOUT_SECONDS: float = 5.0
_WAVE_TRAILER_NAME = "Eawf-Wave"

# Every git probe in this module passes ``stdin=subprocess.DEVNULL`` AND the
# ``detached_subprocess_kwargs()`` detach knobs. These are non-interactive
# ``capture_output=True`` reads, so a dead stdin is correct. When the drift scan
# runs inside the live TUI's Doctor-mode gather the parent fd 0 is the
# controlling TTY; a dead stdin stops the child reading it, but a child that
# merely SHARES the parent's controlling terminal can still provoke a terminal
# escape-reply (a Device-Attributes / capability response) written back onto the
# shared TTY -- which the App's stdin reader then mis-parses as synthetic
# digit-mode-switch keypresses. The detach kwargs put each child in its own
# session (POSIX) / windowless console (win32) so it has no controlling terminal
# and can neither provoke nor receive such a reply, without ever disturbing the
# App's own fd 0 (which a process-global redirect would corrupt mid-render).

# Field/record separators for the one-pass ``git log`` walks. A commit message
# can carry newlines but never a NUL byte, so NUL is a safe record boundary even
# though the full-body placeholder spans lines; the unit separator delimits
# fields within a record. The body is always the last field and is split off
# with a bounded ``maxsplit``, so a stray separator byte inside a message stays
# in the body instead of shifting it out of reach.
# These are the literal output bytes the parser splits on; the ``--format``
# string itself uses git's ``%x00`` / ``%x1f`` placeholders (ASCII in argv)
# because subprocess rejects a literal NUL byte inside a command argument.
_REC_SEP = "\x00"
_FIELD_SEP = "\x1f"
_REC_SEP_PLACEHOLDER = "%x00"
_FIELD_SEP_PLACEHOLDER = "%x1f"
_BODY_PLACEHOLDER = "%B"

# A bracketed commit-subject prefix, e.g. ``[P30-I07-W08]`` or ``[P28-W02]``.
_BRACKET_PREFIX_RE = re.compile(r"^\[(P\d{2,}(?:-I\d{2,})?-W\d{2,})\]")

# The ``Eawf-Wave`` line exactly as ``tools/commit_prefix_lint.py`` accepts it:
# line-anchored anywhere in the message, not only in git's final trailer
# paragraph. Git's ``%(trailers)`` parse misses a commit that puts a blank line
# between ``Eawf-Wave:`` and ``Co-Authored-By:``, which the lint accepts, so the
# index would lose that wave. The lint runs under the system interpreter and
# cannot be imported from here, so the pattern is copied and a parity test pins
# the two together.
_WAVE_TRAILER_RE = re.compile(
    rf"^{_WAVE_TRAILER_NAME}:\s+"
    r"(?P<wave>P\d{2,}(?:-I(?!00)\d{2,})?-W(?!00)\d{2,})\s*$",
    re.MULTILINE,
)

# Transient per-wave refs whose commit must lose to an integration ref under a
# twin. ``worktree-agent-*`` is the Claude harness's detached worktree branch
# name; ``refs/worktrees/`` is git's own per-worktree pseudo-ref namespace; the
# ``-pNN-wMM`` suffix is the project's per-wave worktree branch convention
# (``feature/<symbol>-v<X.Y>-pNN-wMM``). Anything else (HEAD, the long-running
# feature branch, ``main``, ``origin/*``) counts as an integration ref.
_TRANSIENT_REF_RE = re.compile(
    r"(?:^|/)worktree-agent-|/worktrees/|-p\d{2,}-w\d{2,}$",
    re.IGNORECASE,
)

# Integration commits win over transient ones; within a tier, most-recent wins.
_TIER_INTEGRATION = 0
_TIER_TRANSIENT = 1


def drift_acks_path(repo_root: Path | None = None) -> Path:
    """Return the ``<repo_root>/.eawf/drift-acks.json`` ack-file path.

    Args:
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        The absolute path to the committed drift-ack file (which may not yet
        exist on disk).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    return root / DRIFT_ACKS_DIRNAME / DRIFT_ACKS_FILENAME


def load_drift_acks(repo_root: Path | None = None) -> set[str]:
    """Load the set of acknowledged drift ``wave_id`` values.

    Reads ``<repo_root>/.eawf/drift-acks.json``. The on-disk shape is::

        {"acked_wave_ids": ["P22-I01-W05", "P27-I05-W06", ...]}

    A missing file, unreadable bytes, or a malformed payload all degrade to
    an empty set so a corrupt ack file never crashes ``eawf doctor`` -- the
    worst case is the acknowledged rows re-surface as warnings.

    Args:
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        The set of acknowledged wave ids (possibly empty).
    """
    path = drift_acks_path(repo_root)
    if not path.exists():
        return set()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"load_drift_acks path={path} status=unreadable err={exc!s}")
        return set()
    raw = body.get("acked_wave_ids") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        logger.warning(f"load_drift_acks path={path} status=malformed")
        return set()
    return {str(w) for w in raw if isinstance(w, str)}


def _phase_and_wave(wave_id: str) -> tuple[str, str] | None:
    """Split ``P##-I##-W##`` into ``("P##", "W##")``; return ``None`` on mismatch."""
    parts = wave_id.split("-")
    if len(parts) != 3:
        return None
    phase, _iter, wave = parts
    if not (phase.startswith("P") and wave.startswith("W")):
        return None
    return phase, wave


def commit_prefix(wave_id: str) -> str | None:
    """Return the canonical bracketed commit subject prefix for *wave_id*.

    I01 waves use the short form ``[P##-W##]``; non-I01 waves use the
    long form ``[P##-I##-W##]`` so the iter is disambiguated. ``None``
    if *wave_id* is malformed.

    ``P19-I01-W04`` -> ``"[P19-W04]"``.
    ``P19-I02-W01`` -> ``"[P19-I02-W01]"``.
    """
    pair = _phase_and_wave(wave_id)
    if pair is None:
        return None
    phase, wave = pair
    parts = wave_id.split("-")
    iter_token = parts[1]
    if iter_token == "I01":
        return f"[{phase}-{wave}]"
    return f"[{phase}-{iter_token}-{wave}]"


def commit_wave_trailer(wave_id: str) -> str | None:
    """Return the trailer-mode wave marker for *wave_id*.

    ``P19-I02-W01`` -> ``"Eawf-Wave: P19-I02-W01"``.
    """
    if _phase_and_wave(wave_id) is None:
        return None
    return f"{_WAVE_TRAILER_NAME}: {wave_id}"


def _candidate_prefixes(wave_id: str) -> list[str]:
    """Return the prefix forms to grep for, in priority order.

    Executors sometimes emit the long form ``[P##-I01-W##]`` for I01
    waves even though the canonical short form drops the iter token,
    and the reverse happens too. Try the canonical form first, then
    fall back to the other shape so cherry-picked commits are
    discoverable regardless of which executor wrote them.
    """
    pair = _phase_and_wave(wave_id)
    if pair is None:
        return []
    phase, wave = pair
    parts = wave_id.split("-")
    iter_token = parts[1]
    canonical = commit_prefix(wave_id)
    assert canonical is not None  # _phase_and_wave already validated shape
    if iter_token == "I01":
        return [canonical, f"[{phase}-{iter_token}-{wave}]"]
    return [canonical, f"[{phase}-{wave}]"]


def _candidate_grep_terms(wave_id: str) -> list[str]:
    terms = _candidate_prefixes(wave_id)
    trailer = commit_wave_trailer(wave_id)
    if trailer is not None:
        terms.append(trailer)
    return terms


def _body_wave_ids(message: str) -> list[str]:
    """Return every wave id named on an ``Eawf-Wave`` line of *message*.

    A short ``P##-W##`` value names the ``I01`` iter, as it does for the
    commit lint. A squash commit can name several waves, so every line counts;
    the first id is the one the lint scopes the commit to.

    Args:
        message: The full commit message, subject included.

    Returns:
        The distinct full wave ids in message order; empty when no line
        matches.
    """
    wave_ids: list[str] = []
    for match in _WAVE_TRAILER_RE.finditer(message):
        parts = match.group("wave").split("-")
        wave_id = "-".join(parts) if len(parts) == 3 else f"{parts[0]}-I01-{parts[1]}"
        if wave_id not in wave_ids:
            wave_ids.append(wave_id)
    return wave_ids


def _commit_wave_keys(subject: str, body: str) -> list[str]:
    """Return a commit's index keys: its bracket prefix, then its body wave ids."""
    keys: list[str] = []
    prefix_match = _BRACKET_PREFIX_RE.match(subject)
    if prefix_match:
        keys.append(f"[{prefix_match.group(1)}]")
    keys.extend(_body_wave_ids(body))
    return keys


def _candidate_index_keys(wave_id: str) -> list[str]:
    """Return the index-lookup keys for *wave_id*, in priority order.

    Mirrors :func:`_candidate_grep_terms` but yields the bare keys the
    index is built on: the bracketed prefix forms (canonical first, then
    its long/short alternate) followed by the bare wave id (the
    ``Eawf-Wave`` trailer value). The canonical-then-alt prefix order
    preserves the same most-recent-wins semantics the per-wave
    ``git log --grep`` path used.
    """
    keys = _candidate_prefixes(wave_id)
    if _phase_and_wave(wave_id) is not None:
        keys.append(wave_id)
    return keys


def _run_git(
    args: list[str], *, repo_root: Path | None, timeout: float = _TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    """Run one detached read-only git command and return ``None`` on probe failure."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None


def _normalise_commit_message(message: str) -> str:
    return "\n".join(line.rstrip() for line in message.strip().splitlines())


def commit_identity_digest(commit: str, *, repo_root: Path | None = None) -> str | None:
    """Hash normalized full message plus semantic patch for rebase-safe identity."""
    message = _run_git(["show", "-s", "--format=%B", commit], repo_root=repo_root)
    patch = _run_git(
        ["show", "--format=", "--no-ext-diff", "--no-renames", "--unified=0", commit],
        repo_root=repo_root,
    )
    if message is None or patch is None or message.returncode != 0 or patch.returncode != 0:
        return None
    semantic_lines = [
        line.rstrip() for line in patch.stdout.splitlines() if not line.startswith("index ")
    ]
    payload = (
        _normalise_commit_message(message.stdout) + "\n\x00\n" + "\n".join(semantic_lines).strip()
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def build_wave_sha_index(repo_root: Path | None = None) -> Mapping[str, str]:
    """Build the wave-key -> commit-SHA map in ONE ``git log`` pass.

    Replaces the O(closed-waves) per-wave ``git log --grep`` shell-outs in
    the bulk reconcilers with a single ``git log --all --source`` walk that
    indexes every commit's bracketed subject prefix AND every ``Eawf-Wave``
    line in its full message, read the way the commit lint reads it (see
    :func:`_body_wave_ids`). Consumers (:func:`derive_wave_sha` and the
    release repin) look a wave's candidate keys up in the returned map
    instead of shelling out per wave.

    Keys are the bracketed prefix (e.g. ``[P30-I07-W08]``, ``[P28-W02]``)
    and the full wave id (the ``Eawf-Wave`` value, e.g. ``P28-I03-W02``;
    a short ``P28-W02`` value is keyed as ``P28-I01-W02``). Values
    are the full 40-hex SHA.

    Deterministic twin resolution: ``--source`` annotates each commit with
    the ref it was reached through. When the same key appears on both an
    integration ref (HEAD, the long-running feature branch, ``main``,
    ``origin/*``) and a transient per-wave ref (``worktree-agent-*``,
    ``refs/worktrees/*``, a ``-pNN-wMM`` worktree branch), the integration
    SHA wins. Within one tier the most-recent commit wins (``git log`` emits
    newest-first, so the first sighting per tier is kept).

    Args:
        repo_root: Repository working directory; defaults to the process
            cwd.

    Returns:
        A mapping of wave key to 40-hex SHA. Empty when git is unavailable,
        the log call fails or times out, or history carries no wave keys.
    """
    if shutil.which("git") is None:
        logger.debug("build_wave_sha_index git=not-on-path")
        return {}
    fmt = _REC_SEP_PLACEHOLDER + _FIELD_SEP_PLACEHOLDER.join(("%H", "%S", "%s", _BODY_PLACEHOLDER))
    cmd = ["git", "log", "--all", "--source", f"--format={fmt}"]
    try:
        # Annotate ``out`` explicitly: splatting the ``dict[str, Any]`` detach
        # kwargs defeats mypy's ``subprocess.run`` overload narrowing, so the
        # ``text=True`` -> ``CompletedProcess[str]`` inference is restored here.
        out: subprocess.CompletedProcess[str] = subprocess.run(
            cmd,
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        logger.debug("build_wave_sha_index status=timeout")
        return {}
    except (FileNotFoundError, OSError) as exc:
        logger.debug(f"build_wave_sha_index status=os-error err={exc!s}")
        return {}
    if out.returncode != 0:
        logger.debug(
            f"build_wave_sha_index status=non-zero rc={out.returncode} "
            f"stderr={out.stderr.strip()!r}"
        )
        return {}
    return _parse_index(out.stdout)


def _parse_index(raw: str) -> dict[str, str]:
    """Parse ``git log --source`` output into the wave-key -> SHA map.

    Tier per key tracks whether the winning SHA came from an integration
    ref so a later transient sighting cannot overwrite it; within a tier the
    first (newest) sighting is kept.
    """
    index: dict[str, str] = {}
    tier_seen: dict[str, int] = {}
    for record in raw.split(_REC_SEP):
        if not record:
            continue
        fields = record.split(_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha, source_ref, subject, body = fields
        sha = sha.strip()
        if not sha:
            continue
        tier = _TIER_TRANSIENT if _TRANSIENT_REF_RE.search(source_ref) else _TIER_INTEGRATION
        for key in _commit_wave_keys(subject, body):
            prior = tier_seen.get(key)
            if prior is None or tier < prior:
                index[key] = sha
                tier_seen[key] = tier
    return index


def _sha_from_index(wave_id: str, index: Mapping[str, str]) -> str | None:
    """Resolve *wave_id* against a prebuilt *index*, candidate-key priority order."""
    for key in _candidate_index_keys(wave_id):
        sha = index.get(key)
        if sha is not None:
            return sha
    return None


def _git_merge_base_head_main(
    *, repo_root: Path | None = None, fallback: str = "origin/main"
) -> str:
    """Return ``git merge-base HEAD main`` or ``fallback`` on failure.

    Used as the diff-base of last resort when a wave-anchored SHA is
    unavailable (no ``wave_id`` was threaded into the gate, or the
    wave has not yet committed). The merge-base is preferred over the
    raw ``main`` ref because it scopes the diff to "commits unique to
    this branch", which is what every other ``changed_files`` caller
    already expects.

    Args:
        repo_root: Repository working directory; defaults to the process
            cwd.
        fallback: String returned when git is missing, ``main`` is not
            reachable, or the call times out. Defaults to ``origin/main``
            so callers can still feed it to ``git diff <base>...HEAD``
            via ``changed_files``.

    Returns:
        The 40-char merge-base SHA on success; ``fallback`` otherwise.
    """
    if shutil.which("git") is None:
        logger.debug("_git_merge_base_head_main git=not-on-path")
        return fallback
    try:
        # Annotate explicitly so the splatted detach kwargs do not collapse the
        # ``subprocess.run`` overload to its ``Any``-returning fallback (which
        # would make ``out.stdout`` Any).
        out: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "merge-base", "HEAD", "main"],
            cwd=str(repo_root) if repo_root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"_git_merge_base_head_main status=failed err={exc!s}")
        return fallback
    if out.returncode != 0:
        logger.debug(
            f"_git_merge_base_head_main status=non-zero rc={out.returncode} "
            f"stderr={out.stderr.strip()!r}"
        )
        return fallback
    sha = out.stdout.strip()
    if not sha:
        return fallback
    return sha


def derive_diff_base(
    wave_id: str | None,
    *,
    repo_root: Path | None = None,
    fallback: str = "origin/main",
) -> str:
    """Return a diff-base ref suitable for ``git diff <base>...HEAD``.

    Threading order matches the W15 audit-DSL runner contract:

    1. When *wave_id* resolves via :func:`derive_wave_sha`, return
       ``f"{sha}~1"`` so the diff scopes to the wave's own delta.
    2. Otherwise, fall back to ``git merge-base HEAD main`` (per
       :func:`_git_merge_base_head_main`).
    3. If even the merge-base lookup fails, return *fallback* — keeps
       the call site fail-open (matches :data:`~eawf.platform.lint.
       _conditional.DEFAULT_DIFF_BASE`).

    The fallback chain matters because audit gates run in environments
    that range from a fully-fledged repo (with the wave already
    committed) to a fresh clone in CI (where ``derive_wave_sha`` legitimately
    returns ``None``).
    """
    if wave_id is not None:
        sha = derive_wave_sha(wave_id, repo_root=repo_root)
        if sha is not None:
            return f"{sha}~1"
    return _git_merge_base_head_main(repo_root=repo_root, fallback=fallback)


def derive_wave_sha(
    wave_id: str,
    *,
    repo_root: Path | None = None,
    index: Mapping[str, str] | None = None,
) -> str | None:
    """Return the most recent commit SHA whose subject carries the wave's prefix.

    Two resolution paths share the canonical-then-alt prefix form plus the
    ``Eawf-Wave`` trailer fallback:

    - When *index* is supplied (the bulk-reconciler fast path), the SHA is
      looked up from the prebuilt :func:`build_wave_sha_index` map. The
      index already encodes integration-wins twin resolution and
      most-recent-wins ordering, so this path is a pure dict lookup with no
      shell-out -- the whole-state walk costs one ``git log`` instead of one
      per closed wave.
    - When *index* is ``None`` (single-lookup callers such as
      ``wave show --commit`` and the renderers), the lookup walks
      ``git log --all --grep=<prefix> --format=%H -n 1`` per candidate so
      the call stays branch-agnostic and survives cherry-picks.

    Returns ``None`` when:

    - git is not installed,
    - the prefix cannot be derived from *wave_id*,
    - no commit matches any candidate prefix.

    Logs a debug line on failure rather than raising -- renderers should
    degrade to an empty SHA, not crash.
    """
    if index is not None:
        return _sha_from_index(wave_id, index)
    candidates = _candidate_grep_terms(wave_id)
    if not candidates:
        return None
    if shutil.which("git") is None:
        logger.debug(f"derive_wave_sha wave={wave_id} git=not-on-path")
        return None
    for prefix in candidates:
        cmd = [
            "git",
            "log",
            "--all",
            f"--grep={prefix}",
            "-F",
            "--format=%H",
            "-n",
            "1",
        ]
        try:
            # Annotate explicitly so the splatted detach kwargs do not collapse
            # the ``subprocess.run`` overload to its ``Any``-returning fallback.
            out: subprocess.CompletedProcess[str] = subprocess.run(
                cmd,
                cwd=str(repo_root) if repo_root else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                **detached_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            logger.debug(f"derive_wave_sha wave={wave_id} status=timeout prefix={prefix!r}")
            return None
        except (FileNotFoundError, OSError) as exc:
            logger.debug(
                f"derive_wave_sha wave={wave_id} status=os-error prefix={prefix!r} err={exc!s}"
            )
            return None
        if out.returncode != 0:
            rc = out.returncode
            logger.debug(
                f"derive_wave_sha wave={wave_id} status=non-zero rc={rc} prefix={prefix!r}"
            )
            continue
        sha = out.stdout.strip().splitlines()
        if sha:
            return sha[0]
    return None
