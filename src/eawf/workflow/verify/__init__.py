"""Verify spine — readiness, compile-gate, and waiver evaluation surfaces.

The v0.4 verify spine has four concerns:

* :func:`~eawf.workflow.verify.readiness.compute` — derived readiness view
  for a scope (wave / iter / phase), computed from typed
  :class:`~eawf.kernel.spec.common.CriterionSpec` /
  :class:`~eawf.kernel.spec.common.GateSpec` definitions, the legacy
  :attr:`~eawf.kernel.state.models.Wave.success_criteria` list, and the
  SHA-bound :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows.
* :func:`~eawf.workflow.verify.compile.compile_gate` (W08) — translates a
  typed :class:`~eawf.kernel.spec.common.GateSpec` into the
  :class:`~eawf.workflow.audit_dsl.models.CheckSpec` shape the W15-hardened
  gate runner executes. v0.4.0 compiles only
  ``evidence_kind="deterministic"`` gates; ``"jury"`` + ``"attested"``
  return ``None`` and defer to v0.4.1+.
* Waivers (W11) — operator-attested gate overrides honoured by the
  readiness compute via SHA-bound freshness on
  :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows.
* :func:`~eawf.workflow.verify.dispatch_close.verify_close_readiness`
  (W57) — deterministic post-execution gate the daemon dispatch
  runner consults after a subagent returns. The gate inspects only
  the typed :class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`
  and refuses a close path on a failing verdict / blank summary /
  wave-id mismatch.

This package owns the typed view models (:mod:`models`), the read-only
``compute`` function (:mod:`readiness`), the compile-gate seam
(:mod:`compile`), and the dispatch-close gate (:mod:`dispatch_close`).
The three wave-close seams (``_close_and_pin``,
``_compute_wave_close_readiness``, ``wave_land``) attach ``compute`` as
an **advisory** call by default: warnings surface in operator output +
envelope extras, but no close path blocks on a non-ready readiness. A
profile that sets ``verify.enforce: true`` flips the advisory to
gating: ``compute`` then raises
:class:`~eawf.workflow.lifecycle._errors.LifecycleError` on a non-ready
result via :func:`~eawf.workflow.verify.readiness._enforce_readiness`,
and the close seams reject the mutation. No shipped profile enables
``enforce`` — the bit defaults ``False`` so existing repos keep the
advisory close behaviour until they opt in.
"""

from __future__ import annotations

from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.dispatch_close import (
    DispatchCloseBlockedError,
    VerifyResult,
    verify_close_readiness,
)
from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)
from eawf.workflow.verify.readiness import compute, load_active_verify_block

__all__ = [
    "CloseReadiness",
    "CriterionView",
    "DispatchCloseBlockedError",
    "GateResult",
    "VerifyResult",
    "compile_gate",
    "compute",
    "load_active_verify_block",
    "verify_close_readiness",
]
