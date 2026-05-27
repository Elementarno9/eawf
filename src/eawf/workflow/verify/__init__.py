"""Verify spine — readiness, compile-gate, and waiver evaluation surfaces.

The v0.4 verify spine has three concerns:

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

This package owns the typed view models (:mod:`models`), the read-only
``compute`` function (:mod:`readiness`), and the compile-gate seam
(:mod:`compile`). The three wave-close seams (``_close_and_pin``,
``_apply_wave_close``, ``wave_land``) attach ``compute`` as an
**advisory** call: warnings surface in operator output + envelope
extras, but no close path blocks on a non-ready readiness. W19 (later
wave) flips the advisory to gating behind ``profile.verify.enforce``.
"""

from __future__ import annotations

from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)
from eawf.workflow.verify.readiness import compute, load_active_verify_block

__all__ = [
    "CloseReadiness",
    "CriterionView",
    "GateResult",
    "compile_gate",
    "compute",
    "load_active_verify_block",
]
