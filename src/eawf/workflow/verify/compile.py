"""Gate compilation seam for the v0.4 verify spine (W08).

The :func:`compile_gate` function translates a typed
:class:`~eawf.kernel.spec.common.GateSpec` plus the parent
:class:`~eawf.kernel.spec.common.CriterionSpec` into the
:class:`~eawf.workflow.audit_dsl.models.CheckSpec` shape that the
gate runner (hardened in W15 — see
:mod:`eawf.workflow.audit_dsl.registry`) executes against the
checkout. The compile-gate is the bridge between the typed-spec layer
(authored by planners + agents) and the audit-DSL runner (the live
subprocess + diff-base + scope-resolution machinery already in tree).

v0.4.0 contract: **only ``evidence_kind="deterministic"`` compiles**.
``"jury"`` and ``"attested"`` gates return ``None`` here so the
readiness compute knows to fall back to its evidence-row path for the
non-deterministic flavours (jury votes + operator attestations land
in v0.4.1+ — see ``.ea/local/research/2026-05-26-v04-roadmap.md`` §7).

Reuse contract: the public alias
:func:`eawf.workflow.skills.audit.build_criterion_specs` is the
canonical directive->CheckSpec builder for ``command_exit_zero`` gates.
Compile-gate calls that helper so audit + readiness see the **same**
CheckSpec shape; the only delta is that compile-gate then overlays the
W15-added kwargs (``timeout_class``, ``scope``, ``wave_id``,
``wave_file_scopes``) from the gate's typed ``args`` dict.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.workflow.audit_dsl.models import CheckKind, CheckSpec
from eawf.workflow.skills.audit import build_criterion_specs

logger = logging.getLogger(__name__)


#: Kwargs the W15 hardening pass added to ``CommandExitZeroArgs`` that
#: compile_gate must thread through from the typed ``GateSpec.args``
#: dict onto the synthesised :class:`CheckSpec.args`. The shared
#: :func:`eawf.workflow.skills.audit.build_criterion_specs` helper only
#: sets ``argv`` + ``criterion``; everything below is the per-gate
#: scoping + timeout-budget the runner needs to honour.
_COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS: tuple[str, ...] = (
    "timeout_class",
    "scope",
    "wave_id",
    "wave_file_scopes",
)


def compile_gate(gate: GateSpec, *, criterion: CriterionSpec) -> CheckSpec | None:
    """Compile a typed :class:`GateSpec` into a runnable :class:`CheckSpec`.

    v0.4.0 contract: only ``criterion.evidence_kind == "deterministic"``
    compiles to a runnable spec. ``"jury"`` and ``"attested"`` return
    ``None`` so the readiness compute keeps using its evidence-row
    path for the non-deterministic flavours (the v0.4.1 jury / attested
    machinery lands in a later wave per the v0.4 roadmap §7).

    For ``gate.kind == "command_exit_zero"`` the compiler:

    1. Pulls ``args["argv"]`` off the typed gate and routes the
       construction through :func:`build_criterion_specs` so the
       resulting CheckSpec carries the canonical
       ``{"argv": [...], "criterion": ...}`` shape audit already
       produces.
    2. Overlays the W15-added kwargs (``timeout_class``, ``scope``,
       ``wave_id``, ``wave_file_scopes``) from ``gate.args`` onto the
       CheckSpec's args dict so the runner picks them up.
    3. Renames the spec to the gate id so per-gate evidence + waivers
       still address it by its typed identity.

    For other registered deterministic kinds (e.g. ``regex_match``,
    ``schema_validate`` — landing in later waves), the compiler passes
    ``gate.args`` through verbatim with ``name=gate.id``. The
    registry-level validation runs at dispatch time, so an unknown or
    malformed kind raises out of the runner rather than here.

    Args:
        gate: Typed gate row attached to the criterion.
        criterion: Parent criterion. Only ``id`` + ``evidence_kind``
            are read; ``gate_ids`` referential integrity is enforced
            by the readiness loader.

    Returns:
        A :class:`CheckSpec` ready for
        :func:`eawf.workflow.audit_dsl.runner.run_checks`, or ``None``
        when the criterion's ``evidence_kind`` is not
        ``"deterministic"`` (jury / attested defer to v0.4.1+) or
        ``command_exit_zero`` gates lack a usable ``argv``.
    """
    if criterion.evidence_kind != "deterministic":
        logger.debug(
            f"compile_gate skip gate_id={gate.id!r} criterion={criterion.id!r} "
            f"evidence_kind={criterion.evidence_kind!r}"
        )
        return None

    if gate.kind == "command_exit_zero":
        argv = gate.args.get("argv")
        if not isinstance(argv, list) or not argv:
            logger.debug(
                f"compile_gate skip gate_id={gate.id!r} kind={gate.kind!r} reason=missing-argv"
            )
            return None
        directive: dict[str, Any] = {
            "criterion": criterion.id,
            "argv": list(argv),
        }
        base_specs = build_criterion_specs(criterion_checks=[directive], wave=None)
        if not base_specs:
            return None
        base = base_specs[0]
        merged_args: dict[str, Any] = dict(base.args)
        for key in _COMMAND_EXIT_ZERO_PASSTHROUGH_KEYS:
            if key in gate.args:
                merged_args[key] = gate.args[key]
        compiled = CheckSpec(kind=base.kind, name=gate.id, args=merged_args)
        logger.debug(
            f"compile_gate ok gate_id={gate.id!r} kind={compiled.kind!r} "
            f"evidence_kind={criterion.evidence_kind!r}"
        )
        return compiled

    # Generic passthrough for other registered deterministic kinds. The
    # runner's per-kind args validator (or a missing-kind dispatch)
    # raises at execution time rather than here so compile_gate stays a
    # pure shape transform. ``gate.kind`` is ``str`` on the typed spec
    # layer; the cast aligns the type with the CheckSpec.kind Literal
    # while Pydantic still raises ``ValidationError`` at construction
    # time when the value is not a registered CheckKind.
    compiled = CheckSpec(kind=cast(CheckKind, gate.kind), name=gate.id, args=dict(gate.args))
    logger.debug(f"compile_gate ok gate_id={gate.id!r} kind={compiled.kind!r} reason=passthrough")
    return compiled


__all__ = ["compile_gate"]
