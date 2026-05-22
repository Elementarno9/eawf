"""Hypothesis CLI mutators: define / verdict / list.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from eawf.cli.errors import InvalidInput, NotFound
from eawf.evidence import _io
from eawf.evidence.guards import require_complete_audit
from eawf.state.enums import HypothesisStatus, HypothesisVerdict
from eawf.state.models import Hypothesis, State
from eawf.store.envelope import Envelope

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
        raise InvalidInput(f"hypothesis {hypothesis_id!r} already exists")

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
    ``inconclusive``. Audit-evidence guard fires before mutation.
    """
    hypotheses: dict[str, Hypothesis] = dict(state.hypotheses or {})
    if hypothesis_id not in hypotheses:
        raise NotFound(f"hypothesis {hypothesis_id!r} not found")

    require_complete_audit(state, audit_id)

    status_for_verdict = {
        HypothesisVerdict.CONFIRMED: HypothesisStatus.CONFIRMED,
        HypothesisVerdict.REJECTED: HypothesisStatus.REJECTED,
        HypothesisVerdict.INCONCLUSIVE: HypothesisStatus.INCONCLUSIVE,
    }[verdict]

    now = datetime.now(UTC)
    prior = hypotheses[hypothesis_id]
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
