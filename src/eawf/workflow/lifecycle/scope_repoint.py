"""Bounded file-scope and criterion-text repoint for a recorded wave row.

Sibling of :mod:`eawf.workflow.lifecycle.gate_repoint`, which repairs the
recorded gate argv of a closed wave under a frozen-fingerprint guard.
This module repairs the other two fields that go wrong after the fact:

* ``file_scopes`` -- what a wave claimed it would touch. When the pinned
  commit touched a different set, the recorded scope contradicts the
  commit, and every later reader (audit, review, scope-agreement gate)
  reasons from the wrong list. The repair is not free-text: the new
  scopes are DERIVED from ``git show --name-only`` of the wave's own
  pinned commit, minus the ``.ea/`` state paths, so the record can only
  move toward what the commit actually did.
* the ``text`` of a named success criterion -- prose that names the
  wrong symbol, or prose the report scrub refuses to echo, leaves the
  criterion unusable while the verdict it carries is sound.

``spec sync`` refuses a non-PENDING wave and there is no wave-level
reopen, so without this verb both fields stay wrong forever.

The bound is the mechanism, not a comment. The mutation is applied and
then checked against a fingerprint of the whole wave row with only the
repointable fields elided (:func:`frozen_record_fingerprint`): the
``file_scopes`` list when a scope repoint was requested, and the ``text``
of exactly the criteria the caller named. Anything else that moves --
the status, the outcome, ``closed_at``, the commit pin, the gates, an
unnamed criterion's text, or any non-text field of a named criterion --
rolls the whole mutation back and refuses it. A criterion-text repoint
additionally requires a non-empty reason, which the caller records on the
event row, and the replacement text is re-validated through
:class:`~eawf.kernel.spec.common.CriterionSpec` plus the EAWF021
measurability rule, so an unmeasurable rewrite cannot enter the record.

Status bound: a file-scope repoint reads a pinned commit, which only a
CLOSED wave carries, so that leg is CLOSED-only. A criterion-text
repoint is also reachable on a CLAIMED / IN_PROGRESS wave, because the
text that blocks a close is discovered exactly then -- at close time,
when the wave can be neither synced (``spec sync`` is PENDING-only) nor
closed. A PENDING wave is plan-time scope and belongs to ``spec sync``
either way.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.platform.lint.eawf021_measurable_criterion import check_criterion
from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)

#: Stand-in written over ``file_scopes`` when the frozen fingerprint is
#: computed for a scope repoint. Any real list would make the fingerprint
#: sensitive to the one field that leg is allowed to move.
ELIDED_SCOPES: Final[str] = "<repointable-file-scopes>"

#: Stand-in written over the ``text`` of each criterion the caller named.
#: Criteria the caller did NOT name keep their real text in the
#: fingerprint, so an unnamed rewrite is refused.
ELIDED_TEXT: Final[str] = "<repointable-criterion-text>"

#: Repo-relative prefix of the state tree. The daemon rewrites it on
#: every close, so it says nothing about what a wave's code changed and
#: never belongs in a derived scope list.
STATE_PATH_PREFIX: Final[str] = ".ea/"

#: Wave statuses whose ``file_scopes`` this verb may re-derive. Only a
#: closed wave carries the pinned commit the derivation reads.
SCOPE_REPOINT_STATUSES: Final[frozenset[WaveStatus]] = frozenset({WaveStatus.CLOSED})

#: Wave statuses whose criterion text this verb may rewrite. The in-flight
#: statuses are included because a criterion that blocks its own close is
#: found at close time, when the wave is past ``spec sync`` and not yet
#: closed.
TEXT_REPOINT_STATUSES: Final[frozenset[WaveStatus]] = frozenset(
    {WaveStatus.CLOSED, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
)

_GIT_TIMEOUT_SECONDS: Final[float] = 5.0


class CriterionTextRepoint(BaseModel):
    """One requested text rewrite against a single recorded criterion.

    Attributes:
        criterion_id: Id of a criterion row already recorded on the wave.
            The repoint never creates a criterion, so an unknown id is
            refused.
        text: Replacement prose, re-validated through
            :class:`~eawf.kernel.spec.common.CriterionSpec` and EAWF021
            before it can reach state.
    """

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=500)


class CriterionTextChange(BaseModel):
    """The applied before/after text pair for one repointed criterion."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    before_text: str
    after_text: str


class ScopeRepointReport(BaseModel):
    """What a repoint actually moved, for the operator and the audit event.

    Attributes:
        wave_id: The wave whose record was repointed.
        scopes_before: ``file_scopes`` as recorded before the repoint.
        scopes_after: ``file_scopes`` as recorded after it; equal to
            *scopes_before* when no scope repoint was requested or the
            derivation reproduced the recorded list.
        changed_criteria: One row per criterion whose text actually
            differs.
        unchanged_criterion_ids: Criterion ids that survived the pass
            untouched, so a replayed repoint reads as a no-op rather than
            as a silent partial application.
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    scopes_before: list[str] = Field(default_factory=list)
    scopes_after: list[str] = Field(default_factory=list)
    changed_criteria: list[CriterionTextChange] = Field(default_factory=list)
    unchanged_criterion_ids: list[str] = Field(default_factory=list)

    @property
    def scopes_changed(self) -> bool:
        """Whether the recorded ``file_scopes`` list actually moved."""
        return self.scopes_before != self.scopes_after


def frozen_record_fingerprint(
    wave: Wave,
    *,
    elide_scopes: bool,
    elided_criterion_ids: frozenset[str],
) -> dict[str, Any]:
    """Return everything about *wave* that a repoint must leave equal.

    The fingerprint is the wave row's full JSON dump with the requested
    degrees of freedom blanked: ``file_scopes`` when *elide_scopes* is
    set, and the ``text`` of every criterion named in
    *elided_criterion_ids*. Everything else -- status, outcome,
    ``closed_at``, the commit pin, the gates, the sessions, every
    non-text criterion field, and the text of every criterion the caller
    did not name -- still rides the fingerprint.

    Args:
        wave: The wave row to fingerprint.
        elide_scopes: Whether the ``file_scopes`` list is a permitted
            degree of freedom for this repoint.
        elided_criterion_ids: Ids of the criteria whose text is a
            permitted degree of freedom for this repoint.

    Returns:
        A JSON-safe dict comparable with ``==`` across a mutation.
    """
    payload: dict[str, Any] = wave.model_dump(mode="json")
    if elide_scopes:
        payload["file_scopes"] = ELIDED_SCOPES
    for criterion in payload.get("success_criteria", []):
        if criterion.get("id") in elided_criterion_ids:
            criterion["text"] = ELIDED_TEXT
    return payload


def derive_commit_file_scopes(repo_root: Path, commit: str) -> list[str]:
    """Return the non-state paths *commit* touched, sorted and deduplicated.

    The derivation is what keeps a scope repoint honest: the operator
    names no paths, so the record can only move toward what the pinned
    commit actually changed. ``.ea/`` paths are dropped because the
    daemon rewrites the state tree on every close, which says nothing
    about the wave's own subject.

    Args:
        repo_root: Repository working directory the commit is read from.
        commit: The wave's pinned commit-ish.

    Returns:
        Repo-relative paths in sorted order.

    Raises:
        LifecycleError: When git is not on PATH, the call fails or times
            out (an unreachable or unknown commit), or the commit touched
            no path outside the state tree.
    """
    if shutil.which("git") is None:
        raise LifecycleError("cannot derive file scopes: git is not on PATH")
    cmd = ["git", "show", "--name-only", "--format=", commit]
    try:
        out: subprocess.CompletedProcess[str] = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise LifecycleError(f"cannot derive file scopes: git show {commit!r} timed out") from exc
    except OSError as exc:
        raise LifecycleError(
            f"cannot derive file scopes: git show {commit!r} failed: {exc}"
        ) from exc
    if out.returncode != 0:
        raise LifecycleError(
            f"cannot derive file scopes: git show {commit!r} exited {out.returncode} "
            f"({out.stderr.strip()!r}); the pinned commit is unreachable from {str(repo_root)!r}"
        )
    paths = {
        line.strip()
        for line in out.stdout.splitlines()
        if line.strip() and not line.strip().startswith(STATE_PATH_PREFIX)
    }
    if not paths:
        raise LifecycleError(
            f"commit {commit!r} touches no path outside {STATE_PATH_PREFIX!r}; "
            "a repoint never clears the recorded file scopes"
        )
    return sorted(paths)


def build_criterion_text_repoint(
    wave: Wave,
    repoints: list[CriterionTextRepoint],
) -> list[CriterionSpec]:
    """Build the candidate criteria list that applies *repoints* to *wave*.

    Every recorded criterion is carried over; the named ones get their
    ``text`` replaced. Candidate rows are rebuilt through
    ``CriterionSpec.model_validate`` rather than ``model_copy`` so the
    model validators fire at this parse seam -- ``model_copy`` skips
    them, which would let an over-long or over-broad rewrite through --
    and the replacement text is additionally run past EAWF021 so an
    unmeasurable rewrite is refused.

    Only the replacement ``text`` is measured, not the recorded
    ``measurable_signal``: this verb moves the text, and refusing a text
    repair over a pre-existing finding in a field it may not touch would
    leave the record unrepairable.

    Args:
        wave: The wave whose recorded criteria are the base of the rewrite.
        repoints: Requested per-criterion text rewrites.

    Returns:
        The candidate criteria list, in the wave's recorded order.

    Raises:
        LifecycleError: When *repoints* is empty, names a criterion id
            twice, names a criterion the wave does not record, supplies a
            text the criterion model rejects, or supplies a text EAWF021
            finds unmeasurable.
    """
    if not repoints:
        raise LifecycleError("no criterion text repoints supplied")
    requested: dict[str, str] = {}
    for repoint in repoints:
        if repoint.criterion_id in requested:
            raise LifecycleError(
                f"duplicate criterion repoint for criterion {repoint.criterion_id!r}"
            )
        requested[repoint.criterion_id] = repoint.text
    recorded = {criterion.id for criterion in wave.success_criteria}
    unknown = sorted(criterion_id for criterion_id in requested if criterion_id not in recorded)
    if unknown:
        raise LifecycleError(
            f"wave {wave.id!r} records no criterion/criteria {unknown}; "
            "a repoint never creates a criterion"
        )

    candidates: list[CriterionSpec] = []
    for criterion in wave.success_criteria:
        text = requested.get(criterion.id)
        if text is None:
            candidates.append(criterion)
            continue
        violations = check_criterion(text, response=criterion.response)
        if violations:
            reasons = "; ".join(violation.reason for violation in violations)
            raise LifecycleError(
                f"criterion {criterion.id!r} repoint text rejected by EAWF021: {reasons}"
            )
        payload = criterion.model_dump(mode="json")
        payload["text"] = text
        try:
            candidates.append(CriterionSpec.model_validate(payload))
        except ValidationError as exc:
            raise LifecycleError(
                f"criterion {criterion.id!r} repoint text rejected: {exc}"
            ) from exc
    return candidates


def repoint_closed_wave_record(
    state: State,
    *,
    wave_id: str,
    scope_source: Path | None = None,
    criterion_texts: list[CriterionTextRepoint] | None = None,
    reason: str | None = None,
) -> ScopeRepointReport:
    """Rewrite a wave's file scopes and named criterion text, and only those.

    Mutates *state* in place. The wave is never reopened and its status
    never moves; the whole row is fingerprinted before and after
    (:func:`frozen_record_fingerprint`) and the mutation is rolled back
    and refused when anything outside the requested degrees of freedom
    moved. That covers the recorded verdict, the commit pin, the gates,
    every non-text criterion field and every criterion the caller did not
    name, so this verb cannot be used to rewrite a verdict under cover of
    a scope or prose fix.

    Args:
        state: State to mutate in place.
        wave_id: Canonical id of the wave to repoint.
        scope_source: Repository working directory to derive
            ``file_scopes`` from, using the wave's pinned commit.
            ``None`` leaves the recorded scopes untouched.
        criterion_texts: Per-criterion text rewrites. ``None`` or empty
            leaves every recorded criterion untouched.
        reason: Why the text rewrite is warranted. Required (non-blank)
            for a criterion-text repoint; the caller records it on the
            event row.

    Returns:
        A :class:`ScopeRepointReport` naming what moved.

    Raises:
        LifecycleError: When *wave_id* is unknown, the request asks for
            nothing, the wave's status does not permit the requested leg,
            a criterion-text repoint carries no reason, a named criterion
            id is unknown, the wave carries no pinned commit, the
            derivation fails, or the mutation would change anything other
            than the requested fields.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    requests = list(criterion_texts or [])
    derived = _authorise_repoint(
        wave,
        scope_source=scope_source,
        requests=requests,
        reason=reason,
    )
    candidates = build_criterion_text_repoint(wave, requests) if requests else None

    elided_ids = frozenset(repoint.criterion_id for repoint in requests)
    before = frozen_record_fingerprint(
        wave,
        elide_scopes=derived is not None,
        elided_criterion_ids=elided_ids,
    )
    scopes_before = list(wave.file_scopes)
    texts_before = {criterion.id: criterion.text for criterion in wave.success_criteria}
    original_criteria = list(wave.success_criteria)
    if derived is not None:
        wave.file_scopes = derived
    if candidates is not None:
        wave.success_criteria = candidates
    after = frozen_record_fingerprint(
        wave,
        elide_scopes=derived is not None,
        elided_criterion_ids=elided_ids,
    )
    if after != before:
        wave.file_scopes = scopes_before
        wave.success_criteria = original_criteria
        raise LifecycleError(
            f"wave {wave_id!r} repoint refused: it changes more than the file scopes and "
            f"the named criterion text ({_describe_drift(before, after)})"
        )

    changed, unchanged = _text_changes(wave, texts_before)
    report = ScopeRepointReport(
        wave_id=wave_id,
        scopes_before=scopes_before,
        scopes_after=list(wave.file_scopes),
        changed_criteria=changed,
        unchanged_criterion_ids=unchanged,
    )
    logger.info(
        f"repoint_closed_wave_record wave={wave_id} scopes_changed={report.scopes_changed} "
        f"texts_changed={len(changed)} unchanged={len(unchanged)}"
    )
    return report


def _authorise_repoint(
    wave: Wave,
    *,
    scope_source: Path | None,
    requests: list[CriterionTextRepoint],
    reason: str | None,
) -> list[str] | None:
    """Check a repoint request's preconditions and derive the new file scopes.

    Every refusal that does not need the mutation applied lives here, so
    the applier is left with the fingerprint guard alone.

    Args:
        wave: The wave the request targets.
        scope_source: Repository working directory for the derivation, or
            ``None`` when no scope repoint was requested.
        requests: The criterion-text rewrites, possibly empty.
        reason: The operator's reason for a text rewrite.

    Returns:
        The derived file scopes, or ``None`` when no scope repoint was
        requested.

    Raises:
        LifecycleError: When the request asks for nothing, the wave's
            status does not permit a requested leg, a text repoint
            carries no reason, the wave records no pinned commit, or the
            derivation fails.
    """
    if scope_source is None and not requests:
        raise LifecycleError(
            f"wave {wave.id!r} repoint requests nothing: name a scope source or a criterion text"
        )
    if requests:
        _require_status(wave, allowed=TEXT_REPOINT_STATUSES, leg="criterion-text")
        if reason is None or not reason.strip():
            raise LifecycleError(
                f"wave {wave.id!r} criterion-text repoint requires a non-empty reason"
            )
    if scope_source is None:
        return None
    _require_status(wave, allowed=SCOPE_REPOINT_STATUSES, leg="file-scope")
    if wave.commit is None:
        raise LifecycleError(
            f"wave {wave.id!r} records no pinned commit; "
            "a file-scope repoint derives the scopes from it"
        )
    return derive_commit_file_scopes(scope_source, wave.commit)


def _text_changes(
    wave: Wave,
    texts_before: dict[str, str],
) -> tuple[list[CriterionTextChange], list[str]]:
    """Split the wave's criteria into the texts that moved and those that did not.

    Args:
        wave: The wave as it stands after the mutation.
        texts_before: Criterion id to recorded text, captured before it.

    Returns:
        The applied change rows and the ids whose text is unchanged, both
        in the wave's recorded criterion order.
    """
    changed: list[CriterionTextChange] = []
    unchanged: list[str] = []
    for criterion in wave.success_criteria:
        before_text = texts_before[criterion.id]
        if criterion.text == before_text:
            unchanged.append(criterion.id)
            continue
        changed.append(
            CriterionTextChange(
                criterion_id=criterion.id,
                before_text=before_text,
                after_text=criterion.text,
            )
        )
    return changed, unchanged


def _require_status(wave: Wave, *, allowed: frozenset[WaveStatus], leg: str) -> None:
    """Refuse *wave* when its status does not permit the requested repoint leg.

    Args:
        wave: The wave whose status is checked.
        allowed: The statuses that permit this leg.
        leg: Human name of the leg, for the refusal message.

    Raises:
        LifecycleError: When the wave's status is outside *allowed*.
    """
    if wave.status in allowed:
        return
    permitted = ", ".join(sorted(status.value for status in allowed))
    raise LifecycleError(
        f"wave {wave.id!r} is {wave.status.value!r}; a {leg} repoint needs one of "
        f"[{permitted}]. A plan-time edit goes through `eawf spec sync`"
    )


def _describe_drift(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Name the fingerprint fields that moved, for the refusal message.

    Args:
        before: Frozen fingerprint captured before the assignment.
        after: Frozen fingerprint captured after it.

    Returns:
        A comma-joined list of the differing top-level wave fields, so
        triage reads the reason off the error without a re-run.
    """
    keys = set(before) | set(after)
    drifted = sorted(key for key in keys if before.get(key) != after.get(key))
    return "changed fields: " + ", ".join(drifted)


__all__ = [
    "ELIDED_SCOPES",
    "ELIDED_TEXT",
    "SCOPE_REPOINT_STATUSES",
    "STATE_PATH_PREFIX",
    "TEXT_REPOINT_STATUSES",
    "CriterionTextChange",
    "CriterionTextRepoint",
    "ScopeRepointReport",
    "build_criterion_text_repoint",
    "derive_commit_file_scopes",
    "frozen_record_fingerprint",
    "repoint_closed_wave_record",
]
