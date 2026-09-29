"""Hypothesis CLI mutators: define / verdict / list.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.surfaces.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.kernel.state.enums import HypothesisStatus, HypothesisVerdict
from eawf.kernel.state.models import Hypothesis, State
from eawf.kernel.store.envelope import Envelope
from eawf.surfaces.cli.errors import UserError, ValidationError
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.guards import require_complete_audit

logger = logging.getLogger(__name__)


def define_hypothesis(
    state: State,
    *,
    hypothesis_id: str,
    scope_id: str,
    text: str,
    metric: str,
    confirm: str,
    reject: str,
    source_artifact_id: str | None = None,
) -> Envelope:
    """Create a pending :class:`Hypothesis` record in place."""
    hypotheses: dict[str, Hypothesis] = dict(state.hypotheses or {})
    if hypothesis_id in hypotheses:
        raise UserError(f"hypothesis {hypothesis_id!r} already exists", kind="InvalidInput")

    now = datetime.now(UTC)
    hyp = Hypothesis(
        id=hypothesis_id,
        scope_id=scope_id,
        title=text,
        metric=metric,
        confirm=confirm,
        reject=reject,
        status=HypothesisStatus.PENDING,
        verdict=None,
        audit_id=None,
        source_artifact_id=source_artifact_id,
        defined_at=now,
    )
    hypotheses[hypothesis_id] = hyp
    state.hypotheses = hypotheses
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-hyp-define-{hypothesis_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="hypothesis.define",
        actor="cli",
        command="hypothesis define",
        args={
            "hypothesis_id": hypothesis_id,
            "metric": metric,
            "confirm": confirm,
            "reject": reject,
        },
        summary=f"hypothesis {hypothesis_id} defined ({metric})",
    )


def set_verdict(
    state: State,
    *,
    hypothesis_id: str,
    verdict: HypothesisVerdict,
    audit_id: str,
) -> Envelope:
    """Record a hypothesis verdict in place.

    Status follows the verdict: ``confirmed`` / ``rejected`` /
    ``inconclusive``. Audit-evidence guard fires before mutation. A
    verdict is immutable: a hypothesis already verdicted is refused, and
    changed evidence is tested by defining a new hypothesis, so the prior
    row keeps what its audit found.

    Raises:
        UserError: ``kind="NotFound"`` for an unknown hypothesis;
            ``kind="InvalidInput"`` when it already carries a verdict.
        ValidationError: When *audit_id* is absent, unknown, incomplete, or
            completed before the hypothesis was stated (a stale audit).
    """
    hypotheses: dict[str, Hypothesis] = dict(state.hypotheses or {})
    if hypothesis_id not in hypotheses:
        raise UserError(f"hypothesis {hypothesis_id!r} not found", kind="NotFound")
    prior = hypotheses[hypothesis_id]
    if prior.verdict is not None:
        raise UserError(
            f"hypothesis {hypothesis_id!r} already carries the verdict "
            f"{prior.verdict.value!r}; a verdict is immutable, so re-test by defining "
            "a new hypothesis",
            kind="InvalidInput",
        )

    require_complete_audit(state, audit_id)
    audit = (state.audits or {})[audit_id]
    if prior.defined_at is not None and audit.created_at < prior.defined_at:
        raise ValidationError(
            f"audit-evidence: audit {audit_id!r} predates hypothesis {hypothesis_id!r} "
            "and cannot have evaluated it (INV.AUDIT.STALE)"
        )

    status_for_verdict = {
        HypothesisVerdict.CONFIRMED: HypothesisStatus.CONFIRMED,
        HypothesisVerdict.REJECTED: HypothesisStatus.REJECTED,
        HypothesisVerdict.INCONCLUSIVE: HypothesisStatus.INCONCLUSIVE,
    }[verdict]

    now = datetime.now(UTC)
    updated = prior.model_copy(
        update={
            "verdict": verdict,
            "status": status_for_verdict,
            "audit_id": audit_id,
        }
    )
    hypotheses[hypothesis_id] = updated
    state.hypotheses = hypotheses
    state.updated_at = now

    return _io.event_envelope(
        event_id=f"EVT-hyp-verdict-{hypothesis_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="hypothesis.verdict",
        actor="cli",
        command="hypothesis verdict",
        args={
            "hypothesis_id": hypothesis_id,
            "verdict": verdict.value,
            "audit_id": audit_id,
        },
        summary=f"hypothesis {hypothesis_id} verdict={verdict.value}",
    )


def list_hypotheses(
    state: State,
    *,
    scope_id: str | None = None,
    status: HypothesisStatus | None = None,
) -> list[Hypothesis]:
    """Return hypotheses filtered by *scope_id* / *status*."""
    out: list[Hypothesis] = []
    for hyp in (state.hypotheses or {}).values():
        if scope_id is not None and hyp.scope_id != scope_id:
            continue
        if status is not None and hyp.status != status:
            continue
        out.append(hyp)
    out.sort(key=lambda h: h.id)
    return out
