"""In-process binding probes for the idle-contract gate.

The emit-time body-validation probe and the I03 contract probes (intent guard,
UI require-gate, mockup golden-diff tier) drive the live functions with
deliberately defective input and red when a guard stops rejecting it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import get_args

import pydantic
from idle_contract_common import (
    _NON_UI_SCOPE,
    _PROBE_OPENED_AT,
    _UI_SCOPE,
    GateFailure,
    GateResult,
)

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.kernel.state.enums import (
    EffortBucket,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.spec_sync_lints import (
    require_affordance_parity_for_ui_scope as _require_affordance_parity_for_ui_scope,
)
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.audit_dsl.models import CheckKind
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY
from eawf.workflow.lifecycle.transitions import LifecycleError, open_iter, open_phase
from eawf.workflow.lifecycle.wave import plan_wave as _plan_wave
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
)
from eawf.workflow.skills.engine import run_skill as _run_skill

# =========================================================================== #
# Binding-proof probe: emit-time body validation must reject a drifted body.
# =========================================================================== #

#: A ``run_skill`` with the shape of
#: :func:`eawf.workflow.skills.engine.run_skill`. Injected so the
#: body-validation-idle failure mode is testable: a stub that returns an
#: envelope without validating the body makes the probe stop raising.
type RunSkillFn = Callable[[Skill, SkillContext], OutputEnvelope]

#: The registered skill the binding probe drifts. ``/audit`` resolves to
#: :class:`~eawf.workflow.skills.bodies.audit.AuditBody`, whose only required
#: fields are ``scope_id`` and ``kind`` -- a minimal valid body that an extra
#: key drifts unambiguously.
_PROBE_SKILL_NAME = "/audit"

#: The extra-forbid key the probe injects to drift the body. It is not a field
#: on any registered body model, so an ``extra="forbid"`` model rejects it.
_DRIFT_KEY = "__idle_contract_probe_drift__"

#: Pure in-process probe context. The scope / session are well-formed URN-shaped
#: strings; the probe never reads ``.ea/`` or persists anything, so they only
#: need to satisfy the envelope header's type, not resolve to real state.
_PROBE_SCOPE = "urn:eawf:v1:state:QR/P00"
_PROBE_SESSION = "urn:eawf:v1:store:QR/sessions/SES-PROBE"


class _DriftedBodySkill(Skill):
    """Pure in-process probe skill that emits a deliberately drifted dict body.

    The skill's :meth:`probe` always succeeds with a synthetic instrument map
    (it never shells out, so the binding probe stays hermetic), and its
    :meth:`action` returns a :class:`SkillResult` whose ``body`` is a dict that
    is valid for :class:`~eawf.workflow.skills.bodies.audit.AuditBody` except
    for one extra-forbid key. Driving this skill through ``run_skill`` exercises
    exactly the emit-time body-validation chokepoint: a live binding rejects the
    drift with :class:`pydantic.ValidationError` before the envelope is built.
    """

    name = _PROBE_SKILL_NAME

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        """Return an always-ok probe with a synthetic instrument map (no shell)."""
        return ProbeOutcome(ok=True, instrument_probe={"git": "ok"})

    def action(self, ctx: SkillContext) -> SkillResult:
        """Return an ``ok`` result whose dict body carries one extra-forbid key."""
        body: dict[str, object] = {
            "scope_id": _PROBE_SCOPE,
            "kind": "evaluation",
            _DRIFT_KEY: "this key is not a field on AuditBody",
        }
        return SkillResult(status="ok", body=body)


def check_skill_body_binding(
    *,
    run_skill_fn: RunSkillFn = _run_skill,
) -> GateResult:
    """Assert the emit-time body-validation binding rejects a drifted dict body.

    Builds a pure in-process :class:`_DriftedBodySkill` whose action returns a
    dict body that is valid for the registered ``/audit`` body model except for
    one :data:`_DRIFT_KEY` extra-forbid key, then drives it through
    *run_skill_fn*. A live emit-time binding (the
    :func:`~eawf.workflow.skills.engine._validate_body` chokepoint that
    ``run_skill`` invokes before building the envelope) raises
    :class:`pydantic.ValidationError` on the drift -- proving the binding is
    not idle.

    The probe is the meta-binding for the W02/W03/W05 emit-validation work: if a
    later refactor drops the ``_validate_body`` call (or neuters it to a no-op),
    *run_skill_fn* returns an envelope WITHOUT raising, this check fails
    :attr:`GateFailure.BODY_VALIDATION_IDLE`, and the gate exits non-zero. The
    failure-mode injection seam is *run_skill_fn*: a test passes a stub that
    skips validation to prove the gate bites when the binding is removed.

    The probe mutates nothing -- it never writes a file, never touches
    ``.ea/``, and never runs a mutating ``eawf`` command. The synthetic scope /
    session strings exist only to satisfy the envelope header's type.

    Args:
        run_skill_fn: The skill engine under test. Defaults to
            :func:`eawf.workflow.skills.engine.run_skill`; a test injects a
            no-validation stub to exercise the idle failure mode.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when
        *run_skill_fn* raised :class:`pydantic.ValidationError` on the drifted
        body; otherwise ``failure`` is :attr:`GateFailure.BODY_VALIDATION_IDLE`.
    """
    skill = _DriftedBodySkill()
    ctx = SkillContext(scope=_PROBE_SCOPE, session=_PROBE_SESSION)
    try:
        run_skill_fn(skill, ctx)
    except pydantic.ValidationError:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (emit-time body validation rejected a "
                f"drifted {_PROBE_SKILL_NAME} body with pydantic.ValidationError)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.BODY_VALIDATION_IDLE,
        message=(
            "emit-time body validation is idle: a drifted dict body (an "
            f"extra-forbid key on the {_PROBE_SKILL_NAME} body model) was emitted "
            "through run_skill WITHOUT raising pydantic.ValidationError; the "
            "body-validation-at-emit binding regressed to a no-op"
        ),
    )


# =========================================================================== #
# I03 contract probes: intent guard, UI require-gate, mockup golden tier.
# =========================================================================== #

#: A ``plan_wave``-shaped callable. ``Callable[..., object]`` is intentional:
#: the real function is keyword-only after ``state``, and tests inject a
#: no-op stand-in to prove the gate fails when the guard stops rejecting.
type PlanWaveFn = Callable[..., object]

#: A ``require_affordance_parity_for_ui_scope``-shaped callable. Kept injectable
#: for the same reason as :data:`PlanWaveFn`.
type UiRequireGateFn = Callable[..., None]

#: A ``_tier_for_gate_kind``-shaped callable.
type TierForGateKindFn = Callable[[str], OracleTier]

_I03_PHASE_ID = "P00"
_I03_ITER_ID = "P00-I01"
_I03_INTENT_PROBE_WAVE_ID = "P00-I01-W01"
_I03_UI_PROBE_WAVE_ID = "P00-I01-W02"
_MOCKUP_GOLDEN_DIFF_KIND = "mockup_golden_diff"


def _make_i03_probe_state() -> State:
    """Build a pure in-process state with one open phase + iter.

    Returns:
        A validated :class:`State` that is sufficient for
        :func:`eawf.workflow.lifecycle.wave.plan_wave` to reach the authored
        wave guards. No files are read or written.
    """
    state = State.model_validate(
        {
            "schema_version": "1.8",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _PROBE_OPENED_AT.isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["probe"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    open_phase(state, phase_id=_I03_PHASE_ID, title="Probe phase")
    open_iter(state, iter_id=_I03_ITER_ID, phase_id=_I03_PHASE_ID, title="Probe iter")
    return state


def _probe_required_intent_guard(plan_wave_fn: PlanWaveFn) -> GateResult | None:
    """Return a failure when the authored-wave ``intent=None`` guard is idle."""
    try:
        plan_wave_fn(
            _make_i03_probe_state(),
            wave_id=_I03_INTENT_PROBE_WAVE_ID,
            iter_id=_I03_ITER_ID,
            title="Intent probe wave",
            file_scopes=[_NON_UI_SCOPE],
            effort_bucket=EffortBucket.M,
            intent=None,
        )
    except LifecycleError as exc:
        if "has no intent" in str(exc):
            return None
        return GateResult(
            passed=False,
            failure=GateFailure.REQUIRED_INTENT_IDLE,
            message=(
                "required-intent guard did not fire cleanly: expected "
                f"{_I03_INTENT_PROBE_WAVE_ID!r} with intent=None to raise the "
                f"'has no intent' LifecycleError, got {exc!r}"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.REQUIRED_INTENT_IDLE,
        message=(
            "required-intent guard is idle: a None-intent probe wave planned "
            "without raising LifecycleError"
        ),
    )


def _probe_ui_require_contract(ui_require_gate_fn: UiRequireGateFn) -> GateResult | None:
    """Return a failure when the UI affordance-parity require-gate is idle."""
    try:
        ui_require_gate_fn(
            wave_id=_I03_UI_PROBE_WAVE_ID,
            file_scopes=[_UI_SCOPE],
            gates=[],
        )
    except DaemonValidationError as exc:
        if "affordance_parity" in str(exc):
            return None
        return GateResult(
            passed=False,
            failure=GateFailure.UI_REQUIRE_GATE_IDLE,
            message=(
                "UI-scope require-gate did not fire cleanly: expected the "
                f"ungated probe wave {_I03_UI_PROBE_WAVE_ID!r} to name "
                f"'affordance_parity', got {exc!r}"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.UI_REQUIRE_GATE_IDLE,
        message=(
            "UI-scope require-gate is idle: a UI probe wave with no "
            "affordance_parity gate was accepted"
        ),
    )


def _probe_mockup_golden_diff_registration(
    *,
    registry: Mapping[str, object],
    tier_for_gate_kind_fn: TierForGateKindFn,
) -> GateResult | None:
    """Return a failure when ``mockup_golden_diff`` is unregistered or unmapped."""
    check_kinds = set(get_args(CheckKind))
    if _MOCKUP_GOLDEN_DIFF_KIND not in check_kinds:
        return GateResult(
            passed=False,
            failure=GateFailure.MOCKUP_GOLDEN_DIFF_IDLE,
            message=(
                f"{_MOCKUP_GOLDEN_DIFF_KIND} CheckKind is idle: it is absent "
                "from the CheckKind literal"
            ),
        )
    runner = registry.get(_MOCKUP_GOLDEN_DIFF_KIND)
    if not callable(runner):
        return GateResult(
            passed=False,
            failure=GateFailure.MOCKUP_GOLDEN_DIFF_IDLE,
            message=(
                f"{_MOCKUP_GOLDEN_DIFF_KIND} CheckKind is idle: no callable "
                "runner is registered in CHECK_REGISTRY"
            ),
        )
    try:
        tier = tier_for_gate_kind_fn(_MOCKUP_GOLDEN_DIFF_KIND)
    except ValueError as exc:
        return GateResult(
            passed=False,
            failure=GateFailure.MOCKUP_GOLDEN_DIFF_IDLE,
            message=(
                f"{_MOCKUP_GOLDEN_DIFF_KIND} CheckKind is idle: no oracle-tier "
                f"mapping exists ({exc})"
            ),
        )
    if tier is not OracleTier.T5_GOLDEN:
        return GateResult(
            passed=False,
            failure=GateFailure.MOCKUP_GOLDEN_DIFF_IDLE,
            message=(
                f"{_MOCKUP_GOLDEN_DIFF_KIND} CheckKind is mapped to "
                f"{tier.value!r}, expected {OracleTier.T5_GOLDEN.value!r}"
            ),
        )
    return None


def check_i03_contracts(
    *,
    plan_wave_fn: PlanWaveFn = _plan_wave,
    ui_require_gate_fn: UiRequireGateFn = _require_affordance_parity_for_ui_scope,
    registry: Mapping[str, object] = CHECK_REGISTRY,
    tier_for_gate_kind_fn: TierForGateKindFn = _tier_for_gate_kind,
) -> GateResult:
    """Assert every I03 contract fires through a pure in-process probe.

    Args:
        plan_wave_fn: Authored-wave planner under test. Defaults to the live
            :func:`eawf.workflow.lifecycle.wave.plan_wave`; tests inject a
            no-op to prove the required-intent guard is not idle.
        ui_require_gate_fn: UI-scope require-gate under test. Defaults to the
            live affordance-parity sync lint.
        registry: Audit-DSL check registry under test.
        tier_for_gate_kind_fn: Gate-kind to oracle-tier mapper under test.

    Returns:
        A passing :class:`GateResult` only when all three I03 probes fire.
    """
    for failure in (
        _probe_required_intent_guard(plan_wave_fn),
        _probe_ui_require_contract(ui_require_gate_fn),
        _probe_mockup_golden_diff_registration(
            registry=registry,
            tier_for_gate_kind_fn=tier_for_gate_kind_fn,
        ),
    ):
        if failure is not None:
            return failure
    return GateResult(
        passed=True,
        failure=None,
        message=(
            "idle-contract gate: ok (I03 required-intent guard, UI "
            "affordance-parity require-gate, and mockup_golden_diff tier mapping fired)"
        ),
    )
