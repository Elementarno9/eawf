"""EviBound evidence-reference resolver (Keystone-B root).

This module is the root of the EviBound evidence chain (later waves
layer the rung-1 deterministic gate, the rung-2 in-process NLI scorer,
and the rung-3 spawned jury on top of it). It exposes a single
dispatcher, :func:`resolve`, that routes an evidence reference to the
right *deterministic* check and reports a typed result.

Routing key — the landmine
---------------------------
:func:`resolve` routes on :data:`~eawf.kernel.spec.common.CriterionEvidenceKind`
(``deterministic | jury | attested``) — the enum that classifies *how* a
criterion's evidence is gathered. It does NOT route on
:data:`~eawf.kernel.spec.common.EvidenceKind`
(``audit | artifact | decision | store_record | external_url``), which
classifies *what* a reference points at. The two enums are deliberately
distinct (see ``common.py``): routing on ``EvidenceKind`` would
mis-dispatch a criterion's verification flavor onto the reference-target
vocabulary. Only the ``deterministic`` flavor has an automated check
this wave can run; ``jury`` and ``attested`` are deferred to later
rungs.

Reused seams (no logic duplicated)
----------------------------------
The four deterministic checks reuse the existing Citation / URN seams
rather than reimplementing them:

* portability — :meth:`Citation._ref_must_be_portable` (rejects absolute
  / ``file:`` / ``~/`` / local-host refs).
* dense ``[N]`` marker — :func:`citation_numbers_in_text` (the prose
  ``[N]`` scanner backing :func:`validate_dense_citation_refs`).
* URN grammar — :func:`eawf.kernel.state.urn.parse` (shape-only parse).
* disk-exists — ``(project_root / ref).is_file()`` (the same
  ``Path.is_file`` lookup :func:`eawf.kernel.spec.heuristics.missing_test_paths`
  performs against a project root).

Deferred (open question OQ-R)
-----------------------------
Two checks are explicitly NOT implemented this wave and are flagged on
the result rather than silently treated as passed:

* URN -> record *dereference* — confirming the parsed URN actually
  resolves to a live record in ``state.json`` / a store. :func:`resolve`
  validates URN *grammar* only.
* file:line anchor existence — confirming a ``path:line`` reference
  points at a line that exists in the file. :func:`resolve` treats such
  refs as plain repo-relative paths for the disk-exists check and flags
  the anchor check as deferred.

Both surface as :data:`DeferredAspect` members in
:attr:`ResolveResult.deferred_aspects`; the ``jury`` / ``attested``
criterion flavors surface as a whole-result
``status == ResolveStatus.DEFERRED``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from eawf.kernel.spec.common import CriterionEvidenceKind
from eawf.kernel.state.urn import parse as parse_urn
from eawf.platform.artifacts.references import (
    Citation,
    citation_numbers_in_text,
)

logger = logging.getLogger(__name__)


class ResolveStatus(StrEnum):
    """Terminal status of a :func:`resolve` call.

    ``RESOLVED`` means the selected deterministic check ran and the
    reference passed it (subject to any :attr:`ResolveResult.deferred_aspects`
    that this wave does not yet verify). ``UNRESOLVED`` means the check
    ran and the reference failed it. ``DEFERRED`` means no deterministic
    check applies at all this wave — the criterion's evidence flavor is
    ``jury`` or ``attested`` and is handled by a later EviBound rung.
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    DEFERRED = "deferred"


class ResolveCheck(StrEnum):
    """The deterministic check :func:`resolve` selected for a reference.

    ``NONE`` is used when the criterion flavor is non-deterministic and
    no check was selected (``status == ResolveStatus.DEFERRED``).
    """

    PORTABILITY = "portability"
    DENSE_MARKER = "dense_marker"
    DISK_EXISTS = "disk_exists"
    URN_GRAMMAR = "urn_grammar"
    NONE = "none"


class DeferredAspect(StrEnum):
    """An evidence check that is recognised but not implemented this wave.

    These are the open-question (OQ-R) follow-ons. They are attached to
    the result so a caller can see that a ``RESOLVED`` verdict is
    shape-only, not a full dereference.
    """

    #: The criterion flavor is ``jury`` — a spawned multi-reviewer vote.
    JURY_VOTE = "jury_vote"
    #: The criterion flavor is ``attested`` — a human operator sign-off.
    OPERATOR_ATTESTATION = "operator_attestation"
    #: A URN parsed (grammar OK) but was not dereferenced to a record.
    URN_RECORD_DEREFERENCE = "urn_record_dereference"
    #: A ``path:line`` ref had its path checked but not its line anchor.
    FILE_LINE_ANCHOR = "file_line_anchor"


@dataclass(frozen=True)
class ResolveResult:
    """Typed outcome of routing one evidence reference through :func:`resolve`.

    Attributes:
        ref: The evidence reference that was routed.
        evidence_kind: The :data:`CriterionEvidenceKind` the routing
            decision was made on — recorded so a caller can confirm the
            dispatcher routed on the criterion's flavor, not on a
            reference-target kind.
        status: One of :class:`ResolveStatus`.
        check: The deterministic check that ran, or
            :attr:`ResolveCheck.NONE` when none applied.
        reason: Short human-readable explanation of the outcome — the
            failure message for ``UNRESOLVED`` or the deferral reason for
            ``DEFERRED``. Empty for a clean ``RESOLVED``.
        deferred_aspects: Recognised-but-unimplemented checks (OQ-R).
            Non-empty on a ``DEFERRED`` whole-result and also on a
            ``RESOLVED`` URN / ``path:line`` ref whose dereference /
            anchor check is a follow-on.
    """

    ref: str
    evidence_kind: CriterionEvidenceKind
    status: ResolveStatus
    check: ResolveCheck
    reason: str = ""
    deferred_aspects: tuple[DeferredAspect, ...] = field(default_factory=tuple)


def _looks_like_urn(ref: str) -> bool:
    """Return True when *ref* uses the ``urn:eawf:`` scheme.

    A cheap prefix test routes the reference to the URN-grammar check;
    the authoritative grammar validation is :func:`parse_urn`, which
    runs only for refs that pass this gate.
    """
    return ref.startswith("urn:eawf:")


def _has_dense_marker(ref: str) -> bool:
    """Return True when *ref* contains at least one dense ``[N]`` marker.

    Reuses :func:`citation_numbers_in_text` so the marker grammar
    (``[1]``, ``[2]``, ... but not ``![1]`` image syntax) stays
    identical to the prose-citation validator.
    """
    return bool(citation_numbers_in_text(ref))


def _split_anchor(ref: str) -> tuple[str, bool]:
    """Split a possible ``path:line`` anchor off a repo-relative *ref*.

    Returns ``(path, has_anchor)`` where ``path`` is the portion before a
    trailing ``:<digits>`` anchor and ``has_anchor`` records whether one
    was present. The line anchor itself is NOT verified this wave (OQ-R);
    the caller flags it as a deferred aspect.
    """
    head, sep, tail = ref.rpartition(":")
    if sep and head and tail.isdigit():
        return head, True
    return ref, False


def _check_portability(ref: str) -> ResolveResult:
    """Route *ref* through the Citation portability validator.

    Reuses :meth:`Citation._ref_must_be_portable` so the absolute-path /
    ``file:`` / ``~/`` / local-host rejection logic is the single
    authoritative copy. The validator raises :class:`ValueError` on a
    non-portable ref; that is translated into an ``UNRESOLVED`` result
    rather than propagated, because resolution is a *check*, not a
    constructor.
    """
    try:
        Citation._ref_must_be_portable(ref)
    except ValueError as exc:
        return ResolveResult(
            ref=ref,
            evidence_kind="deterministic",
            status=ResolveStatus.UNRESOLVED,
            check=ResolveCheck.PORTABILITY,
            reason=str(exc),
        )
    return ResolveResult(
        ref=ref,
        evidence_kind="deterministic",
        status=ResolveStatus.RESOLVED,
        check=ResolveCheck.PORTABILITY,
    )


def _check_dense_marker(ref: str) -> ResolveResult:
    """Confirm *ref* carries at least one dense ``[N]`` citation marker."""
    numbers = citation_numbers_in_text(ref)
    if not numbers:
        return ResolveResult(
            ref=ref,
            evidence_kind="deterministic",
            status=ResolveStatus.UNRESOLVED,
            check=ResolveCheck.DENSE_MARKER,
            reason="no dense [N] citation marker found",
        )
    return ResolveResult(
        ref=ref,
        evidence_kind="deterministic",
        status=ResolveStatus.RESOLVED,
        check=ResolveCheck.DENSE_MARKER,
    )


def _check_urn_grammar(ref: str) -> ResolveResult:
    """Validate URN *grammar* only; dereference-to-record is deferred (OQ-R).

    Reuses :func:`eawf.kernel.state.urn.parse`, which raises
    :class:`ValueError` on a malformed or unknown-kind URN. A grammar
    pass attaches :attr:`DeferredAspect.URN_RECORD_DEREFERENCE` to the
    result so the caller knows the live record was NOT looked up.
    """
    try:
        parse_urn(ref)
    except ValueError as exc:
        return ResolveResult(
            ref=ref,
            evidence_kind="deterministic",
            status=ResolveStatus.UNRESOLVED,
            check=ResolveCheck.URN_GRAMMAR,
            reason=str(exc),
        )
    return ResolveResult(
        ref=ref,
        evidence_kind="deterministic",
        status=ResolveStatus.RESOLVED,
        check=ResolveCheck.URN_GRAMMAR,
        deferred_aspects=(DeferredAspect.URN_RECORD_DEREFERENCE,),
    )


def _check_disk_exists(ref: str, *, project_root: Path) -> ResolveResult:
    """Confirm *ref* resolves to an existing regular file under *project_root*.

    Strips an optional trailing ``:<line>`` anchor before the
    ``Path.is_file`` lookup (the same lookup
    :func:`eawf.kernel.spec.heuristics.missing_test_paths` performs). When an
    anchor was present, :attr:`DeferredAspect.FILE_LINE_ANCHOR` is
    attached because line-existence is not verified this wave (OQ-R).

    Args:
        ref: A repo-relative path, optionally with a ``:<line>`` anchor.
        project_root: Absolute path the ref is resolved against.
    """
    path, has_anchor = _split_anchor(ref)
    deferred: tuple[DeferredAspect, ...] = (DeferredAspect.FILE_LINE_ANCHOR,) if has_anchor else ()
    candidate = project_root / path
    if not candidate.is_file():
        return ResolveResult(
            ref=ref,
            evidence_kind="deterministic",
            status=ResolveStatus.UNRESOLVED,
            check=ResolveCheck.DISK_EXISTS,
            reason=f"no file at {path!r} under project root",
            deferred_aspects=deferred,
        )
    return ResolveResult(
        ref=ref,
        evidence_kind="deterministic",
        status=ResolveStatus.RESOLVED,
        check=ResolveCheck.DISK_EXISTS,
        deferred_aspects=deferred,
    )


def _resolve_deterministic(ref: str, *, project_root: Path) -> ResolveResult:
    """Select and run the deterministic check for *ref* by reference shape.

    Selection order:

    1. A non-portable ref fails fast on the portability check regardless
       of shape — an absolute path or ``file:`` URL is never valid
       evidence.
    2. A ``urn:eawf:`` ref -> URN-grammar check.
    3. A ref carrying a dense ``[N]`` marker -> dense-marker check.
    4. Otherwise the ref is a repo-relative path -> disk-exists check.

    Args:
        ref: The evidence reference (already known to be a
            ``deterministic`` criterion's evidence).
        project_root: Absolute path for the disk-exists branch.
    """
    portability = _check_portability(ref)
    if portability.status is ResolveStatus.UNRESOLVED:
        return portability
    if _looks_like_urn(ref):
        return _check_urn_grammar(ref)
    if _has_dense_marker(ref):
        return _check_dense_marker(ref)
    return _check_disk_exists(ref, project_root=project_root)


def resolve(
    ref: str,
    evidence_kind: CriterionEvidenceKind,
    *,
    project_root: Path,
) -> ResolveResult:
    """Route an evidence *ref* to its deterministic check by *evidence_kind*.

    The dispatcher routes on
    :data:`~eawf.kernel.spec.common.CriterionEvidenceKind` (the criterion's
    verification flavor), NOT on
    :data:`~eawf.kernel.spec.common.EvidenceKind` (the reference-target
    vocabulary). Only the ``deterministic`` flavor has an automated check
    this wave can run; ``jury`` and ``attested`` return a
    :attr:`ResolveStatus.DEFERRED` result that names the responsible
    later rung rather than silently passing.

    Within ``deterministic`` the concrete check (portability, dense
    ``[N]`` marker, URN grammar, disk-exists) is chosen by the shape of
    ``ref`` — see :func:`_resolve_deterministic`.

    Args:
        ref: The evidence reference string to resolve.
        evidence_kind: The criterion's :data:`CriterionEvidenceKind`.
        project_root: Absolute path the disk-exists check resolves
            repo-relative refs against.

    Returns:
        A :class:`ResolveResult`. ``RESOLVED`` / ``UNRESOLVED`` carry the
        check that ran; ``DEFERRED`` carries the open-question reason and
        the responsible :class:`DeferredAspect`.

    Raises:
        ValueError: When *ref* is empty / whitespace-only — an empty
            reference cannot be routed to any check.
    """
    if not ref.strip():
        raise ValueError("evidence ref must be non-empty")

    if evidence_kind == "deterministic":
        result = _resolve_deterministic(ref, project_root=project_root)
        logger.debug(
            f"resolve ref={ref!r} kind={evidence_kind} status={result.status} check={result.check}"
        )
        return result
    if evidence_kind == "jury":
        return ResolveResult(
            ref=ref,
            evidence_kind=evidence_kind,
            status=ResolveStatus.DEFERRED,
            check=ResolveCheck.NONE,
            reason="jury flavor resolves via the spawned-jury rung (deferred, OQ-R)",
            deferred_aspects=(DeferredAspect.JURY_VOTE,),
        )
    if evidence_kind == "attested":
        return ResolveResult(
            ref=ref,
            evidence_kind=evidence_kind,
            status=ResolveStatus.DEFERRED,
            check=ResolveCheck.NONE,
            reason="attested flavor resolves via operator sign-off (deferred, OQ-R)",
            deferred_aspects=(DeferredAspect.OPERATOR_ATTESTATION,),
        )
    assert_never(evidence_kind)


__all__ = [
    "DeferredAspect",
    "ResolveCheck",
    "ResolveResult",
    "ResolveStatus",
    "resolve",
]
