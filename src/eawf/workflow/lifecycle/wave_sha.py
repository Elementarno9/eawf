"""Derive a wave's commit SHA from git history via the ``[P##-W##]`` prefix.

The derive step is the fallback path behind ``eawf wave show --commit``:
when ``Wave.commit`` has not been pinned (via ``wave close --commit
<ref>``), the commit subject's ``[P##-W##]`` prefix is the durable
signal. Git history rewrites preserve subjects, so the SHA stays
discoverable even after cherry-pick or rebase.

The helpers in this module are intentionally thin subprocess wrappers
that return ``None`` rather than raise when git is unavailable or the
wave has not yet been committed. Callers (renderers, validators) treat
``None`` as "SHA not yet derivable" and degrade gracefully.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from eawf.platform.subprocess_detach import detached_subprocess_kwargs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

# Committed home for the operator-acknowledged git/state drift list. Lives at
# ``<repo_root>/.eawf/drift-acks.json`` -- deliberately OUTSIDE ``.ea/`` so the
# daemon's state-authority rule (AGENTS rule 4) is untouched: this file is
# never a lifecycle mutation, it is a reviewer-visible record of "yes, these
# historical closed-wave commit drifts are known and accepted". ``eawf doctor``
# reads it to suppress the acknowledged rows; ``eawf wave ack-drift`` appends to
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

# Field/record separators for the one-pass ``git log`` in
# :func:`build_wave_sha_index`. A commit message can carry newlines but never a
# NUL byte, so NUL is a safe record boundary even when the trailer placeholder
# emits its own newline; the unit separator delimits fields within a record.
# These are the literal output bytes the parser splits on; the ``--format``
# string itself uses git's ``%x00`` / ``%x1f`` placeholders (ASCII in argv)
# because subprocess rejects a literal NUL byte inside a command argument.
_REC_SEP = "\x00"
_FIELD_SEP = "\x1f"
_REC_SEP_PLACEHOLDER = "%x00"
_FIELD_SEP_PLACEHOLDER = "%x1f"

# A bracketed commit-subject prefix, e.g. ``[P30-I07-W08]`` or ``[P28-W02]``.
_BRACKET_PREFIX_RE = re.compile(r"^\[(P\d{2,}(?:-I\d{2,})?-W\d{2,})\]")

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


DriftKind = Literal[
    "pinned_but_missing",
    "pinned_mismatch",
    "ambiguous_successor",
    "closed_no_pin",
    "closed_unfindable",
]


@dataclass(frozen=True)
class Drift:
    """One git/state mismatch row surfaced by :func:`detect_git_state_drift`.

    Attributes:
        wave_id: The wave whose state-recorded commit pointer disagrees
            with what ``git log --grep`` produces.
        kind: Which mismatch shape we hit:

            - ``pinned_but_missing`` — state has ``Wave.commit`` set, but
              ``git log --grep`` returns no commit (commit not on any
              reachable ref; suggests a force-push or repo-clean).
            - ``pinned_mismatch`` — state and git both produce a SHA,
              but they disagree (suggests a rebase that rewrote the
              wave commit without ``eawf wave close --commit <ref>``).
            - ``closed_no_pin`` — CLOSED wave with no ``Wave.commit``
              and no derivable SHA from git history (no commit subject
              carries the wave's bracketed prefix).
            - ``closed_unfindable`` — same as ``closed_no_pin`` but the
              git binary itself was unavailable; the drift is
              indeterminate and the operator should re-run with a
              working git on PATH before acting.
        state_commit: The SHA recorded in ``Wave.commit`` (``None`` for
            the ``closed_no_pin`` / ``closed_unfindable`` kinds).
        git_commit: The SHA ``git log --grep`` returned (``None`` for
            the ``pinned_but_missing`` / ``closed_no_pin`` /
            ``closed_unfindable`` kinds).
    """

    wave_id: str
    kind: DriftKind
    state_commit: str | None = None
    git_commit: str | None = None


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


def save_drift_acks(acked_wave_ids: set[str], repo_root: Path | None = None) -> Path:
    """Persist *acked_wave_ids* to ``<repo_root>/.eawf/drift-acks.json``.

    Writes a deterministic, sorted payload (so re-saving the same set is a
    byte-stable no-op and the committed file stays diff-clean). The write is
    atomic: a sibling tempfile is ``os.replace``\\d onto the target.

    Args:
        acked_wave_ids: The full ack set to persist (the caller unions any
            new acks into the existing set first).
        repo_root: Repository working directory; defaults to the process cwd.

    Returns:
        The path the ack file was written to.
    """
    path = drift_acks_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"acked_wave_ids": sorted(acked_wave_ids)},
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(f"save_drift_acks path={path} count={len(acked_wave_ids)}")
    return path


def _resolve_acked_wave_ids(*, repo_root: Path | None, acked_wave_ids: set[str] | None) -> set[str]:
    """Return the explicit ack set or load the repo ack file when available."""
    if acked_wave_ids is not None:
        return acked_wave_ids
    if repo_root is not None:
        return load_drift_acks(repo_root)
    return set()


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
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None


def _normalise_commit_message(message: str) -> str:
    return "\n".join(line.rstrip() for line in message.strip().splitlines())


def commit_matches_wave(commit: str, wave_id: str, *, repo_root: Path | None = None) -> bool:
    """Return whether *commit* explicitly identifies *wave_id* by prefix or trailer."""
    out = _run_git(["show", "-s", "--format=%B", commit], repo_root=repo_root)
    if out is None or out.returncode != 0:
        return False
    message = out.stdout or ""
    subject = message.splitlines()[0] if message.splitlines() else ""
    trailer = commit_wave_trailer(wave_id)
    return any(subject.startswith(prefix) for prefix in _candidate_prefixes(wave_id)) or (
        trailer is not None and trailer in message
    )


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


def _reachable_wave_keys(repo_root: Path | None) -> dict[str, set[str]]:
    """Return every reachable commit and its explicit wave identity keys."""
    fmt = _REC_SEP_PLACEHOLDER + _FIELD_SEP_PLACEHOLDER.join(
        ("%H", "%s", f"%(trailers:key={_WAVE_TRAILER_NAME},valueonly)")
    )
    out = _run_git(["log", "--all", f"--format={fmt}"], repo_root=repo_root, timeout=20.0)
    if out is None or out.returncode != 0:
        return {}
    result: dict[str, set[str]] = {}
    for raw_record in out.stdout.split(_REC_SEP):
        fields = raw_record.strip("\n").split(_FIELD_SEP)
        if len(fields) != 3:
            continue
        sha, subject, trailers = fields
        keys: set[str] = set()
        prefix_match = _BRACKET_PREFIX_RE.match(subject)
        if prefix_match:
            keys.add(f"[{prefix_match.group(1)}]")
        keys.update(line.strip() for line in trailers.splitlines() if line.strip())
        result[sha] = keys
    return result


def _first_parent_wave_candidates(repo_root: Path | None) -> dict[str, list[str]]:
    """Index all wave identities on the current integration first-parent."""
    fmt = _REC_SEP_PLACEHOLDER + _FIELD_SEP_PLACEHOLDER.join(
        ("%H", "%s", f"%(trailers:key={_WAVE_TRAILER_NAME},valueonly)")
    )
    out = _run_git(["log", "--first-parent", "HEAD", f"--format={fmt}"], repo_root=repo_root)
    if out is None or out.returncode != 0:
        return {}
    indexed: dict[str, list[str]] = {}
    for raw_record in out.stdout.split(_REC_SEP):
        if not raw_record:
            continue
        fields = raw_record.strip("\n").split(_FIELD_SEP)
        if len(fields) != 3:
            continue
        sha, subject, trailers = fields
        keys: list[str] = []
        prefix_match = _BRACKET_PREFIX_RE.match(subject)
        if prefix_match:
            keys.append(f"[{prefix_match.group(1)}]")
        keys.extend(line.strip() for line in trailers.splitlines() if line.strip())
        for key in keys:
            indexed.setdefault(key, []).append(sha)
    return indexed


def _candidate_shas(wave_id: str, candidates: Mapping[str, list[str]]) -> list[str]:
    """Return unique first-parent candidates for *wave_id*, newest first."""
    found: list[str] = []
    for key in _candidate_index_keys(wave_id):
        for sha in candidates.get(key, []):
            if sha not in found:
                found.append(sha)
    return found


def _subject_tokens(subject: str) -> set[str]:
    """Return meaningful lowercase tokens from a wave title or commit subject."""
    body = _BRACKET_PREFIX_RE.sub("", subject, count=1)
    body = re.sub(r"^\s*[a-z]+(?:\([^)]+\))?!?:\s*", "", body)
    return {token for token in re.findall(r"[a-z0-9]+", body.casefold()) if len(token) > 1}


def _select_legacy_candidate(
    *,
    wave_title: str,
    candidate_shas: list[str],
    repo_root: Path | None,
    identity: Callable[[str], str | None],
    subject_cache: dict[str, str | None],
) -> str | None:
    """Select one unique first-parent commit that matches the wave title."""
    title_tokens = _subject_tokens(wave_title)
    if not title_tokens:
        return None
    groups: dict[str, list[str]] = {}
    scores: dict[str, int] = {}
    for sha in candidate_shas:
        digest = identity(sha)
        if digest is None:
            continue
        if sha not in subject_cache:
            result = _run_git(["show", "-s", "--format=%s", sha], repo_root=repo_root)
            subject_cache[sha] = (
                result.stdout.strip() if result is not None and result.returncode == 0 else None
            )
        subject = subject_cache[sha]
        if subject is None:
            continue
        groups.setdefault(digest, []).append(sha)
        scores[digest] = max(
            scores.get(digest, 0),
            len(title_tokens & _subject_tokens(subject)),
        )
    if not scores:
        return None
    best = max(scores.values())
    winners = [digest for digest, score in scores.items() if score == best and score > 0]
    if len(winners) != 1:
        return None
    winner_group = groups[winners[0]]
    return winner_group[0] if len(winner_group) == 1 else None


def build_wave_sha_index(repo_root: Path | None = None) -> Mapping[str, str]:
    """Build the wave-key -> commit-SHA map in ONE ``git log`` pass.

    Replaces the O(closed-waves) per-wave ``git log --grep`` shell-outs in
    the bulk reconcilers with a single ``git log --all --source`` walk that
    indexes every commit's bracketed subject prefix AND its ``Eawf-Wave``
    trailer value. Consumers (:func:`derive_wave_sha`,
    :func:`detect_git_state_drift`, :func:`scan_commit_pins`) look a wave's
    candidate keys up in the returned map instead of shelling out per wave.

    Keys are the bracketed prefix (e.g. ``[P30-I07-W08]``, ``[P28-W02]``)
    and the bare wave id (the trailer value, e.g. ``P28-I03-W02``). Values
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
    fmt = _REC_SEP_PLACEHOLDER + _FIELD_SEP_PLACEHOLDER.join(
        ("%H", "%S", "%s", f"%(trailers:key={_WAVE_TRAILER_NAME},valueonly)")
    )
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
        fields = record.split(_FIELD_SEP)
        if len(fields) < 4:
            continue
        sha, source_ref, subject, trailer = fields[0], fields[1], fields[2], fields[3]
        sha = sha.strip()
        if not sha:
            continue
        tier = _TIER_TRANSIENT if _TRANSIENT_REF_RE.search(source_ref) else _TIER_INTEGRATION
        keys: list[str] = []
        match = _BRACKET_PREFIX_RE.match(subject)
        if match is not None:
            keys.append(f"[{match.group(1)}]")
        trailer_wave = trailer.strip().splitlines()
        if trailer_wave:
            keys.append(trailer_wave[0].strip())
        for key in keys:
            if not key:
                continue
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


PinClassification = tuple[DriftKind | Literal["unpinned_derivable"], str | None, bool] | None


def _classify_unpinned(candidate_shas: list[str]) -> PinClassification:
    """Classify one closed wave with no stored commit."""
    if len(candidate_shas) == 1:
        return "unpinned_derivable", candidate_shas[0], True
    if len(candidate_shas) > 1:
        return "ambiguous_successor", None, False
    return "closed_no_pin", None, False


def _matching_identity_candidates(
    *,
    candidate_shas: list[str],
    commit_digest: str | None,
    identity: Callable[[str], str | None],
) -> list[str]:
    """Return first-parent candidates with the stored commit's identity."""
    if commit_digest is None:
        return []
    return [sha for sha in candidate_shas if identity(sha) == commit_digest]


def _select_repair_target(
    *,
    matching_shas: list[str],
    candidate_shas: list[str],
    wave_title: str,
    repo_root: Path | None,
    identity: Callable[[str], str | None],
    subject_cache: dict[str, str | None],
) -> str | None:
    """Select a semantic target, then a unique legacy title match."""
    if len(matching_shas) == 1:
        return matching_shas[0]
    if len(matching_shas) > 1:
        return None
    if len(candidate_shas) == 1:
        return candidate_shas[0]
    return _select_legacy_candidate(
        wave_title=wave_title,
        candidate_shas=candidate_shas,
        repo_root=repo_root,
        identity=identity,
        subject_cache=subject_cache,
    )


def _reachable_pin_is_clean(
    *,
    reachable_pin: str,
    candidate_shas: list[str],
    wave_id: str,
    reachable_wave_keys: Mapping[str, set[str]],
) -> bool:
    """Return whether a reachable pin explicitly belongs to the wave."""
    if any(_shas_match(reachable_pin, candidate) for candidate in candidate_shas):
        return True
    expected_keys = set(_candidate_index_keys(wave_id))
    return bool(reachable_wave_keys.get(reachable_pin, set()) & expected_keys)


def _classify_unreachable_pin(
    *,
    matching_shas: list[str],
    candidate_shas: list[str],
    commit_digest: str | None,
    wave_title: str,
    repo_root: Path | None,
    identity: Callable[[str], str | None],
    subject_cache: dict[str, str | None],
) -> PinClassification:
    """Classify an unreachable commit object without losing legacy misbindings."""
    if len(matching_shas) == 1:
        return "pinned_mismatch", matching_shas[0], True
    if len(matching_shas) > 1:
        return "ambiguous_successor", None, False
    if commit_digest is None or not candidate_shas:
        return "pinned_but_missing", None, False
    selected = _select_repair_target(
        matching_shas=[],
        candidate_shas=candidate_shas,
        wave_title=wave_title,
        repo_root=repo_root,
        identity=identity,
        subject_cache=subject_cache,
    )
    if selected is not None:
        return "pinned_mismatch", selected, True
    return "ambiguous_successor", None, False


def _classify_wave_pin(
    *,
    wave_id: str,
    wave_title: str,
    pinned: str | None,
    stored_digest: str | None,
    reachable_shas: set[str],
    reachable_wave_keys: Mapping[str, set[str]],
    candidates: Mapping[str, list[str]],
    repo_root: Path | None,
    identity_cache: dict[str, str | None],
    subject_cache: dict[str, str | None],
) -> PinClassification:
    """Classify one wave against identity-aware integration history."""

    def identity(sha: str) -> str | None:
        if sha not in identity_cache:
            identity_cache[sha] = commit_identity_digest(sha, repo_root=repo_root)
        return identity_cache[sha]

    candidate_shas = _candidate_shas(wave_id, candidates)
    if pinned is None:
        return _classify_unpinned(candidate_shas)

    reachable_pin = next((sha for sha in reachable_shas if _shas_match(pinned, sha)), None)
    if (
        reachable_pin is not None
        and _reachable_pin_is_clean(
            reachable_pin=reachable_pin,
            candidate_shas=candidate_shas,
            wave_id=wave_id,
            reachable_wave_keys=reachable_wave_keys,
        )
        and (stored_digest is None or identity(reachable_pin) == stored_digest)
    ):
        return None

    commit_digest = stored_digest or identity(pinned)
    matching = _matching_identity_candidates(
        candidate_shas=candidate_shas,
        commit_digest=commit_digest,
        identity=identity,
    )
    if reachable_pin is None:
        return _classify_unreachable_pin(
            matching_shas=matching,
            candidate_shas=candidate_shas,
            commit_digest=commit_digest,
            wave_title=wave_title,
            repo_root=repo_root,
            identity=identity,
            subject_cache=subject_cache,
        )

    # A reachable pin with the wrong wave identity is a legacy misbinding.
    # Repair only when the integration first-parent has one unambiguous target.
    if candidate_shas:
        selected = _select_repair_target(
            matching_shas=matching,
            candidate_shas=candidate_shas,
            wave_title=wave_title,
            repo_root=repo_root,
            identity=identity,
            subject_cache=subject_cache,
        )
        if selected is not None:
            return "pinned_mismatch", selected, True
        return "ambiguous_successor", None, False
    return "pinned_but_missing", None, False


def detect_git_state_drift(
    state: State,
    *,
    repo_root: Path | None = None,
    acked_wave_ids: set[str] | None = None,
) -> list[Drift]:
    """Compare every CLOSED wave's recorded commit against git history.

    For each ``WaveStatus.CLOSED`` row, four mismatch shapes get
    surfaced (see :class:`DriftKind`):

    1. ``Wave.commit`` is set AND ``derive_wave_sha`` returns ``None``
       — the pinned commit is no longer reachable from any ref.
    2. ``Wave.commit`` is set AND ``derive_wave_sha`` returns a
       different SHA — the wave was rebased after pinning.
    3. ``Wave.commit`` is ``None`` AND ``derive_wave_sha`` returns
       ``None`` because git has no matching subject — the wave closed
       without recording its commit and git no longer carries the
       bracketed prefix.
    4. ``Wave.commit`` is ``None`` AND git is unavailable on PATH —
       we cannot decide; surfaced as ``closed_unfindable`` so the
       operator knows the gap is indeterminate.

    Waves whose status is not CLOSED are ignored: only closed waves
    have a stable expectation about which commit anchors them.

    Acknowledged historical drifts (squashed / cherry-pick-twin / lost
    commits that the operator has reviewed and accepted via
    ``eawf wave ack-drift``) are filtered out. When *acked_wave_ids* is
    supplied, that set is the explicit filter. When it is ``None`` and
    *repo_root* is supplied, the filter is loaded from
    ``<repo_root>/.eawf/drift-acks.json`` so read-only projections such
    as ``eawf status`` and the TUI doctor share the same ack path as
    ``eawf doctor``. With no *repo_root*, no implicit ack file is read;
    unit callers that do not model a repo stay hermetic.

    Args:
        state: The validated :class:`State` to walk.
        repo_root: Repository working directory; defaults to the
            process cwd via the subprocess machinery in
            :func:`derive_wave_sha`.
        acked_wave_ids: Wave ids whose drift the operator has already
            acknowledged; ``None`` means load the repo ack file when
            *repo_root* is supplied, otherwise use no acks.

    Returns:
        List of :class:`Drift` rows, ordered by ``wave_id`` so render
        output stays stable across runs. Empty list when every closed
        wave reconciles cleanly (or every drift is acknowledged).
    """
    from eawf.kernel.state.enums import WaveStatus

    acked = _resolve_acked_wave_ids(repo_root=repo_root, acked_wave_ids=acked_wave_ids)
    git_available = shutil.which("git") is not None
    reachable_wave_keys = _reachable_wave_keys(repo_root) if git_available else {}
    reachable_shas = set(reachable_wave_keys)
    candidates = _first_parent_wave_candidates(repo_root) if git_available else {}
    identity_cache: dict[str, str | None] = {}
    subject_cache: dict[str, str | None] = {}

    drifts: list[Drift] = []
    for wave_id in sorted(state.waves):
        wave = state.waves[wave_id]
        if wave.status != WaveStatus.CLOSED:
            continue
        if wave_id in acked:
            continue
        pinned = wave.commit
        if not git_available:
            missing_kind: DriftKind = (
                "closed_unfindable" if pinned is None else "pinned_but_missing"
            )
            drifts.append(Drift(wave_id=wave_id, kind=missing_kind, state_commit=pinned))
            continue
        classified = _classify_wave_pin(
            wave_id=wave_id,
            wave_title=wave.title,
            pinned=pinned,
            stored_digest=wave.commit_identity_digest,
            reachable_shas=reachable_shas,
            reachable_wave_keys=reachable_wave_keys,
            candidates=candidates,
            repo_root=repo_root,
            identity_cache=identity_cache,
            subject_cache=subject_cache,
        )
        if classified is None or classified[0] == "unpinned_derivable":
            continue
        kind, successor, _repairable = classified
        if kind == "unpinned_derivable":
            continue
        if kind in {
            "pinned_but_missing",
            "pinned_mismatch",
            "ambiguous_successor",
            "closed_no_pin",
            "closed_unfindable",
        }:
            drifts.append(
                Drift(
                    wave_id=wave_id,
                    kind=kind,
                    state_commit=pinned,
                    git_commit=successor,
                )
            )
    logger.info(
        f"detect_git_state_drift waves={len(state.waves)} drifts={len(drifts)} "
        f"acked={len(acked)} git_available={git_available}"
    )
    return drifts


def _shas_match(a: str, b: str) -> bool:
    """Compare two git SHAs prefix-tolerantly.

    ``Wave.commit`` is validated as ``ShaStr`` (40-hex), but tests and
    ad-hoc tools sometimes record a short prefix; ``derive_wave_sha``
    always returns the full 40-hex. Both directions of prefix-match
    succeed when one ref is a strict prefix of the other.
    """
    if a == b:
        return True
    return a.startswith(b) or b.startswith(a)


# ---- Commit-pin verify + repair --------------------------------------------
#
# ``detect_git_state_drift`` is the *hard-drift* detector that ``doctor`` and
# ``status`` consume: it only surfaces a closed wave when its recorded pointer
# is provably wrong (or indeterminate). The verify/repair scan below is a
# superset built for ``eawf wave verify-commits``: in addition to the four
# hard-drift kinds it also flags the *soft* ``unpinned_derivable`` case -- a
# closed wave with no ``Wave.commit`` whose SHA is still derivable from the
# bracketed commit subject. That case is silently fine for ``wave show``
# (the derive fallback covers it), but ``--repair`` can harden the derivable
# SHA into an explicit pin so future reads do not re-query git.

CommitPinKind = Literal[
    "pinned_but_missing",
    "pinned_mismatch",
    "ambiguous_successor",
    "closed_no_pin",
    "closed_unfindable",
    "unpinned_derivable",
]
RepairBasis = Literal[
    "semantic_identity",
    "unique_first_parent",
    "unique_legacy_title_match",
]


@dataclass(frozen=True)
class CommitPinIssue:
    """One commit-pin issue surfaced by :func:`scan_commit_pins`.

    Attributes:
        wave_id: The closed wave whose ``Wave.commit`` pin needs
            attention.
        kind: Which issue shape (see :data:`CommitPinKind`). The first
            four kinds mirror :class:`DriftKind`; ``unpinned_derivable``
            is the soft, harden-able extra that :func:`scan_commit_pins`
            adds on top of :func:`detect_git_state_drift`.
        state_commit: The SHA currently recorded in ``Wave.commit``
            (``None`` for the unpinned kinds).
        git_commit: The SHA ``git log --grep`` derived (``None`` for the
            kinds where git produced nothing).
        repairable: ``True`` when ``--repair`` can re-pin a derivable
            SHA (``pinned_mismatch`` and ``unpinned_derivable``); ``False``
            for the kinds with no derivable SHA to pin
            (``pinned_but_missing`` / ``closed_no_pin`` /
            ``closed_unfindable``).
    """

    wave_id: str
    kind: CommitPinKind
    state_commit: str | None = None
    git_commit: str | None = None
    git_identity_digest: str | None = None
    repair_basis: RepairBasis | None = None
    repairable: bool = False


def _repair_basis(
    *,
    wave_id: str,
    pinned: str | None,
    stored_digest: str | None,
    successor: str | None,
    candidates: Mapping[str, list[str]],
    repo_root: Path | None,
    identity_cache: dict[str, str | None],
) -> RepairBasis | None:
    """Classify the evidence that uniquely selected one repair target."""
    if successor is None:
        return None

    def identity(sha: str) -> str | None:
        if sha not in identity_cache:
            identity_cache[sha] = commit_identity_digest(sha, repo_root=repo_root)
        return identity_cache[sha]

    prior_digest = stored_digest
    if prior_digest is None and pinned is not None:
        prior_digest = identity(pinned)
    if prior_digest is not None and identity(successor) == prior_digest:
        return "semantic_identity"
    if len(_candidate_shas(wave_id, candidates)) == 1:
        return "unique_first_parent"
    return "unique_legacy_title_match"


def scan_commit_pins(
    state: State,
    *,
    repo_root: Path | None = None,
    acked_wave_ids: set[str] | None = None,
) -> list[CommitPinIssue]:
    """Scan every CLOSED wave's commit pin for drift OR a harden-able gap.

    A superset of :func:`detect_git_state_drift`: the four hard-drift
    kinds are reported identically, and one extra soft kind --
    ``unpinned_derivable`` -- is added for a closed wave that has no
    ``Wave.commit`` but whose SHA is still derivable from the bracketed
    commit subject. ``detect_git_state_drift`` treats that case as clean
    (the ``wave show`` derive fallback covers it); this scan surfaces it
    so ``eawf wave verify-commits --repair`` can pin the derivable SHA.
    Acknowledged historical rows are skipped using the same policy as
    :func:`detect_git_state_drift`: an explicit *acked_wave_ids* set wins;
    otherwise ``<repo_root>/.eawf/drift-acks.json`` is loaded when
    *repo_root* is supplied.

    Repair policy encoded on each row's ``repairable`` flag:

    - ``pinned_mismatch`` -> repairable; re-pin to the git-derived SHA
      (git history is ground truth for what actually landed).
    - ``unpinned_derivable`` -> repairable; pin the derivable SHA.
    - ``pinned_but_missing`` / ``closed_no_pin`` / ``closed_unfindable``
      -> NOT repairable; there is no derivable SHA to pin, so the
      operator must reconcile by hand (recover the commit, re-run with
      git on PATH, or re-close the wave).

    Args:
        state: The validated :class:`State` to walk.
        repo_root: Repository working directory; defaults to the process
            cwd via the subprocess machinery in :func:`derive_wave_sha`.
        acked_wave_ids: Wave ids whose commit-pin issue the operator has
            already acknowledged; ``None`` means load the repo ack file
            when *repo_root* is supplied, otherwise use no acks.

    Returns:
        List of :class:`CommitPinIssue` rows ordered by ``wave_id`` so
        render output stays stable. Empty list when every closed wave
        carries an in-sync pin.
    """
    from eawf.kernel.state.enums import WaveStatus

    acked = _resolve_acked_wave_ids(repo_root=repo_root, acked_wave_ids=acked_wave_ids)
    git_available = shutil.which("git") is not None
    reachable_wave_keys = _reachable_wave_keys(repo_root) if git_available else {}
    reachable_shas = set(reachable_wave_keys)
    candidates = _first_parent_wave_candidates(repo_root) if git_available else {}
    identity_cache: dict[str, str | None] = {}
    subject_cache: dict[str, str | None] = {}

    issues: list[CommitPinIssue] = []
    for wave_id in sorted(state.waves):
        wave = state.waves[wave_id]
        if wave.status != WaveStatus.CLOSED:
            continue
        if wave_id in acked:
            continue
        pinned = wave.commit
        if not git_available:
            issues.append(
                CommitPinIssue(
                    wave_id=wave_id,
                    kind="closed_unfindable" if pinned is None else "pinned_but_missing",
                    state_commit=pinned,
                    git_commit=None,
                    repairable=False,
                )
            )
            continue
        classified = _classify_wave_pin(
            wave_id=wave_id,
            wave_title=wave.title,
            pinned=pinned,
            stored_digest=wave.commit_identity_digest,
            reachable_shas=reachable_shas,
            reachable_wave_keys=reachable_wave_keys,
            candidates=candidates,
            repo_root=repo_root,
            identity_cache=identity_cache,
            subject_cache=subject_cache,
        )
        if classified is None:
            continue
        kind, successor, repairable = classified
        issues.append(
            CommitPinIssue(
                wave_id=wave_id,
                kind=kind,
                state_commit=pinned,
                git_commit=successor,
                git_identity_digest=(
                    commit_identity_digest(successor, repo_root=repo_root)
                    if successor is not None
                    else None
                ),
                repair_basis=(
                    _repair_basis(
                        wave_id=wave_id,
                        pinned=pinned,
                        stored_digest=wave.commit_identity_digest,
                        successor=successor,
                        candidates=candidates,
                        repo_root=repo_root,
                        identity_cache=identity_cache,
                    )
                    if repairable
                    else None
                ),
                repairable=repairable,
            )
        )
    logger.info(
        f"scan_commit_pins waves={len(state.waves)} issues={len(issues)} "
        f"acked={len(acked)} git_available={git_available}"
    )
    return issues


@dataclass(frozen=True)
class RepairAction:
    """One re-pin applied by :func:`repair_commit_pins`.

    Attributes:
        wave_id: The wave whose ``Wave.commit`` was re-pinned.
        kind: The :data:`CommitPinKind` that motivated the re-pin.
        old_commit: The prior ``Wave.commit`` value (``None`` when the
            wave was unpinned before the repair).
        new_commit: The git-derived 40-hex SHA now pinned.
    """

    wave_id: str
    kind: CommitPinKind
    old_commit: str | None
    new_commit: str
    identity_digest: str | None = None
    basis: RepairBasis = "unique_first_parent"


def repair_commit_pins(
    state: State, issues: list[CommitPinIssue]
) -> tuple[list[RepairAction], list[CommitPinIssue]]:
    """Re-pin every repairable issue in *issues* against *state* in place.

    Mutates ``state.waves[wave_id].commit`` for each repairable row
    (``pinned_mismatch`` / ``unpinned_derivable``) to its git-derived
    SHA. Non-repairable rows are returned untouched so the caller can
    report them as skipped.

    The function is a pure in-process mutator: it never reads git (the
    derived SHA was already resolved by :func:`scan_commit_pins` and
    carried on :attr:`CommitPinIssue.git_commit`) and never touches
    disk -- the caller persists ``state`` through the canonical writer.

    Args:
        state: Loaded :class:`State`; mutated in place.
        issues: Rows from :func:`scan_commit_pins` for *state*.

    Returns:
        A ``(repaired, skipped)`` pair: ``repaired`` lists one
        :class:`RepairAction` per re-pinned wave; ``skipped`` lists the
        non-repairable :class:`CommitPinIssue` rows verbatim.
    """
    repaired: list[RepairAction] = []
    skipped: list[CommitPinIssue] = []
    for issue in issues:
        if not issue.repairable or issue.git_commit is None:
            skipped.append(issue)
            continue
        wave = state.waves.get(issue.wave_id)
        if wave is None:
            # Defensive: the scan was taken from this same state, so a
            # missing wave here means the caller passed a stale issue
            # list. Skip rather than raise so a partial repair still
            # records the rows it could apply.
            skipped.append(issue)
            continue
        old_commit = wave.commit
        wave.commit = issue.git_commit
        wave.commit_identity_digest = issue.git_identity_digest
        repaired.append(
            RepairAction(
                wave_id=issue.wave_id,
                kind=issue.kind,
                old_commit=old_commit,
                new_commit=issue.git_commit,
                identity_digest=issue.git_identity_digest,
                basis=issue.repair_basis or "unique_first_parent",
            )
        )
        logger.info(
            f"repair_commit_pins wave={issue.wave_id} kind={issue.kind} "
            f"old={old_commit!r} new={issue.git_commit}"
        )
    return repaired, skipped
