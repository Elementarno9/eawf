"""Verify spine — readiness, compile-gate, and waiver evaluation surfaces.

The v0.4 verify spine has three concerns:

* :func:`~eawf.workflow.verify.readiness.compute` — derived readiness view
  for a scope (wave / iter / phase), computed from typed
  :class:`~eawf.kernel.spec.common.CriterionSpec` /
  :class:`~eawf.kernel.spec.common.GateSpec` definitions, the legacy
  :attr:`~eawf.kernel.state.models.Wave.success_criteria` list, and the
  SHA-bound :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows.
* Compile-gate (W08, later wave) — evaluates the gate DAG.
* Waivers (W11, later wave) — operator-attested gate overrides.

This package owns the typed view models (:mod:`models`) and the read-only
``compute`` function (:mod:`readiness`). The three wave-close seams
(``_close_and_pin``, ``_apply_wave_close``, ``wave_land``) attach
``compute`` as an **advisory** call: warnings surface in operator
output + envelope extras, but no close path blocks on a non-ready
readiness. W19 (later wave) flips the advisory to gating behind
``profile.verify.enforce``.
"""

from __future__ import annotations

from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)
from eawf.workflow.verify.readiness import compute

__all__ = [
    "CloseReadiness",
    "CriterionView",
    "GateResult",
    "compute",
]
