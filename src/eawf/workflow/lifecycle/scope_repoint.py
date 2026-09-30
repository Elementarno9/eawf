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

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import Wave
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
    "derive_commit_file_scopes",
    "frozen_record_fingerprint",
]
