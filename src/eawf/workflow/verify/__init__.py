"""Verify spine — readiness, compile-gate, and waiver evaluation surfaces.

The v0.4 verify spine has four concerns:

* :func:`~eawf.workflow.verify.readiness.compute` — derived readiness view
  for a scope (wave / iter / phase), computed from typed
  :class:`~eawf.kernel.spec.common.CriterionSpec` /
  :class:`~eawf.kernel.spec.common.GateSpec` definitions, the legacy
  :attr:`~eawf.kernel.state.models.Wave.success_criteria` list, and the
  SHA-bound :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows.
* :func:`~eawf.workflow.verify.compile.compile_gate` — translates a
  typed :class:`~eawf.kernel.spec.common.GateSpec` into the
  :class:`~eawf.workflow.audit_dsl.models.CheckSpec` shape the W15-hardened
  gate runner executes. v0.4.0 compiles only
  ``evidence_kind="deterministic"`` gates; ``"jury"`` + ``"attested"``
  return ``None`` and defer to v0.4.1+.
* :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
  — the one execution seam every readiness check passes through. It runs
  the compiled checks in a child interpreter pinned to a throwaway
  sandbox, so a floor check that drives eawf's own RPCs cannot reach the
  caller's live runtime directory or live ledger.
* Waivers — operator-attested gate overrides honoured by the
  readiness compute via SHA-bound freshness on
  :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows.

This package owns the typed view models (:mod:`models`), the read-only
``compute`` function (:mod:`readiness`), the compile-gate seam
(:mod:`compile`), and the out-of-process check runner
(:mod:`sandboxed_checks`).
A profile that sets ``verify.enforce: true`` makes ``compute`` raise
:class:`~eawf.kernel.errors.lifecycle.LifecycleError` on a non-ready
result via :func:`~eawf.workflow.verify.readiness._enforce_readiness`.
No shipped profile enables ``enforce``; the bit defaults ``False``.
"""

from __future__ import annotations

from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.hosted_close import (
    count_scope_waivers,
)
from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)
from eawf.workflow.verify.readiness import compute, load_active_verify_block
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process

__all__ = [
    "CloseReadiness",
    "CriterionView",
    "GateResult",
    "compile_gate",
    "compute",
    "count_scope_waivers",
    "load_active_verify_block",
    "run_checks_out_of_process",
]
