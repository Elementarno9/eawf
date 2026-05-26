"""Derived view models for the v0.4 verify spine.

The models here are **read-only**: ``readiness.compute`` returns a
:class:`CloseReadiness` instance, but nothing in this package persists
the view. Persistence happens at the EvidenceRecord layer; the readiness
view is a deterministic projection.

Three strict Pydantic v2 models:

* :class:`GateResult` — one gate's per-cadence outcome.
* :class:`CriterionView` — one criterion plus its gate results.
* :class:`CloseReadiness` — the rolled-up scope-wide view, including
  whether the scope is ``ready`` to close.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.models import IdStr

#: Closed status literal for one gate execution.
#:
#: * ``pass`` — the gate fired and reported success.
#: * ``fail`` — the gate fired and reported failure.
#: * ``blocked`` — the gate could not execute (missing input, env error).
GateStatus = Literal["pass", "fail", "blocked"]


#: Closed status literal for one criterion's rolled-up outcome.
#:
#: * ``pass`` — every required gate (or, in legacy mode, the criterion
#:   itself) reports pass.
#: * ``fail`` — at least one required gate failed.
#: * ``blocked`` — at least one required gate is blocked (and none failed).
#: * ``pending`` — no evidence exists yet; the criterion is unverified.
#: * ``waived`` — an operator waiver clears the criterion.
CriterionStatus = Literal["pass", "fail", "blocked", "pending", "waived"]


#: Source of the criterion view — typed spec or the legacy string list.
#:
#: ``legacy`` views carry no gate results because the legacy
#: :attr:`~eawf.kernel.state.models.Wave.success_criteria` field is a
#: free-form ``list[str]``; ``spec`` views carry the criterion's typed
#: gate results.
CriterionSource = Literal["legacy", "spec"]


class GateResult(_StrictModel):
    """One gate's per-cadence outcome inside a :class:`CriterionView`.

    Attributes:
        gate_id: Stable id of the gate (mirrors
            :attr:`eawf.kernel.spec.common.GateSpec.id`).
        status: Closed status literal — see :data:`GateStatus`.
    """

    gate_id: IdStr
    status: GateStatus


class CriterionView(_StrictModel):
    """One criterion's rolled-up view, plus per-gate detail when typed.

    Attributes:
        id: Stable criterion id. For ``source="spec"`` views this is
            the :class:`~eawf.kernel.spec.common.CriterionSpec.id`; for
            ``source="legacy"`` views the id is a synthesised stable
            slug ``CR-<index>`` so the view shape stays uniform.
        source: Whether the view was synthesised from the typed spec
            layer or the legacy string list.
        status: Rolled-up criterion status — see
            :data:`CriterionStatus`.
        gate_results: Per-gate outcomes for ``source="spec"`` views;
            ``None`` for legacy views (no gates exist on the string
            list).
    """

    id: IdStr
    source: CriterionSource
    status: CriterionStatus
    gate_results: list[GateResult] | None = None


class CloseReadiness(_StrictModel):
    """Derived close-readiness view for a scope (wave / iter / phase).

    Produced by :func:`eawf.workflow.verify.readiness.compute`. The view
    is purely derived from inputs and is never persisted — recomputing
    on the same ``(state, store_dir, repo_root)`` tuple yields an equal
    instance.

    Attributes:
        ready: ``True`` iff every required criterion has a status in
            ``{pass, waived}`` AND no blocking gate result fired. Note
            the wave-level advisory contract (W06): a non-ready value
            is **logged + counted** but never blocks the close path.
            W19 (later wave) flips the gating behaviour behind
            ``profile.verify.enforce``.
        criteria: One :class:`CriterionView` per criterion attached to
            the scope. Includes both typed-spec criteria and legacy
            string criteria when both exist (mixed-mode waves are
            supported during the v0.4.x migration window).
        warnings: Free-form advisory strings — one per non-blocking
            anomaly the compute step surfaced (legacy criteria, missing
            evidence, etc.). Renderers and the daemon close envelope
            tally ``len(warnings)`` as the rolled-up advisory metric.
        waived_gate_ids: Gate ids whose ``GateResult.status`` was set
            to ``pass`` because a waiver cleared them. Empty until the
            W11 waiver subsystem lands; reserved here so the model
            shape stabilises now and the W11 transition is additive.
    """

    ready: bool
    criteria: list[CriterionView]
    warnings: list[str] = Field(default_factory=list)
    waived_gate_ids: list[IdStr] = Field(default_factory=list)


__all__ = [
    "CloseReadiness",
    "CriterionSource",
    "CriterionStatus",
    "CriterionView",
    "GateResult",
    "GateStatus",
]
