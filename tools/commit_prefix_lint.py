"""Commit-msg + diff scope linter for eawf phase-bundled commits.

Enforces:

1. Subject line must match one of three prefix grammars:

   - **Wave form** (planned wave deliverable):
     ``^\\[P\\d{2,}(-I\\d{2,})?-W\\d{2,}\\]\\s+<type>:\\s+\\S.*$``
     where ``<type>`` is one of ``feat|fix|chore|docs|refactor|test|
     build|perf|ci|revert|state``. The ``-W##`` suffix declares the
     wave the commit advances.

   - **Bare phase/iter form** (post-P26-W23): a bare
     ``[P##(-I##)?]`` prefix with ``type`` ∈ {``state``, ``docs``}:
     ``^\\[P\\d{2,}(-I\\d{2,})?\\]\\s+(state|docs):\\s+\\S.*$``
     ``state`` is the canonical signal for phase/iter-scope
     bookkeeping; ``docs`` carries phase/iter-scoped documentation
     artifacts that no single wave owns (closure audits, promoted
     research / decision / incident briefs).

     The ``-CORE`` suffix is retired. It survives only in commits
     already on the trunk, where ``git log`` reads it as the
     pre-P26-W23 spelling of ``[P##] state:``; this lint rejects it
     in anything new, because the conventional-commit ``type`` is
     the semantic signal and a second carrier for it is drift.

   - **Trailer form** (the default written form): a bare
     ``<type>: <subject>`` with no bracket prefix, plus an
     ``Eawf-Wave: P##-I##-W##`` trailer in the body naming the wave the
     commit advances. Selected by ``vcs.conventions.subject_style``,
     which defaults to ``trailer``.

     Under ``subject_style: trailer`` the wave form above still passes,
     but with a deprecation warning on stderr (exit code stays ``0``);
     the bare ``[P##(-I##)?] state|docs:`` form is exempt because it
     advances no single wave and so has no trailer to carry.

     A bare ``<type>: <subject>`` with NO ``Eawf-Wave`` trailer is
     accepted only when ``state.current.phase_id`` is ``None`` (no
     ACTIVE phase) — an out-of-phase commit advances no wave, so
     neither carrier has anything to name. With an ACTIVE phase it is
     rejected: under ``trailer`` for the missing trailer, under
     ``bracket`` for the missing bracket prefix.

   ``W00`` and ``I00`` are rejected in the bracketed forms: wave /
   iter indices are 1-based by convention, and reactive waves get the
   next available ``W##`` per the feedback-commit-prefix-taxonomy
   memory. Phase / iter / wave id width widened to ``\\d{2,}`` so
   3-digit ids are accepted once the queue grows
   that far.

2. State-bookkeeping path whitelist applies to any commit with
   ``type == "state"`` — the canonical, and only, semantic signal.
   It fires on the wave form and on the bare ``[P##]`` /
   ``[P##-I##]`` form alike.

   State-scoped commits MUST touch only state-bookkeeping paths
   (``.ea/state.json``, ``.ea/store/event.jsonl``,
   ``.ea/store/audit.jsonl``, ``.secrets.baseline``, and per-wave
   spec files under ``.ea/specs/``). Touching anything else is
   rejected.

   Bare ``[P##(-I##)?] docs:`` commits are similarly path-gated:
   they MUST touch only ``.ea/artifacts/**`` (promoted documentation
   artifacts). Wave-form ``[P##-W##] docs:`` commits are unrestricted.

3. A recognized Claude or Codex ``Co-Authored-By`` trailer MUST be
   present (the ``prepare-commit-msg`` stage hook auto-inserts it when
   the active harness is detected; this backstop rejects commits where
   the trailer was hand-deleted).

4. Wave-close bookkeeping rides the wave commit. A ``state``-typed
   subject that closes exactly one wave — ``[P31-I01] state: close
   W27`` — is rejected: those close records belong on that wave's own
   commit, staged and folded in with ``git commit --amend``. A claim
   batch, an iter close, or a phase close names no single wave and
   stays a bare ``[P##] state:`` commit.

5. One commit per wave. A commit naming a wave that already has a
   commit reachable from ``HEAD`` is rejected. Exempt: a commit whose
   staged paths are all on the state-bookkeeping whitelist (that IS
   the fold amend), an amend of ``HEAD`` itself (which rewrites the
   wave's commit rather than adding one, so the wave still ends up with
   exactly one), and the ``state`` / ``test`` types (bookkeeping and
   the managed-golden refresh the snapshot-pairing gate forces into its
   own paired commit). Genuinely new work appends a reactive wave and
   commits under its own ``W##`` id.

6. Wave-source authorization. A non-``state`` commit naming a wave must
   prove that wave is CLAIMED or IN_PROGRESS in the **canonical**
   main-worktree ``state.json``. A fold amend is the exception: when
   every staged path is on the state-bookkeeping whitelist, a CLOSED
   wave proves the commit too, because the close records the fold
   exists to carry only exist once the wave is closed. Adding
   deliverable bytes under a CLOSED wave stays rejected.

All checks run as a ``commit-msg``-stage pre-commit hook. The first
argument is the commit-message file path (pre-commit passes it). The
linter consults ``git diff --cached --name-only`` for staged paths.

Exit codes:
- ``0`` — accepted (an advisory deprecation warning may still print to
  stderr).
- ``1`` — rejected (message printed to stderr).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coauthor_policy import (
    SUPPORTED_TRAILERS,
    coauthor_disabled,
    has_any_coauthor_trailer,
    has_supported_trailer,
)

_TYPES = "feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state"

# Subject grammar — three accepted forms:
#
# 1. Wave form: ``[P##(-I##)?-W##] <type>: ...`` — the ``-W##``
#    suffix is mandatory. The retired ``-CORE`` alias is no longer
#    accepted here; ``type == "state"`` on form 2 carries what it
#    used to.
# 2. State-bookkeeping form: ``[P##(-I##)?] <state|docs>: ...`` —
#    post-P26-W23 grammar; valid only when the conventional-commit
#    type is ``state`` (any path on the state whitelist) or ``docs``
#    (restricted to ``.ea/artifacts/**``).
# 3. Trailer form: ``<type>: <subject>`` with no bracket prefix — the
#    default written form, carrying the wave in an ``Eawf-Wave`` body
#    trailer. Without that trailer it is accepted ONLY when
#    ``state.current.phase_id`` is ``None`` (no ACTIVE phase), because
#    an out-of-phase commit advances no wave to name.
#
# The negative lookaheads ``(?!00)`` on both the iter and wave digit
# pairs reject ``I00`` / ``W00``: wave and iter indices are 1-based
# throughout the eawf state model, and reactive waves append the next
# available ``W##`` per the feedback-commit-prefix-taxonomy memory.
# The digit-width is ``\d{2,}`` (not ``\d{2}``) so 3+ digit ids are
# accepted once the queue grows past P/I/W 99.
_SUBJECT_WAVE_RE = re.compile(
    r"^\[P\d{2,}(-I(?!00)\d{2,})?-W(?!00)\d{2,}\]\s+"
    rf"(?P<type>{_TYPES}):\s+\S.*$"
)
_SUBJECT_BARE_RE = re.compile(
    r"^\[P\d{2,}(-I(?!00)\d{2,})?\]\s+"
    r"(?P<type>state|docs):\s+\S.*$"
)
_SUBJECT_BARE_CONVENTIONAL_RE = re.compile(rf"^(?P<type>{_TYPES}):\s+\S.*$")
_WAVE_TRAILER_NAME = "Eawf-Wave"
_WAVE_TRAILER_RE = re.compile(
    rf"^{_WAVE_TRAILER_NAME}:\s+"
    r"(?P<wave>P\d{2,}(?:-I(?!00)\d{2,})?-W(?!00)\d{2,})\s*$",
    re.MULTILINE,
)
_BRACKET_SCOPE_RE = re.compile(
    r"^\[(?P<phase>P\d{2,})"
    r"(?:-(?P<iter>I(?!00)\d{2,}))?"
    r"(?:-(?P<wave>W(?!00)\d{2,}))?\]"
)
_FULL_WAVE_SCOPE_RE = re.compile(
    r"^(?P<phase>P\d{2,})"
    r"(?:-(?P<iter>I(?!00)\d{2,}))?"
    r"-(?P<wave>W(?!00)\d{2,})$"
)
_RELEASE_ANNOTATION_RE: re.Pattern[str]  # bound below, from the workflow mirror.
# Fires on ANY ``release=`` substring, not just the standalone
# ``(release=`` paren group. The .github/workflows/phase-release.yaml
# extraction regex only tags when the annotation is its own paren group,
# so a fused shape like ``(audit=A-x, release=v0.6.0)`` — a one-character
# malformation — would silently zero the tag + PyPI + npm publish. Detecting
# the broader signal lets the lint reject that shape before it lands.
_RELEASE_ANNOTATION_SIGNAL_RE = re.compile(r"release=")
# Byte-for-byte copy of the phase-release.yaml extraction regex, kept as a
# module constant so the reject diagnostic and the workflow stay in lockstep.
# The lockstep is enforced by a test that reads the workflow, because drifting
# it silently is the exact failure it exists to prevent: a narrower copy here
# rejects an annotation the workflow would have tagged, so the phase cannot
# close, while a wider copy admits one the workflow would skip.
_WORKFLOW_RELEASE_EXTRACTION_RE = r"\(release=(v\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?(?:\.dev\d+)?)\)"
# The acceptance check compiles the mirror rather than restating it. A second
# hand-written copy is what drifted: the checker rejected a shape the workflow
# accepted, which is a rejection that cannot be right by construction, since
# the workflow is the thing the check exists to predict.
_RELEASE_ANNOTATION_RE = re.compile(_WORKFLOW_RELEASE_EXTRACTION_RE)
_SUBJECT_STYLE_BRACKET = "bracket"
_SUBJECT_STYLE_TRAILER = "trailer"
# Mirrors ``vcs.conventions.subject_style`` in src/eawf/kernel/config/defaults.py.
# The hook runs under system Python and cannot import the package, so the two
# defaults are kept in lockstep by hand and pinned by
# tests/unit/kernel/config/test_subject_style_default.py.
_SUBJECT_STYLE_DEFAULT = _SUBJECT_STYLE_TRAILER
_BRACKET_FORM_DEPRECATION = (
    "deprecated bracket-prefix wave subject: {subject!r}\n"
    "vcs.conventions.subject_style is 'trailer': write '<type>: <summary>' "
    f"with an '{_WAVE_TRAILER_NAME}: P##-I##-W##' trailer instead. The bracket "
    "form still passes, but it is deprecated and its acceptance will be "
    "withdrawn once the trailer form is universal."
)
_STATE_ONLY_ALLOWED = (
    ".ea/state.json",
    # ``.secrets.baseline`` auto-tracks state.json line numbers; the
    # detect-secrets pre-commit hook regenerates it whenever state.json
    # mutates, and refuses to commit when baseline is left unstaged.
    # State-bookkeeping commits therefore always need it riding along.
    ".secrets.baseline",
)
# The TYPED per-kind JSONLs under ``.ea/store/`` are daemon-written, committed
# stores (audit / evidence / decision / flow / role reports / memory) per the
# authority map. They ride the state-bookkeeping surface: e.g. the deterministic
# close gate appends ``evidence.jsonl`` rows as a wave closes, so a state commit
# carries them alongside ``state.json``. ``.ea/specs/`` carries the in-band wave
# spec bodies authored as state.
#
# ``event.jsonl`` is the exception and is NOT committed (gitignored): it is the
# firehose rather than the ledger — one row per mutation plus the raw stdout of
# every spawned agent — so it grows without bound and carries free text nobody
# typed. The prefix below still admits it if a repo chooses to track it; this
# repo does not.
_STATE_ONLY_PREFIXES = (".ea/store/", ".ea/specs/")

# Bare ``[P##(-I##)?] docs:`` commits carry phase/iter-scoped
# documentation artifacts that no single wave owns (closure audits,
# promoted research / decision / incident briefs). They are restricted to
# the promoted-artifact tree; wave-produced docs use the
# ``[P##-W##] docs:`` wave form, which accepts any path.
_DOCS_BARE_PREFIXES = (".ea/artifacts/",)
_CLAIMED_PROOF_STATUSES = frozenset({"claimed", "in_progress"})
# The fold amend stages only state-bookkeeping paths, and the records it folds
# in - the close row plus its evidence - exist only once the wave is CLOSED.
# Demanding a live status there shuts the door the single-wave-close diagnostic
# tells the operator to walk through, so CLOSED joins the accepted set for that
# shape alone; an amend that adds deliverable bytes still needs a live wave.
_FOLD_PROOF_STATUSES = _CLAIMED_PROOF_STATUSES | {"closed"}

# A state subject that names exactly one wave AND a close verb is the
# per-wave close record the fold moved onto the wave commit itself.
_CLOSE_VERB_RE = re.compile(r"\bclos(?:e|es|ed|ing)\b", re.IGNORECASE)
_WAVE_TOKEN_RE = re.compile(r"\bW\d{2,}\b")
# ``state`` is the bookkeeping surface, already gated by the path whitelist;
# ``test`` carries the managed-golden refresh that the snapshot-pairing gate
# forces into its own paired commit (the commit-granularity exception).
_WAVE_COMMIT_CAP_EXEMPT_TYPES = frozenset({"state", "test"})
_WAVE_LOG_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class _ScopeRef:
    """Normalized lifecycle reference parsed from a subject or trailer."""

    phase_id: str
    iter_id: str | None = None
    wave_id: str | None = None


def _find_repo_root(start: Path | None = None) -> Path:
    """Return nearest ancestor with ``.ea`` or ``.git``; cwd fallback."""
    cwd = Path.cwd() if start is None else start
    for parent in [cwd, *cwd.parents]:
        if (parent / ".ea").exists() or (parent / ".git").exists():
            return parent
    return cwd


def _configured_subject_style(repo_root: Path | None = None) -> str:
    """Return ``vcs.conventions.subject_style`` from repo/local config.

    The hook runs under system Python, so this intentionally avoids importing
    package YAML dependencies. It reads the two file-backed repo layers the
    hook can see directly; missing or malformed values fall back to
    :data:`_SUBJECT_STYLE_DEFAULT`.
    """
    root = _find_repo_root(repo_root)
    style = _SUBJECT_STYLE_DEFAULT
    for config_path in (root / ".ea" / "config.yaml", root / ".ea" / "local" / "config.yaml"):
        candidate = _subject_style_from_config(config_path)
        if candidate is not None:
            style = candidate
    return style


def _subject_style_from_config(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_vcs = False
    in_conventions = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            in_vcs = stripped == "vcs:"
            in_conventions = False
            continue
        if in_vcs and indent == 2:
            in_conventions = stripped == "conventions:"
            continue
        if in_vcs and in_conventions and indent == 4 and stripped.startswith("subject_style:"):
            value = _strip_yaml_scalar(stripped.split(":", 1)[1])
            if value in {_SUBJECT_STYLE_BRACKET, _SUBJECT_STYLE_TRAILER}:
                return value
    return None


def _strip_yaml_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _has_wave_trailer(text: str) -> bool:
    return _WAVE_TRAILER_RE.search(text) is not None


def _load_managed_state(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a managed state document, distinguishing absence from corruption."""
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"managed state decode failed at {path}: {exc}"
    if not isinstance(raw, dict):
        return None, f"managed state decode failed at {path}: root must be an object"
    for key in ("current", "phases", "iters", "waves"):
        if not isinstance(raw.get(key), dict):
            return None, f"managed state decode failed at {path}: {key!r} must be an object"
    return raw, None


def _current_phase_active(state_path: Path | None = None) -> bool:
    """Return True when ``state.current.phase_id`` is non-null.

    Walks upward from cwd to find ``.ea/state.json`` when *state_path*
    is omitted. Missing file, unreadable JSON, or null ``phase_id``
    all read as "no ACTIVE phase" (returns ``False``), which is the
    safe default — it lets the pre-flight chore commit subject parse
    in fresh checkouts and in environments where the lint runs
    outside a state-resident project.
    """
    if state_path is None:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / ".ea" / "state.json"
            if candidate.is_file():
                state_path = candidate
                break
    if state_path is None or not state_path.is_file():
        return False
    data, error = _load_managed_state(state_path)
    if error is not None or data is None:
        return False
    current = data.get("current")
    if not isinstance(current, dict):
        return False
    return current.get("phase_id") is not None


def _subject_scope_ref(subject: str) -> _ScopeRef | None:
    """Return the normalized lifecycle reference carried by *subject*."""
    match = _BRACKET_SCOPE_RE.match(subject)
    if match is None:
        return None
    phase_id = match.group("phase")
    iter_token = match.group("iter")
    wave_token = match.group("wave")
    iter_id = f"{phase_id}-{iter_token}" if iter_token is not None else None
    if wave_token is not None:
        iter_id = iter_id or f"{phase_id}-I01"
        return _ScopeRef(
            phase_id=phase_id,
            iter_id=iter_id,
            wave_id=f"{iter_id}-{wave_token}",
        )
    return _ScopeRef(phase_id=phase_id, iter_id=iter_id)


def _trailer_scope_ref(text: str) -> _ScopeRef | None:
    """Return the normalized ``Eawf-Wave`` reference, if present."""
    trailer = _WAVE_TRAILER_RE.search(text)
    if trailer is None:
        return None
    match = _FULL_WAVE_SCOPE_RE.match(trailer.group("wave"))
    if match is None:  # pragma: no cover - the trailer regex already guarantees shape
        return None
    phase_id = match.group("phase")
    iter_token = match.group("iter") or "I01"
    iter_id = f"{phase_id}-{iter_token}"
    return _ScopeRef(
        phase_id=phase_id,
        iter_id=iter_id,
        wave_id=f"{iter_id}-{match.group('wave')}",
    )


def _canonical_state_path(repo_root: Path) -> Path | None:
    """Resolve the main-worktree state through Git's supported common-dir API."""
    proc = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    common_dir = Path(raw)
    if not common_dir.is_absolute():
        return None
    return common_dir.parent / ".ea" / "state.json"


def _validate_scope_hierarchy(state: Mapping[str, Any], ref: _ScopeRef) -> str | None:
    """Return a diagnostic when *ref* does not resolve bidirectionally."""
    phases = state.get("phases")
    iters = state.get("iters")
    waves = state.get("waves")
    if not isinstance(phases, dict) or not isinstance(iters, dict) or not isinstance(waves, dict):
        return "managed state hierarchy unavailable"
    phase = phases.get(ref.phase_id)
    if not isinstance(phase, dict):
        return f"unknown phase reference: {ref.phase_id!r}"
    if ref.iter_id is None:
        return None
    iteration = iters.get(ref.iter_id)
    if not isinstance(iteration, dict):
        return f"unknown iter reference: {ref.iter_id!r}"
    iter_error = _validate_iter_hierarchy(phase, iteration, ref)
    if iter_error is not None or ref.wave_id is None:
        return iter_error
    wave = waves.get(ref.wave_id)
    if not isinstance(wave, dict):
        return f"unknown wave reference: {ref.wave_id!r}"
    return _validate_wave_hierarchy(iteration, wave, ref)


def _validate_iter_hierarchy(
    phase: Mapping[str, Any],
    iteration: Mapping[str, Any],
    ref: _ScopeRef,
) -> str | None:
    """Validate both directions of one phase-to-iter edge."""
    assert ref.iter_id is not None
    phase_iters = phase.get("iter_ids")
    if not isinstance(phase_iters, list) or ref.iter_id not in phase_iters:
        return (
            f"wrong phase/iter hierarchy: phase {ref.phase_id!r} "
            f"does not contain iter {ref.iter_id!r}"
        )
    if iteration.get("phase_id") != ref.phase_id:
        return (
            f"wrong phase/iter hierarchy: iter {ref.iter_id!r} "
            f"belongs to {iteration.get('phase_id')!r}"
        )
    return None


def _validate_wave_hierarchy(
    iteration: Mapping[str, Any],
    wave: Mapping[str, Any],
    ref: _ScopeRef,
) -> str | None:
    """Validate both directions of one iter-to-wave edge."""
    assert ref.iter_id is not None
    assert ref.wave_id is not None
    iter_waves = iteration.get("wave_ids")
    if not isinstance(iter_waves, list) or ref.wave_id not in iter_waves:
        return (
            f"wrong iter/wave hierarchy: iter {ref.iter_id!r} does not contain wave {ref.wave_id!r}"
        )
    if wave.get("iter_id") != ref.iter_id:
        return f"wrong iter/wave hierarchy: wave {ref.wave_id!r} belongs to {wave.get('iter_id')!r}"
    return None


def _validate_commit_scope_refs(
    refs: list[tuple[str, _ScopeRef]],
    *,
    managed_state: Mapping[str, Any] | None,
    state_path: Path | None,
    repo_root: Path | None,
    commit_type: str,
    canonical_state_path: Path | None,
    state_only_fold: bool,
) -> str | None:
    """Return the first hierarchy/authorization rejection for commit refs.

    *state_only_fold* is forwarded to the claimed-proof check, which widens
    the accepted canonical statuses for a commit that stages bookkeeping only.
    """
    if refs and state_path is not None and managed_state is None:
        return "managed state hierarchy unavailable: state.json is missing"
    if managed_state is None:
        return None
    resolved_canonical = canonical_state_path
    for origin, ref in refs:
        hierarchy_error = _validate_scope_hierarchy(managed_state, ref)
        if hierarchy_error is not None:
            return f"{origin} rejected: {hierarchy_error}"
        if ref.wave_id is None or commit_type == "state":
            continue
        active_error = _validate_active_source_scope(managed_state, ref)
        if active_error is not None:
            return f"{origin} rejected: {active_error}"
        if resolved_canonical is None:
            assert state_path is not None
            anchor = repo_root or _find_repo_root(state_path.parent)
            resolved_canonical = _canonical_state_path(anchor)
        proof_error = _validate_claimed_proof(
            ref,
            canonical_state_path=resolved_canonical,
            state_only_fold=state_only_fold,
        )
        if proof_error is not None:
            return f"{origin} rejected: {proof_error}"
    return None


def _validate_active_source_scope(state: Mapping[str, Any], ref: _ScopeRef) -> str | None:
    """Require a wave source commit to target the current ACTIVE phase/iter."""
    if ref.iter_id is None or ref.wave_id is None:
        return None
    current = state.get("current")
    phases = state.get("phases")
    iters = state.get("iters")
    if not isinstance(current, dict) or not isinstance(phases, dict) or not isinstance(iters, dict):
        return "managed state active scope unavailable"
    phase = phases.get(ref.phase_id)
    iteration = iters.get(ref.iter_id)
    if not isinstance(phase, dict) or not isinstance(iteration, dict):
        return "managed state active scope unavailable"
    if current.get("phase_id") != ref.phase_id or phase.get("status") != "active":
        return (
            f"source commit phase is not current ACTIVE phase: "
            f"referenced={ref.phase_id!r} current={current.get('phase_id')!r}"
        )
    if current.get("iter_id") != ref.iter_id or iteration.get("status") != "active":
        return (
            f"source commit iter is not current ACTIVE iter: "
            f"referenced={ref.iter_id!r} current={current.get('iter_id')!r}"
        )
    return None


def _validate_claimed_proof(
    ref: _ScopeRef,
    *,
    canonical_state_path: Path | None,
    state_only_fold: bool,
) -> str | None:
    """Require live-wave proof from canonical main-worktree state.

    Args:
        ref: Lifecycle reference carried by the commit's subject or trailer.
        canonical_state_path: Path to the main worktree's ``state.json``,
            which is the only copy a worktree commit may be proven against.
        state_only_fold: True when every staged path is on the
            state-bookkeeping whitelist. Such a commit adds no deliverable
            bytes and exists to record the close, so a CLOSED wave proves it
            just as well as a live one.

    Returns:
        A diagnostic naming the failed proof, or ``None`` when it holds.
    """
    if ref.wave_id is None:
        return None
    if canonical_state_path is None or not canonical_state_path.is_file():
        return (
            f"claimed proof unavailable for wave {ref.wave_id!r}: "
            "canonical main-worktree state is missing"
        )
    canonical, error = _load_managed_state(canonical_state_path)
    if error is not None:
        return error
    if canonical is None:
        return (
            f"claimed proof unavailable for wave {ref.wave_id!r}: "
            "canonical main-worktree state is missing"
        )
    hierarchy_error = _validate_scope_hierarchy(canonical, ref)
    if hierarchy_error is not None:
        return f"canonical claimed proof rejected: {hierarchy_error}"
    waves = canonical["waves"]
    wave = waves[ref.wave_id]
    status = wave.get("status")
    accepted = _FOLD_PROOF_STATUSES if state_only_fold else _CLAIMED_PROOF_STATUSES
    if status not in accepted:
        expected = "CLAIMED, IN_PROGRESS or CLOSED" if state_only_fold else "CLAIMED or IN_PROGRESS"
        return (
            f"claimed proof rejected for wave {ref.wave_id!r}: "
            f"canonical status {status!r} is not {expected}"
        )
    return None


def _staged_paths() -> list[str]:
    """Return paths reported by ``git diff --cached --name-only``.

    Empty list when there are no staged changes (e.g. an amend in the
    working copy, or the hook is invoked outside a commit context).
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_state_only_path(path: str) -> bool:
    if path in _STATE_ONLY_ALLOWED:
        return True
    return any(path.startswith(p) for p in _STATE_ONLY_PREFIXES)


def _is_docs_bare_path(path: str) -> bool:
    return any(path.startswith(p) for p in _DOCS_BARE_PREFIXES)


def _check_scoped_paths(
    *, commit_type: str, staged: list[str], is_bare: bool
) -> tuple[int, str] | None:
    """Enforce the per-scope path whitelist for state- and bare-docs commits.

    State-scoped commits (``type == 'state'``) must touch only
    state-bookkeeping paths. Bare ``[P##(-I##)?] docs:`` commits must touch
    only ``.ea/artifacts/**``. Wave-form ``[P##-W##] docs:`` commits are
    unrestricted (hence the *is_bare* gate on the docs branch).

    Returns a ``(1, diagnostic)`` rejection when a scoped commit strays
    outside its whitelist, else ``None``.
    """
    if commit_type == "state":
        bad = [p for p in staged if not _is_state_only_path(p)]
        if bad:
            return 1, (
                f"state-type commit touches non-state paths: {bad}\n"
                "state-scoped commits must mutate only .ea/state.json, "
                ".ea/store/**, .secrets.baseline, or .ea/specs/**"
            )
    elif is_bare and commit_type == "docs":
        bad = [p for p in staged if not _is_docs_bare_path(p)]
        if bad:
            return 1, (
                f"bare [P##] docs: commit touches non-artifact paths: {bad}\n"
                "bare-prefix docs commits carry phase/iter-scoped artifacts "
                "under .ea/artifacts/** only; wave-produced docs use the "
                "[P##-W##] docs: wave form"
            )
    return None


def _single_wave_close_rejection(subject: str, *, commit_type: str) -> tuple[int, str] | None:
    """Reject a state subject whose summary closes exactly one wave.

    Per-wave close records ride the wave commit, so the state-typed subjects
    left over are the ones that name no single wave: a claim batch, an iter
    close, a phase close. The bracket prefix is stripped first — the wave a
    commit is *scoped to* is not a wave it *closes*, so
    ``[P30-I21-W22] state: close iter + phase`` stays accepted.

    Returns a ``(1, diagnostic)`` rejection naming the wave commit the records
    belong on, else ``None``.
    """
    if commit_type != "state":
        return None
    summary = _BRACKET_SCOPE_RE.sub("", subject, count=1)
    if not _CLOSE_VERB_RE.search(summary):
        return None
    waves = sorted(set(_WAVE_TOKEN_RE.findall(summary)))
    if len(waves) != 1:
        return None
    return 1, (
        f"single-wave close bookkeeping rejected: {subject!r}\n"
        f"the close records for {waves[0]} ride that wave's own commit: stage "
        ".ea/state.json (plus the typed stores under .ea/store/) onto the "
        "cherry-picked wave commit and fold them in with 'git commit --amend', "
        "instead of writing a separate state commit. A bare '[P##] state:' "
        "commit stays correct for a claim batch, an iter close, or a phase "
        "close — none of those names a single wave."
    )


def _wave_grep_terms(ref: _ScopeRef) -> list[str]:
    """Return the fixed strings that identify *ref*'s wave in a commit message.

    Both bracket spellings are covered because executors emit the long
    ``[P##-I01-W##]`` form for I01 waves as often as the canonical short one,
    plus the ``Eawf-Wave`` trailer the default subject style writes.
    """
    assert ref.wave_id is not None
    phase, iter_token, wave_token = ref.wave_id.split("-")
    terms = [f"[{phase}-{iter_token}-{wave_token}]"]
    if iter_token == "I01":
        terms.append(f"[{phase}-{wave_token}]")
    terms.append(f"{_WAVE_TRAILER_NAME}: {ref.wave_id}")
    return terms


def _prior_wave_commits(terms: list[str], *, repo_root: Path | None) -> list[str]:
    """Return SHAs reachable from ``HEAD`` whose message carries one of *terms*.

    Scans ``HEAD`` only, never ``--all``: while a wave commit still lives on
    its worktree branch it must not count as its own predecessor, or every
    cherry-pick of it would be read as a second commit. Probe failures (git
    missing, unborn HEAD, timeout) return an empty list, so the cap fails open
    rather than blocking a commit it cannot reason about.
    """
    found: list[str] = []
    for term in terms:
        try:
            proc = subprocess.run(
                ["git", "log", "HEAD", f"--grep={term}", "-F", "--format=%H", "-n", "1"],
                cwd=None if repo_root is None else str(repo_root),
                check=False,
                capture_output=True,
                text=True,
                timeout=_WAVE_LOG_TIMEOUT_SECONDS,
            )
        except OSError, subprocess.SubprocessError:
            return []
        if proc.returncode != 0:
            continue
        found.extend(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return found


def _author_date_epoch(raw: str | None) -> str | None:
    """Return the epoch-seconds field of a git author date, else ``None``.

    Args:
        raw: A ``GIT_AUTHOR_DATE`` value in the internal form git exports to
            hooks, such as ``"@1788883700 +0200"``.

    Returns:
        The epoch-seconds digits, or ``None`` when *raw* is absent, empty, or
        spelled in any other format (an operator-supplied ISO date, say).
    """
    if not raw:
        return None
    fields = raw.strip().split()
    if not fields:
        return None
    token = fields[0].removeprefix("@")
    return token if token.isdigit() else None


def _head_identity(repo_root: Path | None) -> tuple[str, str] | None:
    """Return ``(sha, author_epoch)`` for ``HEAD``, or ``None`` when unknown.

    Args:
        repo_root: Directory the probe runs in; ``None`` uses the cwd.

    Returns:
        The tip's SHA and author-date epoch seconds. Probe failures (git
        missing, unborn HEAD, timeout, unparsable output) return ``None`` so
        the caller reads the commit as an ordinary append rather than an amend.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%H %at", "HEAD"],
            cwd=None if repo_root is None else str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=_WAVE_LOG_TIMEOUT_SECONDS,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    fields = proc.stdout.strip().split()
    if len(fields) != 2 or not fields[1].isdigit():
        return None
    return fields[0], fields[1]


def _amends_head(
    *,
    prior: list[str],
    repo_root: Path | None,
    env: Mapping[str, str],
) -> bool:
    """Return True when this commit rewrites ``HEAD`` instead of appending.

    ``git commit --amend`` reaches a commit-msg hook with the same argv and
    environment keys as an ordinary commit, so the rewrite has to be inferred
    from two signals taken together: the wave's existing commit is ``HEAD``
    (an amend can only rewrite the tip), and ``--amend`` preserves the original
    author date, which git exports to the hook as ``GIT_AUTHOR_DATE``. A fresh
    commit stamps the current time instead, so an author date equal to
    ``HEAD``'s own is the rewrite's fingerprint.

    The residual false-accept window is one clock second wide - a genuine
    second commit written inside the same second as the wave commit reads as
    an amend. That is the cheaper error: an uncapped extra commit costs one
    line of history, while a false reject leaves the operator unable to land
    the fold the cap's own diagnostic prescribes.

    Args:
        prior: SHAs of the commits already carrying the wave.
        repo_root: Directory the git probe runs in; ``None`` uses the cwd.
        env: Environment the hook was invoked with.

    Returns:
        ``True`` when both signals hold, else ``False``.
    """
    env_epoch = _author_date_epoch(env.get("GIT_AUTHOR_DATE"))
    if env_epoch is None:
        return False
    head = _head_identity(repo_root)
    if head is None:
        return False
    head_sha, head_epoch = head
    return head_sha in prior and head_epoch == env_epoch


def _check_wave_commit_cap(
    *,
    ref: _ScopeRef | None,
    commit_type: str,
    staged: list[str],
    repo_root: Path | None,
    env: Mapping[str, str],
) -> tuple[int, str] | None:
    """Cap a wave at one commit, outside the fold amend and the amend proper.

    Returns a ``(1, diagnostic)`` rejection when the named wave already has a
    commit on ``HEAD`` and this one appends deliverable bytes to it, else
    ``None``.
    """
    if ref is None or ref.wave_id is None:
        return None
    if commit_type in _WAVE_COMMIT_CAP_EXEMPT_TYPES:
        return None
    if all(_is_state_only_path(path) for path in staged):
        # The fold amend (and a bare reword, which stages nothing) adds no
        # deliverable bytes: it folds close bookkeeping into the commit that
        # already carries the wave.
        return None
    prior = _prior_wave_commits(_wave_grep_terms(ref), repo_root=repo_root)
    if not prior:
        return None
    if _amends_head(prior=prior, repo_root=repo_root, env=env):
        # An amend rewrites the wave's existing commit rather than adding one,
        # so the wave still ends up with exactly one commit. Rejecting it would
        # refuse the remedy the diagnostic below prescribes.
        return None
    return 1, (
        f"second commit for wave {ref.wave_id}: {prior[0][:12]} already carries it\n"
        "one commit per wave: fold this change into the wave commit with "
        "'git commit --amend', or — when it is genuinely new work — append a "
        "reactive wave and commit it under that wave's own W## id."
    )


def _extract_subject(text: str) -> str:
    """Return the first non-blank, non-comment line in *text*."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _match_subject(
    subject: str,
    state_path: Path | None,
    *,
    subject_style: str,
    has_wave_trailer: bool,
) -> tuple[re.Match[str] | None, bool, str]:
    """Return (match, is_bare_state_or_docs, error_diag).

    Tries the three accepted forms in order. The bare conventional-commits
    form is rejected when an ACTIVE phase exists; in that case the returned
    match is ``None`` and *error_diag* carries the rejection text. The
    ``is_bare_state_or_docs`` flag tells callers whether the matched form
    is the bracketed bare ``[P##(-I##)?] state|docs:`` form (which still
    needs path-whitelist enforcement).
    """
    wave_match = _SUBJECT_WAVE_RE.match(subject)
    bare_match = _SUBJECT_BARE_RE.match(subject)
    bracketed = wave_match or bare_match
    if bracketed is not None:
        return bracketed, bare_match is not None, ""
    bare_conventional = _SUBJECT_BARE_CONVENTIONAL_RE.match(subject)
    if bare_conventional is not None:
        if subject_style == _SUBJECT_STYLE_TRAILER and has_wave_trailer:
            return bare_conventional, False, ""
        if not _current_phase_active(state_path):
            # An out-of-phase commit advances no wave, so neither carrier has
            # anything to name: the bracket prefix and the Eawf-Wave trailer
            # are both vacuous and the bare subject is the only honest form.
            return bare_conventional, False, ""
        if subject_style == _SUBJECT_STYLE_TRAILER:
            return (
                None,
                False,
                (
                    f"trailer-style commit missing {_WAVE_TRAILER_NAME} trailer: {subject!r}\n"
                    f"set '{_WAVE_TRAILER_NAME}: P##-I##-W##' in the commit body, "
                    "or switch vcs.conventions.subject_style back to 'bracket'"
                ),
            )
        return (
            None,
            False,
            (
                f"bare conventional-commits subject rejected: {subject!r}\n"
                "an ACTIVE phase exists (state.current.phase_id is set); "
                "commits MUST carry a bracketed [P##-W##] / [P##-I##-W##] / "
                "[P##] / [P##-I##] prefix so lifecycle bookkeeping stays "
                "attributable. Bare '<type>: <subject>' is reserved for "
                "out-of-phase commits (state.current.phase_id is None)."
            ),
        )
    return (
        None,
        False,
        (
            f"commit subject rejected: {subject!r}\n"
            "expected '[P##-W##] <type>: <summary>', "
            "'[P##] state: <summary>' (canonical bookkeeping form), "
            "'[P##] docs: <summary>' (phase/iter-scoped artifact docs), "
            "or '<type>: <summary>' (bare conventional-commits, only when "
            "no ACTIVE phase is set in state.json) "
            "(W00 and I00 rejected — wave/iter indices are 1-based; "
            "type ∈ feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state; "
            "bare [P##] accepted only for type=state or type=docs)"
        ),
    )


def _check_coauthor(text: str, env: Mapping[str, str]) -> tuple[int, str]:
    """Return ``(0, "")`` when the co-author trailer policy is satisfied."""
    if coauthor_disabled(env):
        if has_any_coauthor_trailer(text):
            return 1, "co-author trailers are disabled by vcs.coauthor policy"
        return 0, ""
    if not has_supported_trailer(text):
        return 1, (
            f"missing recognized co-author trailer: {SUPPORTED_TRAILERS!r}\n"
            "the prepare-commit-msg hook inserts one when a supported "
            "harness is detected; otherwise paste a recognized trailer manually"
        )
    return 0, ""


def _check_release_annotation(subject: str) -> tuple[int, str] | None:
    """Validate an optional ``(release=vX.Y.Z)`` subject annotation.

    Fires on ANY ``release=`` substring, then accepts only when the
    annotation is shaped as its own paren group
    ``(release=v<MAJOR>.<MINOR>.<PATCH>[aN|bN|rcN])``. A fused shape such as
    ``(audit=A-x, release=v0.6.0)`` carries a ``release=`` signal but does not
    match the standalone group, so it is a hard reject: the
    .github/workflows/phase-release.yaml:46 extraction regex would fail to
    match it and silently skip the tag + PyPI + npm publish.

    Returns ``None`` when no ``release=`` signal is present or the annotation
    is well-formed; otherwise a ``(1, diagnostic)`` rejection naming the
    workflow regex.
    """
    if not _RELEASE_ANNOTATION_SIGNAL_RE.search(subject):
        return None
    if _RELEASE_ANNOTATION_RE.search(subject):
        return None
    return (
        1,
        (
            f"release annotation rejected: {subject!r}\n"
            "a 'release=' signal is present but not shaped as its own paren group "
            "'(release=v<MAJOR>.<MINOR>.<PATCH>[aN|bN|rcN])'. The "
            ".github/workflows/phase-release.yaml:46 extraction regex "
            f"'{_WORKFLOW_RELEASE_EXTRACTION_RE}' matches only the standalone group, "
            "so a fused '(audit=..., release=v...)' shape would silently skip the "
            "tag + PyPI + npm publish"
        ),
    )


def _accept_or_reject(
    text: str,
    env: Mapping[str, str],
    *,
    warning: str,
) -> tuple[int, str]:
    """Return the terminal verdict: the co-author reject, else *warning* at 0.

    A rejection's diagnostic always wins the single message slot; an advisory
    warning is only worth printing on a commit that is actually being accepted.
    """
    code, diag = _check_coauthor(text, env)
    if code != 0:
        return code, diag
    return 0, warning


def _carrier_mismatch(
    subject_ref: _ScopeRef | None,
    trailer_ref: _ScopeRef | None,
) -> tuple[int, str] | None:
    """Reject a commit whose two scope carriers name different waves.

    A commit may carry both the bracket prefix and the ``Eawf-Wave`` trailer
    during the migration to the trailer form; when it does, the two MUST agree
    or the wave a commit advances is ambiguous. ``None`` when they agree or
    only one carrier is present.
    """
    if subject_ref is None or subject_ref.wave_id is None or trailer_ref is None:
        return None
    if subject_ref == trailer_ref:
        return None
    return (
        1,
        (
            "subject/trailer hierarchy mismatch: "
            f"subject={subject_ref.wave_id!r} trailer={trailer_ref.wave_id!r}"
        ),
    )


def _bracket_form_deprecation(
    subject: str,
    *,
    subject_style: str,
    is_bare_bracketed: bool,
) -> str:
    """Return the deprecation warning for a bracket-prefix wave subject.

    Empty string when nothing is deprecated. The bare ``[P##] state:`` /
    ``[P##] docs:`` form is exempt: it advances no single wave, so the
    ``Eawf-Wave`` trailer has nothing to carry and the bracket stays the only
    scope carrier for phase/iter bookkeeping.
    """
    if subject_style != _SUBJECT_STYLE_TRAILER:
        return ""
    if is_bare_bracketed or not subject.startswith("["):
        return ""
    return _BRACKET_FORM_DEPRECATION.format(subject=subject)


def _check_wave_scope(
    *,
    subject: str,
    text: str,
    commit_type: str,
    staged: list[str],
    managed_state: Mapping[str, Any] | None,
    state_path: Path | None,
    repo_root: Path | None,
    canonical_state_path: Path | None,
    env: Mapping[str, str],
) -> tuple[int, str] | None:
    """Validate the wave a commit claims, in escalating specificity.

    The two scope carriers must name the same wave; that wave must resolve in
    managed state and be live (or, for a bookkeeping-only fold, closed); and
    only then does the one-commit-per-wave cap apply — a commit whose wave does
    not resolve has a more fundamental problem than how many commits that wave
    already has.
    """
    # A commit that stages nothing outside the state-bookkeeping whitelist is
    # the fold: it records a close rather than delivering bytes, so both the
    # claimed-proof check and the cap treat it as riding the wave's own commit.
    state_only_fold = all(_is_state_only_path(path) for path in staged)
    subject_ref = _subject_scope_ref(subject)
    trailer_ref = _trailer_scope_ref(text)
    mismatch = _carrier_mismatch(subject_ref, trailer_ref)
    if mismatch is not None:
        return mismatch
    refs = [
        (origin, ref)
        for origin, ref in (("subject", subject_ref), ("Eawf-Wave trailer", trailer_ref))
        if ref is not None
    ]
    scope_error = _validate_commit_scope_refs(
        refs,
        managed_state=managed_state,
        state_path=state_path,
        repo_root=repo_root,
        commit_type=commit_type,
        canonical_state_path=canonical_state_path,
        state_only_fold=state_only_fold,
    )
    if scope_error is not None:
        return 1, scope_error
    return _check_wave_commit_cap(
        ref=subject_ref if subject_ref is not None and subject_ref.wave_id else trailer_ref,
        commit_type=commit_type,
        staged=staged,
        repo_root=repo_root,
        env=env,
    )


def lint(
    message_path: Path,
    staged: list[str],
    env: Mapping[str, str] | None = None,
    state_path: Path | None = None,
    repo_root: Path | None = None,
    subject_style: str | None = None,
    canonical_state_path: Path | None = None,
) -> tuple[int, str]:
    """Run every subject, scope, path, cap, and trailer check on one commit.

    Returns ``(exit_code, diagnostic)``. A non-zero code means rejection and
    the diagnostic carries the reason; a zero code with a non-empty diagnostic
    is an accepted commit plus an advisory warning (the caller prints both to
    stderr). *state_path* lets tests inject a fixture ``state.json``;
    production callers leave it unset and the helper walks upward from cwd to
    find ``.ea/state.json``.
    """
    resolved_env: Mapping[str, str] = {} if env is None else env
    managed_state: dict[str, Any] | None = None
    if state_path is not None:
        managed_state, state_error = _load_managed_state(state_path)
        if state_error is not None:
            return 1, state_error

    text = message_path.read_text(encoding="utf-8")
    subject = _extract_subject(text)
    if not subject:
        return 1, "empty commit subject"
    configured_style = subject_style or _configured_subject_style(repo_root)
    match, is_bare_bracketed, err = _match_subject(
        subject,
        state_path,
        subject_style=configured_style,
        has_wave_trailer=_has_wave_trailer(text),
    )
    if match is None:
        return 1, err
    deprecation = _bracket_form_deprecation(
        subject,
        subject_style=configured_style,
        is_bare_bracketed=is_bare_bracketed,
    )
    release_annotation = _check_release_annotation(subject)
    if release_annotation is not None:
        return release_annotation
    commit_type = match.group("type")
    close_fold = _single_wave_close_rejection(subject, commit_type=commit_type)
    if close_fold is not None:
        return close_fold
    wave_scope = _check_wave_scope(
        subject=subject,
        text=text,
        commit_type=commit_type,
        staged=staged,
        managed_state=managed_state,
        state_path=state_path,
        repo_root=repo_root,
        canonical_state_path=canonical_state_path,
        env=resolved_env,
    )
    if wave_scope is not None:
        return wave_scope
    # Bare conventional-commits (no bracket prefix) has no path whitelist;
    # bracketed forms (wave + bare state/docs) route through the
    # scoped-path check, which internally gates on commit_type / is_bare
    # to apply the right whitelist.
    if subject.startswith("["):
        scoped = _check_scoped_paths(
            commit_type=match.group("type"),
            staged=staged,
            is_bare=is_bare_bracketed,
        )
        if scoped is not None:
            return scoped
    return _accept_or_reject(text, resolved_env, warning=deprecation)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: commit_prefix_lint.py <commit-msg-path>", file=sys.stderr)
        return 1
    message_path = Path(argv[1])
    if not message_path.exists():
        print(f"commit message file missing: {message_path}", file=sys.stderr)
        return 1
    repo_root = _find_repo_root()
    managed_state_path = repo_root / ".ea" / "state.json" if (repo_root / ".ea").is_dir() else None
    exit_code, diag = lint(
        message_path,
        _staged_paths(),
        env=os.environ,
        state_path=managed_state_path,
        repo_root=repo_root,
    )
    if diag:
        print(diag, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
