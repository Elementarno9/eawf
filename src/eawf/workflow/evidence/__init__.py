"""Evidence-area domain package: goals, outcomes, hypotheses, audits, decisions,
incidents, artifacts, backlog.

Each module exposes pure mutator functions that take an in-memory
:class:`~eawf.kernel.state.models.State` plus arguments and return a tuple of
``(updated_state, jsonl_record, event_record)`` so the CLI handler layer can
serialise them under the sibling lock without leaking I/O concerns into the
business logic. The audit-evidence invariant from
:mod:`eawf.kernel.validate.invariants` (``check_audit_evidence``) is the source of
truth for verdict-bearing rules and is invoked at write time via
:func:`require_complete_audit`.
"""

from __future__ import annotations

from eawf.workflow.evidence.evibound import (
    BriefPromotionGate,
    BriefRefOutcome,
    check_brief_promotable,
    run_rung1_gate,
)
from eawf.workflow.evidence.guards import require_complete_audit
from eawf.workflow.evidence.resolve import (
    DeferredAspect,
    ResolveCheck,
    ResolveResult,
    ResolveStatus,
    resolve,
)
from eawf.workflow.evidence.rung2 import (
    ClaimRung,
    EntailmentScorer,
    LexicalEntailmentScorer,
    Rung2ClaimResult,
    Rung2Config,
    Rung2Verdict,
    classify_probability,
    load_default_scorer,
    looks_numeric,
    route_claim_to_rung,
    run_rung2_gate,
    score_claim,
    score_claims,
    verdict_to_status,
)

__all__ = [
    "BriefPromotionGate",
    "BriefRefOutcome",
    "ClaimRung",
    "DeferredAspect",
    "EntailmentScorer",
    "LexicalEntailmentScorer",
    "ResolveCheck",
    "ResolveResult",
    "ResolveStatus",
    "Rung2ClaimResult",
    "Rung2Config",
    "Rung2Verdict",
    "check_brief_promotable",
    "classify_probability",
    "load_default_scorer",
    "looks_numeric",
    "require_complete_audit",
    "resolve",
    "route_claim_to_rung",
    "run_rung1_gate",
    "run_rung2_gate",
    "score_claim",
    "score_claims",
    "verdict_to_status",
]
