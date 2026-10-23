"""Verify spine — compile-gate, sandboxed check runner and waiver surfaces.

* :func:`~eawf.workflow.verify.compile.compile_gate` — translates a
  typed :class:`~eawf.kernel.spec.common.GateSpec` into the
  :class:`~eawf.workflow.audit_dsl.models.CheckSpec` shape the W15-hardened
  gate runner executes. v0.4.0 compiles only
  ``evidence_kind="deterministic"`` gates; ``"jury"`` + ``"attested"``
  return ``None`` and defer to v0.4.1+.
* :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
  — the one execution seam every compiled check passes through. It runs
  the checks in a child interpreter pinned to a throwaway sandbox, so a
  check that drives eawf's own RPCs cannot reach the caller's live
  runtime directory or live ledger.
* Waivers — operator-attested gate overrides, counted per scope by
  :func:`~eawf.workflow.verify.hosted_close.count_scope_waivers`.
* :func:`~eawf.workflow.verify.readiness.load_active_verify_block` — the
  ``verify`` block the enabled profiles declare.

This package also owns the typed view models (:mod:`models`).
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
from eawf.workflow.verify.readiness import load_active_verify_block
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process

__all__ = [
    "CloseReadiness",
    "CriterionView",
    "GateResult",
    "compile_gate",
    "count_scope_waivers",
    "load_active_verify_block",
    "run_checks_out_of_process",
]
