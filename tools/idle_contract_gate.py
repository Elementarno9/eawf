"""Deterministic idle-contract gate for the band-scoped spec-jury QC gate.

This repo has a history of building a verifier and then leaving it IDLE
forever -- a dead gate that never runs (tracked as the B091 idle-verifier
regression). The spec-jury close gate is the latest such verifier: W05/W06
wired the producer
(:func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict`) and the
band-conditional resolver
(:func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`), and the
shipped ``quality`` profile turns it on for a non-empty UI/UX band. This gate
makes "wired + band-scoped, not idle, not global" a CHECKED invariant rather
than a hope.

Four gates run from :func:`main`, in precedence order, and any failing
exits non-zero:

- :func:`check_idle_contract` -- the original single B091 spec-jury contract
  described below.
- :func:`check_skill_body_binding` -- a *binding-proof* probe that drives a
  deliberately drifted dict body through ``run_skill`` and asserts the emit
  path RAISES ``pydantic.ValidationError``. The drift is an extra-forbid key
  against a registered body model, so a working binding rejects it before the
  envelope is built. If a later refactor lets the body-validation-at-emit
  binding regress to idle, the drifted body would emit silently and this probe
  stops raising -- failing the gate. This is the meta-binding that keeps the
  emit-validation binding from silently going dead.
- :func:`check_i03_contracts` -- three in-process probes for the I03 contracts:
  the authored-wave intent guard must reject ``intent=None``, the UI-scope
  require-gate must reject a UI wave with no ``affordance_parity`` gate, and
  ``mockup_golden_diff`` must be a registered ``CheckKind`` with a mapped
  ``OracleTier``.
- :func:`check_resolve_routing_wired` -- a source-scan probe asserting the live
  wave-spawn dispatch (``agent.py``) calls ``resolve_routing`` directly, so the
  per-role tier table selects the spawn model instead of a hardcoded default.
  If a later refactor drops the direct call, the source no longer matches and
  this gate reds -- un-idling the routing contract.
- :func:`check_spec_jury_ballot_fn_wired` -- a source-scan probe asserting the
  daemon close path binds the LIVE per-item spec-jury ballot fn (I09-W05):
  ``_spec_jury_ballot_fn`` returns ``live_per_item_ballot_fn(...)`` and the gate
  consults it. A regression that reverts the builder to a bare ``return None``
  re-idles the producer and reds this row.
- :func:`check_jury_reliability_map_wired` -- a source-scan probe asserting the
  live convener threads the reputation reliability map into the jury reducer
  (I09-W06): ``reliability=reliability`` flows into
  ``aggregate_jury(..., reliability=...)``. Dropping the kwarg re-idles the
  reputation-weighting seam and reds this row.
- :func:`check_jury_block_authority_wired` -- a source-scan probe asserting the
  ordered-oracle jury branch tests ``block_authority is BlockAuthority.BLOCKING``
  BEFORE it raises ``LifecycleError`` (I09-W04), so a veto blocks only an EARNED
  jury. Bypassing the authority gate reds this row.
- :func:`check_validate_jury_cli_wired` -- a source-scan probe asserting
  ``validate_jury`` has a live CLI caller (I09-W07): the ``eawf metrics
  jury-validation`` command binds it directly. Orphaning the reducer reds this
  row.
- :func:`check_track_rpc_wired` -- a source-scan probe asserting the daemon
  method table registers BOTH the ``track.add`` and ``track.switch`` RPCs
  (I11-W02), so the CLI ``track add`` / ``track switch`` shims have a daemon
  caller. Dropping either ``@register("track.<verb>")`` orphans the Track
  add/switch seam and reds this row.
- :func:`check_phase_track_tag_wired` -- a source-scan probe asserting
  ``open_phase`` silently stamps each phase with ``track_id=state.current.track_id``
  (I11-W03), so phases tag their owning Track. Dropping the stamp re-idles the
  silent phase-tag binding and reds this row.
- :func:`check_drive_ladders_wired` -- a source-scan probe asserting the live
  drive (``start_background_drive``) arms ``arm_drive`` with both
  ``classify=_live_lane_error_classifier`` and ``repair=repair_hook`` (I17-W11),
  so the bounded spawn ladder (DL-11) and the grounded repair ladder (DL-7) fire
  on a real autopilot run instead of staying dormant until a test injects them.
  Dropping either kwarg reds this row.
- :func:`check_live_output_text_wired` -- a source-scan probe asserting the live
  wave spawn (``_spawn_and_dispatch``) threads ``output_text=spawn_result.text``
  into ``run_dispatch`` (I17-W11), so the W08 stdout producer fires on a real
  spawn and the agent-watch live tail is not empty. Dropping the thread reds this
  row.
- :func:`check_campaign_claim_fold_wired` -- a source-scan probe asserting the
  campaign run path (``research.py``) folds each round's reconciled claims into
  the canonical ``state.claims`` via ``_commit_worktree_state`` (P30-I18 W05/W06),
  so a live round populates the real claim ledger instead of a throwaway
  ``State.model_construct`` shadow. Reverting to the shadow-only reconcile reds
  this row.
- :func:`check_campaign_carryover_prune_wired` -- a source-scan probe asserting
  ``run_campaign`` calls ``prune_round_carryover`` between rounds (P30-I18 W06),
  so the L1 between-rounds reducer has a production caller. Dropping the call
  (its prior zero-caller state) reds this row.
- :func:`check_runtime_gate_is_not_idle` -- verifies this always-run
  pre-commit gate stays enabled so the runtime close gate cannot ship idle.
- :func:`detect_idle_contracts` -- a *meta-gate* that reads a git diff and
  flags any newly-defined contract (a ``check_*`` / ``*_gate`` / ``*_lint``
  function, a ``CheckKind`` runner registration, an ``OracleTier`` dispatch
  arm, or an ``eawf0##_*.py`` lint module) that ships idle: no call-site
  outside its own module AND no asserting test references it. The meta-gate
  generalizes the B091 lesson from one hardcoded contract to *every* future
  contract a diff introduces, so a fresh dead verifier is caught the same
  commit it lands.

Three independent contracts are asserted, in precedence order:

- **not-idle** -- the producer is importable AND at least one shipped profile
  enables it via a non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
  with :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` true. A
  producer that no profile wires on is idle-forever; this contract fails on
  exactly that.
- **band-scoped (not global)** -- for that band-enabling profile,
  :func:`resolve_wave_verify_block` resolves to ``enforce=True`` for a UI-scope
  probe wave (``file_scopes`` under ``src/eawf/surfaces/tui/`` or
  ``.../render/``) AND to ``enforce=False`` for a non-UI probe wave (e.g.
  ``src/eawf/kernel/...``). A profile that flips enforcement on fleet-wide
  fails this contract because it would gate every wave, not just the band.
- **emit-validation not-idle** -- a drifted dict body (an extra-forbid key on a
  registered body model) driven through ``run_skill`` RAISES
  ``pydantic.ValidationError`` before the envelope is built. A regression that
  drops the emit-time body-validation chokepoint would emit the drift silently;
  this contract (:func:`check_skill_body_binding`) fails on exactly that.

Both band-probe waves and the body-binding probe are pure in-process objects --
the gate never mutates state, never writes a file, never runs a mutating
``eawf`` command.

The checks are injectable: :func:`check_idle_contract` takes the candidate
profile list and the resolver as parameters, and
:func:`check_skill_body_binding` takes the ``run_skill`` callable (each
defaulting to the live production value) so the failure modes are testable
without editing shipped profiles or the engine. Each returns a typed
:class:`GateResult` and the thin :func:`main` CLI maps them onto an exit code.

Invocation:

    uv run tools/idle_contract_gate.py

Exit codes:
- ``0`` -- the producer is importable + wired on for a non-empty band that
  resolves band-scoped (not global), AND the emit-time body validation rejects
  a drifted body, AND all I03 probes fire, AND no newly-defined contract in the
  staged diff ships idle.
- ``1`` -- a contract failed (the failure is named on stderr).
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import get_args

import pydantic

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.kernel.state.enums import EffortBucket, ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.models import ProfileBody, VerifyBlock
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.spec_sync_lints import (
    require_affordance_parity_for_ui_scope as _require_affordance_parity_for_ui_scope,
)
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.workflow.audit_dsl.models import CheckKind
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY, registered_audit_dsl_kinds
from eawf.workflow.dispatch.spec_jury import produce_spec_jury_verdict  # noqa: F401
from eawf.workflow.lifecycle.transitions import LifecycleError, open_iter, open_phase
from eawf.workflow.lifecycle.wave import plan_wave as _plan_wave
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
)
from eawf.workflow.skills.engine import run_skill as _run_skill
from eawf.workflow.verify.readiness import (
    resolve_wave_verify_block,
    wired_audit_dsl_kinds,
)

#: A resolver with the shape of
#: :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`. Injected so
#: the global-flip failure mode is testable with a stub resolver that returns
#: an always-enforcing block.
type ResolveFn = Callable[[VerifyBlock | None, Wave], VerifyBlock | None]

#: Probe file scope that IS UI surface (per
#: :func:`eawf.kernel.spec.heuristics.is_ui_scope`). A band profile MUST resolve
#: to ``enforce=True`` for a wave touching this scope.
_UI_SCOPE = "src/eawf/surfaces/tui/app.py"

#: Probe file scope that is NOT UI surface. A band-scoped (not global) profile
#: MUST resolve to ``enforce=False`` for a wave touching this scope.
_NON_UI_SCOPE = "src/eawf/kernel/state/models.py"

_PROBE_OPENED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class GateFailure(StrEnum):
    """The mutually exclusive ways the idle-contract gate can fail.

    The order encodes precedence: an absent producer wiring (idle) is reported
    before a band/global resolution defect, so a single run names the more
    fundamental problem first.
    """

    PRODUCER_IDLE = "producer_idle"
    BAND_ENFORCES_GLOBALLY = "band_enforces_globally"
    BODY_VALIDATION_IDLE = "body_validation_idle"
    REQUIRED_INTENT_IDLE = "required_intent_idle"
    UI_REQUIRE_GATE_IDLE = "ui_require_gate_idle"
    MOCKUP_GOLDEN_DIFF_IDLE = "mockup_golden_diff_idle"
    RESOLVE_ROUTING_IDLE = "resolve_routing_idle"
    RUNTIME_GATE_IDLE = "runtime_gate_idle"
    AUDIT_DSL_KIND_IDLE = "audit_dsl_kind_idle"
    SPEC_JURY_BALLOT_FN_IDLE = "spec_jury_ballot_fn_idle"
    JURY_RELIABILITY_MAP_IDLE = "jury_reliability_map_idle"
    JURY_BLOCK_AUTHORITY_IDLE = "jury_block_authority_idle"
    VALIDATE_JURY_CLI_IDLE = "validate_jury_cli_idle"
    TRACK_RPC_IDLE = "track_rpc_idle"
    PHASE_TRACK_TAG_IDLE = "phase_track_tag_idle"
    DRIVE_LADDERS_IDLE = "drive_ladders_idle"
    LIVE_OUTPUT_TEXT_IDLE = "live_output_text_idle"
    CAMPAIGN_CLAIM_FOLD_IDLE = "campaign_claim_fold_idle"
    CAMPAIGN_CARRYOVER_PRUNE_IDLE = "campaign_carryover_prune_idle"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one idle-contract check.

    Attributes:
        passed: Whether both the not-idle and band-scoped contracts held.
        failure: The failure kind when ``passed`` is ``False``; ``None`` on a
            pass.
        message: A human-readable line; on failure it names the violated
            contract and the offending profile.
    """

    passed: bool
    failure: GateFailure | None
    message: str


def _make_probe_wave(*, scope: str) -> Wave:
    """Build a pure in-process probe :class:`Wave` whose only varying axis is *scope*.

    The wave is never persisted and never mutated; it exists only so
    :func:`resolve_wave_verify_block` can be asked how it bands a given file
    scope. The id / title are deliberately neutral (no ``uiux_bands`` token
    substring) so band membership is decided by the structural ``file_scopes``
    arm alone -- the gate can then assert the UI / non-UI split unambiguously.

    Args:
        scope: The single repo-relative file scope the probe wave declares.

    Returns:
        A validated :class:`Wave` with ``file_scopes=[scope]``.
    """
    return Wave(
        id="P00-I01-W01",
        iter_id="P00-I01",
        title="idle-contract probe wave",
        status=WaveStatus.PENDING,
        file_scopes=[scope],
        opened_at=_PROBE_OPENED_AT,
    )


def _band_enabling_profiles(profiles: Sequence[ProfileBody]) -> list[ProfileBody]:
    """Return the profiles that wire the spec-jury producer on for a real band.

    A profile wires the producer on when its ``verify`` block declares a
    non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
    AND :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` is true: the
    band list is the structural opt-in and ``enforce`` is the gating bit the
    resolver narrows per wave. A profile with an empty band list (or
    ``enforce=False``, or no verify block at all) leaves the producer idle.

    Args:
        profiles: The candidate profile bodies to scan.

    Returns:
        The subset of *profiles* whose verify block enables a non-empty band
        with enforcement on.
    """
    return [
        profile
        for profile in profiles
        if profile.verify is not None and profile.verify.enforce and profile.verify.uiux_bands
    ]


def _load_shipped_profiles() -> list[ProfileBody]:
    """Load every shipped (built-in) profile body.

    Returns:
        The validated :class:`ProfileBody` for each id from
        :func:`eawf.platform.profiles.loader.list_profiles`, in id order.
    """
    return [load_profile(profile_id) for profile_id in list_profiles()]


def check_idle_contract(
    *,
    profiles: Sequence[ProfileBody] | None = None,
    resolve_fn: ResolveFn = resolve_wave_verify_block,
) -> GateResult:
    """Assert the spec-jury producer is wired on for a band and resolves band-scoped.

    The two contracts are checked in precedence order:

    1. **not-idle** -- at least one of *profiles* enables the producer via a
       non-empty ``uiux_bands`` with ``enforce=True`` (see
       :func:`_band_enabling_profiles`). The producer importability is proven
       by this module importing
       :func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict` at
       module load. When no profile wires it on, the producer is idle-forever
       and the gate fails :attr:`GateFailure.PRODUCER_IDLE`.
    2. **band-scoped** -- for a band-enabling profile, *resolve_fn* resolves to
       ``enforce=True`` for a UI-scope probe wave AND ``enforce=False`` for a
       non-UI probe wave. A profile that resolves to ``enforce=True`` for the
       non-UI probe enforces fleet-wide (it would gate every wave) and the gate
       fails :attr:`GateFailure.BAND_ENFORCES_GLOBALLY`.

    Args:
        profiles: Candidate profile bodies. ``None`` loads the shipped
            built-in profiles via :func:`_load_shipped_profiles`. Tests inject
            a synthetic list to exercise the idle / global failure modes
            without editing shipped profiles.
        resolve_fn: The band-conditional resolver under test. Defaults to
            :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`;
            tests inject a stub to force the global-flip path.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the
        producer is wired on for a non-empty band AND that band profile
        resolves band-scoped (UI enforces, non-UI does not); otherwise
        ``failure`` names the first violated contract.
    """
    candidate = list(profiles) if profiles is not None else _load_shipped_profiles()

    band_profiles = _band_enabling_profiles(candidate)
    if not band_profiles:
        return GateResult(
            passed=False,
            failure=GateFailure.PRODUCER_IDLE,
            message=(
                "spec-jury producer is idle: no shipped profile enables a verify band "
                "(a non-empty 'uiux_bands' with 'enforce: true'); the producer "
                "'produce_spec_jury_verdict' is importable but never wired on"
            ),
        )

    ui_wave = _make_probe_wave(scope=_UI_SCOPE)
    non_ui_wave = _make_probe_wave(scope=_NON_UI_SCOPE)
    for profile in band_profiles:
        ui_resolved = resolve_fn(profile.verify, ui_wave)
        non_ui_resolved = resolve_fn(profile.verify, non_ui_wave)
        ui_enforces = ui_resolved is not None and ui_resolved.enforce
        non_ui_enforces = non_ui_resolved is not None and non_ui_resolved.enforce
        if not ui_enforces or non_ui_enforces:
            return GateResult(
                passed=False,
                failure=GateFailure.BAND_ENFORCES_GLOBALLY,
                message=(
                    f"band profile {profile.name!r} enforces globally, not band-scoped: "
                    f"ui_scope enforce={ui_enforces} (expected True), "
                    f"non_ui_scope enforce={non_ui_enforces} (expected False); "
                    "a band profile must gate only UI/UX waves, never the whole fleet"
                ),
            )

    band_names = ", ".join(sorted(profile.name for profile in band_profiles))
    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"idle-contract gate: ok (spec-jury producer wired on by [{band_names}]; "
            "band resolves enforce=True for UI, enforce=False for non-UI)"
        ),
    )


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


# =========================================================================== #
# resolve_routing wiring: the live dispatch path must call resolve_routing.
# =========================================================================== #

#: The live dispatch module that must call ``resolve_routing`` directly so the
#: per-role tier table selects the spawn model instead of a hardcoded default.
#: The pre-commit gate reads this file off the working tree; a test injects the
#: text to exercise both the wired and idle outcomes.
_LIVE_DISPATCH_MODULE = "src/eawf/runtime/daemon/methods/agent.py"

#: A direct ``resolve_routing(...)`` call (not a docstring / import mention).
#: The trailing ``(`` is what distinguishes a live call from a bare reference,
#: so a comment or ``:func:`` cross-link in prose does not satisfy the gate.
_RESOLVE_ROUTING_CALL_RE = re.compile(r"\bresolve_routing\s*\(")


def check_resolve_routing_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_DISPATCH_MODULE,
) -> GateResult:
    """Assert the live dispatch path calls :func:`resolve_routing` directly.

    The ``(agent_role, effort_bucket) -> model`` routing table is built but was
    only reachable through the per-vendor wrapper; this contract pins that the
    live wave-spawn dispatch (:func:`eawf.runtime.daemon.methods.agent._resolve_spawn_model`)
    calls :func:`eawf.workflow.dispatch.routing.resolve_routing` itself, so role
    + effort selects the model tier rather than a hardcoded default. If a later
    refactor drops the direct call (reverting to a single hardcoded model), the
    source no longer matches :data:`_RESOLVE_ROUTING_CALL_RE` and this gate
    fails :attr:`GateFailure.RESOLVE_ROUTING_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the regressed (idle) outcomes; when it is
    ``None`` the module is read off the working tree via :data:`_REPO_ROOT`
    (mirroring :func:`check_runtime_gate_is_not_idle`, so this gate is immune to
    the meta-gate's injected reader stub).

    Args:
        module_text: The live dispatch module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the live dispatch module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the live
        dispatch module carries a direct ``resolve_routing(`` call; otherwise
        ``failure`` is :attr:`GateFailure.RESOLVE_ROUTING_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _RESOLVE_ROUTING_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live dispatch calls resolve_routing -- "
                "role/effort selects the model tier, not a hardcoded default)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.RESOLVE_ROUTING_IDLE,
        message=(
            "resolve_routing is idle in live dispatch: "
            f"{module_path} carries no direct resolve_routing(...) call, so the "
            "per-role tier table never selects the spawn model (the path "
            "regressed to a hardcoded default)"
        ),
    )


# =========================================================================== #
# I09 jury-validation bindings: each of the four must stay wired, not idle.
# =========================================================================== #

#: The daemon close path that binds the per-item spec-jury ballot fn (I09-W05)
#: and consults the earned block authority (I09-W04). Read off the working tree.
_SPEC_JURY_GATE_MODULE = "src/eawf/runtime/daemon/methods/state.py"

#: The live convener that threads the reputation reliability map into the jury
#: reducer (I09-W06). Read off the working tree.
_CROSS_VENDOR_JURY_MODULE = "src/eawf/observability/eval/cross_vendor_jury.py"

#: The ordered-oracle module whose jury branch consults *block_authority* before
#: raising (I09-W04). Read off the working tree.
_ORACLE_MODULE = "src/eawf/workflow/verify/oracle.py"

#: The CLI command that gives ``validate_jury`` a live caller (I09-W07). Read off
#: the working tree.
_METRICS_CLI_MODULE = "src/eawf/surfaces/cli/commands/metrics.py"

#: ``_spec_jury_ballot_fn`` returns a LIVE ballot fn by binding
#: ``live_per_item_ballot_fn(...)``; a regression that reverts the body to a bare
#: ``return None`` (re-idling the producer) drops this call.
_LIVE_BALLOT_FN_CALL_RE = re.compile(r"\blive_per_item_ballot_fn\s*\(")

#: The gate consults ``_spec_jury_ballot_fn(...)`` and degrades when it is None.
#: A regression that stops calling the builder leaves the producer unreachable.
_BALLOT_FN_CONSULT_RE = re.compile(r"\bballot_fn\s*=\s*_spec_jury_ballot_fn\s*\(")

#: The convener threads ``reliability=`` into the jury reducer; a regression that
#: drops the kwarg re-idles the reputation-weighting seam.
_RELIABILITY_THREAD_RE = re.compile(r"\breliability\s*=\s*reliability\b")

#: The reducer forwards the map into ``aggregate_jury(..., reliability=...)``;
#: this is the production sink that consumes the threaded map.
_AGGREGATE_RELIABILITY_RE = re.compile(r"\baggregate_jury\s*\([^)]*\breliability\s*=")

#: The oracle jury branch tests blocking authority BEFORE it raises; the gate
#: keyword and the raise must both be present so the veto is not unconditional.
_BLOCK_AUTHORITY_GATE_RE = re.compile(r"\bif\s+block_authority\s+is\s+BlockAuthority\.BLOCKING\b")

#: The blocking branch raises a LifecycleError so a calibrated jury's veto blocks
#: the close.
_JURY_VETO_RAISE_RE = re.compile(r"\braise\s+LifecycleError\b")

#: The CLI command binds ``validate_jury(...)`` directly, giving it a live caller
#: (the moment a labelled cohort lands the CLI surfaces a scored report).
_VALIDATE_JURY_CALL_RE = re.compile(r"\bvalidate_jury\s*\(")


def check_spec_jury_ballot_fn_wired(
    *,
    module_text: str | None = None,
    module_path: str = _SPEC_JURY_GATE_MODULE,
) -> GateResult:
    """Assert the spec-jury close gate binds a live per-item ballot fn (I09-W05).

    The TRUST-5 binding: :func:`_spec_jury_ballot_fn` returns a non-``None`` live
    ballot fn by binding
    :func:`eawf.workflow.dispatch.spec_jury.live_per_item_ballot_fn` (when a
    cross-vendor quorum resolves), and :func:`_enforce_spec_jury_gate` consults
    that builder before routing the close through the producer. If a later
    refactor reverts the builder body to a bare ``return None`` (re-idling the
    producer) or stops consulting it, the source no longer matches and this gate
    fails :attr:`GateFailure.SPEC_JURY_BALLOT_FN_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (``return None``) outcomes.

    Args:
        module_text: The spec-jury gate module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the spec-jury gate module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both binds ``live_per_item_ballot_fn(`` and consults
        ``ballot_fn = _spec_jury_ballot_fn(``; otherwise ``failure`` is
        :attr:`GateFailure.SPEC_JURY_BALLOT_FN_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    binds_live = _LIVE_BALLOT_FN_CALL_RE.search(text) is not None
    consults = _BALLOT_FN_CONSULT_RE.search(text) is not None
    if binds_live and consults:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (spec-jury gate binds the live per-item "
                "ballot fn -- the producer is reachable, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.SPEC_JURY_BALLOT_FN_IDLE,
        message=(
            "spec-jury per-item ballot fn is idle: "
            f"{module_path} binds_live={binds_live} consults={consults} (expected "
            "both True); _spec_jury_ballot_fn must bind live_per_item_ballot_fn(...) "
            "and the gate must consult it -- the producer regressed to return None"
        ),
    )


def check_jury_reliability_map_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CROSS_VENDOR_JURY_MODULE,
) -> GateResult:
    """Assert the live convener threads a reliability map into the reducer (I09-W06).

    The TRUST-6 binding: the live convener
    (:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`)
    threads its ``reliability`` map into
    :func:`eawf.observability.eval.cross_vendor_jury._reduce_jury`, which forwards
    it into :func:`eawf.observability.eval.jury.aggregate_jury` as
    ``reliability=...`` -- the first production sink of the built-but-idle
    reputation-weighting seam. If a later refactor drops the ``reliability=``
    forward (re-idling the seam so every juror weights neutrally regardless of
    the reputation map), the source no longer matches and this gate fails
    :attr:`GateFailure.JURY_RELIABILITY_MAP_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled outcomes.

    Args:
        module_text: The cross-vendor jury module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the convener module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both threads ``reliability=reliability`` and forwards it into
        ``aggregate_jury(..., reliability=...)``; otherwise ``failure`` is
        :attr:`GateFailure.JURY_RELIABILITY_MAP_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    threads = _RELIABILITY_THREAD_RE.search(text) is not None
    aggregates = _AGGREGATE_RELIABILITY_RE.search(text) is not None
    if threads and aggregates:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live convener threads the reliability map "
                "into aggregate_jury -- the reputation-weighting seam is not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.JURY_RELIABILITY_MAP_IDLE,
        message=(
            "jury reliability map is idle: "
            f"{module_path} threads={threads} aggregates={aggregates} (expected "
            "both True); the convener must forward reliability=reliability into "
            "aggregate_jury(..., reliability=...) -- the reputation seam went None"
        ),
    )


def check_jury_block_authority_wired(
    *,
    module_text: str | None = None,
    module_path: str = _ORACLE_MODULE,
) -> GateResult:
    """Assert the oracle jury branch consults block authority before raising (I09-W04).

    The TRUST-4 binding: the ordered-oracle jury tier
    (:func:`eawf.workflow.verify.oracle.run_oracle`) tests
    ``block_authority is BlockAuthority.BLOCKING`` BEFORE it raises
    :class:`~eawf.workflow.lifecycle.transitions.LifecycleError`, so a
    non-pass jury outcome blocks the close only once the jury has EARNED blocking
    authority -- an uncalibrated jury's veto stays advisory. If a later refactor
    drops the authority gate (making the veto raise unconditionally, or never),
    the source no longer matches both the gate keyword and the raise, and this
    gate fails :attr:`GateFailure.JURY_BLOCK_AUTHORITY_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the bypassed (unconditional-raise) outcomes.

    Args:
        module_text: The oracle module source. ``None`` reads *module_path* off
            the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the oracle module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries both the ``if block_authority is BlockAuthority.BLOCKING`` gate
        and a ``raise LifecycleError``; otherwise ``failure`` is
        :attr:`GateFailure.JURY_BLOCK_AUTHORITY_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    gated = _BLOCK_AUTHORITY_GATE_RE.search(text) is not None
    raises = _JURY_VETO_RAISE_RE.search(text) is not None
    if gated and raises:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (oracle jury branch consults "
                "block_authority before raising -- the veto is earned, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.JURY_BLOCK_AUTHORITY_IDLE,
        message=(
            "jury block authority is idle: "
            f"{module_path} gated={gated} raises={raises} (expected both True); the "
            "jury branch must test 'block_authority is BlockAuthority.BLOCKING' "
            "before it raises LifecycleError -- the earned-authority gate was bypassed"
        ),
    )


def check_validate_jury_cli_wired(
    *,
    module_text: str | None = None,
    module_path: str = _METRICS_CLI_MODULE,
) -> GateResult:
    """Assert ``validate_jury`` has a live CLI caller (I09-W07).

    The TRUST-7 binding: the ``eawf metrics jury-validation`` command
    (in :data:`_METRICS_CLI_MODULE`) binds
    :func:`eawf.observability.eval.jury_validation.validate_jury` directly, so the
    jury-validation reducer is reachable from the operator surface (it renders the
    honest insufficient-signal banner today and a scored report the moment a
    labelled cohort lands). If a later refactor drops the CLI caller (orphaning
    the reducer back to a never-invoked function), the source no longer matches
    and this gate fails :attr:`GateFailure.VALIDATE_JURY_CLI_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the orphaned outcomes.

    Args:
        module_text: The metrics CLI module source. ``None`` reads *module_path*
            off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the metrics CLI module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a direct ``validate_jury(`` call; otherwise ``failure`` is
        :attr:`GateFailure.VALIDATE_JURY_CLI_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _VALIDATE_JURY_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (validate_jury has a live CLI caller -- "
                "the jury-validation reducer is reachable from the operator surface)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.VALIDATE_JURY_CLI_IDLE,
        message=(
            "validate_jury is idle: "
            f"{module_path} carries no direct validate_jury(...) call, so the "
            "jury-validation reducer has no CLI caller (the metrics command "
            "regressed and orphaned the reducer)"
        ),
    )


# =========================================================================== #
# I11 Track bindings: the add/switch RPCs and the silent phase-tag stamp.
# =========================================================================== #

#: The daemon method module that registers the ``track.add`` / ``track.switch``
#: RPCs (I11-W02). Read off the working tree; the CLI ``track add`` / ``track
#: switch`` shims route their mutations here over JSON-RPC, so a dropped
#: registration leaves the Track add/switch seam without a daemon caller and
#: re-idles it. A test injects the text to drive both outcomes.
_TRACK_RPC_MODULE = "src/eawf/runtime/daemon/methods/state.py"

#: The lifecycle module whose ``open_phase`` silently stamps every phase with the
#: current Track id (I11-W03). Read off the working tree; if the stamp stops
#: firing, phases stop tagging their owning Track and the binding is idle.
_PHASE_TRACK_TAG_MODULE = "src/eawf/workflow/lifecycle/phase.py"

#: The ``@register("track.add")`` decorator that binds the add RPC into the
#: daemon method table. The literal command token is what distinguishes a live
#: registration from a docstring / cross-link mention of ``track.add``.
_TRACK_ADD_REGISTER_RE = re.compile(r"""@register\(\s*['"]track\.add['"]\s*\)""")

#: The ``@register("track.switch")`` decorator that binds the switch RPC.
_TRACK_SWITCH_REGISTER_RE = re.compile(r"""@register\(\s*['"]track\.switch['"]\s*\)""")

#: The silent phase-tag stamp: ``open_phase`` constructs each ``Phase`` with
#: ``track_id=state.current.track_id``. A regression that drops the keyword
#: (constructing the phase with no Track tag) leaves phases untagged and reds
#: this row.
_PHASE_TRACK_TAG_RE = re.compile(r"\btrack_id\s*=\s*state\.current\.track_id\b")


def check_track_rpc_wired(
    *,
    module_text: str | None = None,
    module_path: str = _TRACK_RPC_MODULE,
) -> GateResult:
    """Assert the daemon registers the ``track.add`` / ``track.switch`` RPCs (I11-W02).

    The TRACK-2 binding: the daemon method table registers both
    :func:`eawf.runtime.daemon.methods.state.track_add_rpc` (``@register("track.add")``)
    and :func:`eawf.runtime.daemon.methods.state.track_switch_rpc`
    (``@register("track.switch")``), so the CLI ``track add`` / ``track switch``
    shims have a daemon caller to route their mutations through. If a later
    refactor drops either registration (orphaning the add/switch seam so the CLI
    shim has no RPC to dispatch to), the source no longer matches and this gate
    fails :attr:`GateFailure.TRACK_RPC_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (registration-dropped)
    outcomes.

    Args:
        module_text: The daemon method module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the daemon method module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        registers BOTH ``track.add`` and ``track.switch``; otherwise ``failure``
        is :attr:`GateFailure.TRACK_RPC_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    registers_add = _TRACK_ADD_REGISTER_RE.search(text) is not None
    registers_switch = _TRACK_SWITCH_REGISTER_RE.search(text) is not None
    if registers_add and registers_switch:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (daemon registers track.add + track.switch "
                "RPCs -- the Track add/switch seam has a daemon caller, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.TRACK_RPC_IDLE,
        message=(
            "track add/switch RPCs are idle: "
            f"{module_path} registers_add={registers_add} "
            f"registers_switch={registers_switch} (expected both True); the daemon "
            "must @register('track.add') AND @register('track.switch') -- a dropped "
            "registration orphans the Track add/switch seam from its CLI shim"
        ),
    )


def check_phase_track_tag_wired(
    *,
    module_text: str | None = None,
    module_path: str = _PHASE_TRACK_TAG_MODULE,
) -> GateResult:
    """Assert ``open_phase`` silently stamps each phase with the Track id (I11-W03).

    The TRACK-3 binding: :func:`eawf.workflow.lifecycle.phase.open_phase`
    constructs every :class:`~eawf.kernel.state.models.Phase` with
    ``track_id=state.current.track_id``, so a phase opened while a Track is in
    focus is silently tagged with its owning Track. If a later refactor drops the
    stamp (constructing the phase with no Track tag), phases stop tagging their
    Track and the silent phase-tag binding regresses to idle -- the source no
    longer matches and this gate fails :attr:`GateFailure.PHASE_TRACK_TAG_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (stamp-dropped) outcomes.

    Args:
        module_text: The lifecycle phase module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the phase module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a ``track_id=state.current.track_id`` phase-construction stamp;
        otherwise ``failure`` is :attr:`GateFailure.PHASE_TRACK_TAG_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _PHASE_TRACK_TAG_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (open_phase stamps track_id=state.current."
                "track_id -- phases silently tag their owning Track, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.PHASE_TRACK_TAG_IDLE,
        message=(
            "phase track-tag stamp is idle: "
            f"{module_path} carries no 'track_id=state.current.track_id' phase "
            "construction stamp, so open_phase no longer tags phases with their "
            "owning Track (the silent phase-tag binding regressed)"
        ),
    )


# =========================================================================== #
# P30-I17 live-autopilot bindings: the drive ladders + the live stdout fan.
# =========================================================================== #

#: The daemon fleet module whose ``start_background_drive`` arms the LIVE drive
#: (I17-W11). Read off the working tree; the two production ``arm_drive`` calls
#: must each pass ``classify`` + ``repair`` so the bounded spawn ladder (DL-11)
#: and the grounded repair ladder (DL-7) fire on a real autopilot run -- without
#: the kwargs ``classify is None`` / ``repair is None`` and both ladders stay
#: dormant (they only fire when a test injects the hooks).
_LIVE_DRIVE_MODULE = "src/eawf/runtime/daemon/methods/fleet.py"

#: The live wave-spawn dispatch module whose ``_spawn_and_dispatch`` threads the
#: captured agent answer into the W08 stdout producer (I17-W11). Read off the
#: working tree; without ``output_text=spawn_result.text`` the producer is wired
#: into ``run_dispatch`` but no live caller supplies it, so the agent-watch live
#: tail stays empty on a real spawn.
_LIVE_OUTPUT_MODULE = "src/eawf/runtime/daemon/methods/agent.py"

#: The live drive arms ``arm_drive`` with the production error classifier so the
#: bounded spawn ladder fires; a regression that drops the kwarg re-dormants it.
_DRIVE_CLASSIFY_RE = re.compile(r"\bclassify\s*=\s*_live_lane_error_classifier\b")

#: The live drive arms ``arm_drive`` with the live grounded-repair hook so the
#: bounded repair ladder fires; a regression that drops the kwarg re-dormants it.
_DRIVE_REPAIR_RE = re.compile(r"\brepair\s*=\s*repair_hook\b")

#: The live spawn threads its captured answer text into the stdout producer.
_LIVE_OUTPUT_TEXT_RE = re.compile(r"\boutput_text\s*=\s*spawn_result\.text\b")

# =========================================================================== #
# P30-I18 campaign-run bindings: the state.claims fold + the L1 carryover prune.
# =========================================================================== #

#: The daemon research-methods module whose ``run_campaign`` folds each round's
#: reconciled claims into the canonical ``state.claims`` (W05/W06) and runs the
#: L1 carryover prune between rounds (W06). Read off the working tree; if either
#: binding regresses to the throwaway-shadow / no-prune path the source no longer
#: matches and the corresponding row reds.
_CAMPAIGN_RUN_MODULE = "src/eawf/runtime/daemon/methods/research.py"

#: ``run_campaign`` folds the reconciled claims into REAL state through the
#: daemon-owned canonical writer (``_commit_worktree_state``), the same path
#: ``add_question`` uses for ``state.open_questions``. A regression that reverts
#: to the throwaway ``State.model_construct(claims={}, ...)`` shadow as the ONLY
#: reconcile path drops this call and re-idles the fold.
_CAMPAIGN_CLAIM_FOLD_RE = re.compile(r"\b_commit_worktree_state\s*\(")

#: ``run_campaign`` runs ``reconcile_round_claims`` through that canonical writer
#: by binding it inside the ``apply_func`` the writer calls -- the fold mutates
#: the real ``state.claims``, not a shadow.
_CAMPAIGN_RECONCILE_FOLD_RE = re.compile(r"\breconcile_round_claims\s*\(\s*state\b")

#: ``run_campaign`` calls the L1 between-rounds carryover reducer so the next
#: round + the synthesis work over only the live claims. A regression that drops
#: the call leaves the reducer with zero production callers (its prior state).
_CAMPAIGN_CARRYOVER_CALL_RE = re.compile(r"\bprune_round_carryover\s*\(")


def check_campaign_claim_fold_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CAMPAIGN_RUN_MODULE,
) -> GateResult:
    """Assert ``run_campaign`` folds reconciled claims into canonical state (W05/W06).

    The P30-I18 binding: :func:`eawf.runtime.daemon.methods.research.run_campaign`
    folds each round's reconciled claims into the REAL ``state.claims`` through
    the daemon-owned canonical writer
    (:func:`eawf.runtime.daemon.methods.state._commit_worktree_state`, the same
    path ``add_question`` uses for ``state.open_questions``), by binding
    :func:`reconcile_round_claims` against the live ``state`` inside the writer's
    ``apply_func``. Before the binding the reconcile ran only against a throwaway
    ``State.model_construct(claims={}, ...)`` shadow, so a live round never wrote
    a Claim row to ``state.claims`` -- only ``claim_ids`` landed on the round
    record. If a later refactor reverts to the shadow-only path the source no
    longer matches both anchors and this gate fails
    :attr:`GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (shadow-only) outcomes.

    Args:
        module_text: The campaign-run module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the campaign-run module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both calls ``_commit_worktree_state(`` and reconciles against the live
        ``state`` (``reconcile_round_claims(state``); otherwise ``failure`` is
        :attr:`GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    commits = _CAMPAIGN_CLAIM_FOLD_RE.search(text) is not None
    reconciles_state = _CAMPAIGN_RECONCILE_FOLD_RE.search(text) is not None
    if commits and reconciles_state:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (run_campaign folds reconciled claims into "
                "state.claims via _commit_worktree_state -- a live round populates "
                "the canonical claim ledger, not a throwaway shadow)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE,
        message=(
            "campaign claim fold is idle: "
            f"{module_path} commits={commits} reconciles_state={reconciles_state} "
            "(expected both True); run_campaign must reconcile each round into the "
            "live state via _commit_worktree_state -- the fold regressed to the "
            "throwaway State.model_construct shadow, so state.claims stays empty"
        ),
    )


def check_campaign_carryover_prune_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CAMPAIGN_RUN_MODULE,
) -> GateResult:
    """Assert ``run_campaign`` calls the L1 carryover prune between rounds (W06).

    The P30-I18 binding: :func:`eawf.runtime.daemon.methods.research.run_campaign`
    calls :func:`eawf.kernel.spec.pruning.prune_round_carryover` over the
    accumulated claim ledger between rounds, so the next round + the synthesis
    work over only the live claims (the provably-dead rows drop). Before the
    binding the L1 reducer had ZERO production callers -- a built-but-idle
    contract the P30 thesis forbids. If a later refactor drops the call the
    source no longer matches and this gate fails
    :attr:`GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (no-caller) outcomes.

    Args:
        module_text: The campaign-run module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the campaign-run module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a ``prune_round_carryover(`` call; otherwise ``failure`` is
        :attr:`GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _CAMPAIGN_CARRYOVER_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (run_campaign calls prune_round_carryover "
                "between rounds -- the L1 reducer has a production caller, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE,
        message=(
            "campaign carryover prune is idle: "
            f"{module_path} carries no prune_round_carryover(...) call, so the L1 "
            "between-rounds reducer has zero production callers (the run never prunes "
            "the carried claim ledger -- the contract ships built-but-idle)"
        ),
    )


def check_drive_ladders_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_DRIVE_MODULE,
) -> GateResult:
    """Assert the LIVE drive enables the spawn + repair ladders (I17-W11).

    The W11 binding: :func:`eawf.runtime.daemon.methods.fleet.start_background_drive`
    arms ``arm_drive`` with the production
    :func:`~eawf.runtime.daemon.methods.fleet._live_lane_error_classifier`
    (``classify=``) and the live
    :func:`~eawf.runtime.daemon.methods.fleet._build_live_lane_repair_hook` hook
    (``repair=``), so the bounded spawn ladder (DL-11, :func:`spawn_lane_or_fork`)
    and the bounded grounded repair ladder (DL-7, :func:`repair_lane_or_fork`)
    FIRE on a real autopilot run. With either kwarg dropped the loop takes the
    direct-spawn / terminal-fork path -- the ladders ship built-but-dormant on
    the live path (they fire only when a test injects the hooks). If a later
    refactor drops either binding the source no longer matches and this gate
    fails :attr:`GateFailure.DRIVE_LADDERS_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-dormant outcomes.

    Args:
        module_text: The fleet module source. ``None`` reads *module_path* off
            the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the fleet module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        arms the drive with BOTH ``classify=_live_lane_error_classifier`` and
        ``repair=repair_hook``; otherwise ``failure`` is
        :attr:`GateFailure.DRIVE_LADDERS_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    wires_classify = _DRIVE_CLASSIFY_RE.search(text) is not None
    wires_repair = _DRIVE_REPAIR_RE.search(text) is not None
    if wires_classify and wires_repair:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live drive arms classify + repair -- the "
                "bounded spawn ladder (DL-11) + grounded repair ladder (DL-7) fire "
                "on a real run, not just under test)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.DRIVE_LADDERS_IDLE,
        message=(
            "live drive ladders are idle: "
            f"{module_path} wires_classify={wires_classify} wires_repair={wires_repair} "
            "(expected both True); start_background_drive must arm arm_drive with "
            "classify=_live_lane_error_classifier AND repair=repair_hook -- a dropped "
            "kwarg re-dormants the spawn / repair ladder on the live autopilot run"
        ),
    )


def check_live_output_text_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_OUTPUT_MODULE,
) -> GateResult:
    """Assert the live wave spawn supplies ``output_text`` to the stdout producer (I17-W11).

    The W11 binding: the live wave-spawn dispatch
    (:func:`eawf.runtime.daemon.methods.agent._spawn_and_dispatch`) threads the
    spawned agent's OWN captured answer (``spawn_result.text``) into
    :func:`~eawf.runtime.daemon.dispatch_runner.run_dispatch` as
    ``output_text=``, so the W08 stdout producer emits an ``agent.output`` event
    the agent-watch live tail renders. Without the thread the producer is wired
    into ``run_dispatch`` but no live caller supplies it, so ``emit_agent_output``
    never fires on a real spawn and the tail stays empty. If a later refactor
    drops the binding the source no longer matches and this gate fails
    :attr:`GateFailure.LIVE_OUTPUT_TEXT_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-dormant outcomes.

    Args:
        module_text: The live dispatch module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the live dispatch module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        threads ``output_text=spawn_result.text`` into the live dispatch;
        otherwise ``failure`` is :attr:`GateFailure.LIVE_OUTPUT_TEXT_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _LIVE_OUTPUT_TEXT_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live spawn threads output_text=spawn_result.text "
                "-- the stdout producer fires on a real spawn, the live tail is not empty)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.LIVE_OUTPUT_TEXT_IDLE,
        message=(
            "live output_text fan is idle: "
            f"{module_path} carries no 'output_text=spawn_result.text' thread into "
            "run_dispatch, so the W08 stdout producer never fires on a real spawn "
            "(the agent-watch live tail stays empty -- the producer is wired but unfed)"
        ),
    )


def check_runtime_gate_is_not_idle(
    *,
    precommit_text: str | None = None,
    precommit_path: Path | None = None,
) -> GateResult:
    """Assert the always-run pre-commit idle-contract gate remains enabled."""
    path = precommit_path or (_REPO_ROOT / ".pre-commit-config.yaml")
    text = precommit_text if precommit_text is not None else path.read_text()
    required = (
        "id: idle-contract-gate",
        "entry: uv run python tools/idle_contract_gate.py",
        "pass_filenames: false",
        "always_run: true",
        "stages: [pre-commit]",
    )
    missing = [snippet for snippet in required if snippet not in text]
    if missing:
        return GateResult(
            passed=False,
            failure=GateFailure.RUNTIME_GATE_IDLE,
            message=f"runtime close gate idle: pre-commit binding missing {missing!r}",
        )
    return GateResult(
        passed=True,
        failure=None,
        message="runtime close gate binding: ok (idle-contract-gate always runs at pre-commit)",
    )


# =========================================================================== #
# Registry-wide sweep: every registered audit-DSL kind must be wired on.
# =========================================================================== #

#: A registered-kinds source with the shape of
#: :func:`eawf.workflow.audit_dsl.registry.registered_audit_dsl_kinds`. Injected
#: so a test can drive the sweep with a synthetic kind set (e.g. one re-idled
#: kind) without editing the real registry.
type RegisteredKindsFn = Callable[[], frozenset[str]]

#: A wired-kinds source with the shape of
#: :func:`eawf.workflow.verify.readiness.wired_audit_dsl_kinds`. Injected so a
#: test can simulate a kind losing its production binding (re-idling) and assert
#: the sweep reds.
type WiredKindsFn = Callable[[], frozenset[str]]


def check_audit_dsl_kinds_wired(
    *,
    registered_fn: RegisteredKindsFn = registered_audit_dsl_kinds,
    wired_fn: WiredKindsFn = wired_audit_dsl_kinds,
) -> GateResult:
    """Assert every registered audit-DSL kind has a production binding.

    The wired-on sweep (P30-I10 QUAL-2) generalizes the B091 idle-verifier
    lesson to the audit-DSL kind registry: a kind that is registered (so it
    advertises itself as a falsifier) but has no production binding -- no
    oracle-tier mapping and no close-gate wiring -- can never be escalated to
    by the live close gate, so it ships registered-but-idle exactly like the
    spec-jury producer did.

    The check compares the full registered set (*registered_fn*, the
    :data:`CHECK_REGISTRY` keys plus the state-scoring close-gate kinds)
    against the wired set (*wired_fn*, the kernel ``_GATE_KIND_TIER`` map plus
    the supplemental checkout-gate tiers plus the close-gate kinds). Any
    registered kind absent from the wired set is idle and reds CI, naming the
    offending kind(s).

    Both sources are injectable so a test can drive the re-idle failure mode
    (a wired set missing a registered kind) without editing the real registry
    or tier map -- mirroring how :func:`check_idle_contract` injects its
    ``profiles`` / ``resolve_fn``. The check reads only -- it never mutates
    state, never writes a file, and never runs a mutating ``eawf`` command.

    Args:
        registered_fn: Source of the full registered kind set. Defaults to
            the live :func:`registered_audit_dsl_kinds`.
        wired_fn: Source of the production-wired kind set. Defaults to the
            live :func:`wired_audit_dsl_kinds`.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when every
        registered kind is wired; otherwise ``failure`` is
        :attr:`GateFailure.AUDIT_DSL_KIND_IDLE` and the message names the
        idle kind(s).
    """
    registered = registered_fn()
    wired = wired_fn()
    idle = sorted(registered - wired)
    if idle:
        return GateResult(
            passed=False,
            failure=GateFailure.AUDIT_DSL_KIND_IDLE,
            message=(
                f"audit-DSL kind(s) ship registered-but-idle: {', '.join(idle)} "
                "-- a registered kind needs an oracle-tier mapping or a close-gate "
                "wiring (no production caller means the close gate can never "
                "escalate to it)"
            ),
        )
    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"idle-contract gate: ok (all {len(registered)} registered audit-DSL "
            "kinds are wired on -- each has an oracle-tier or close-gate binding)"
        ),
    )


# =========================================================================== #
# Meta-gate: detect a newly-defined contract that ships idle in a diff.
# =========================================================================== #

#: Repo root, derived once from this file's location (``tools/`` is a sibling
#: of ``src/`` and ``tests/``). The tree-scan default reads files relative to
#: this root so the gate works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Contract-family detectors over a single added source line. The meta-gate is
#: scoped tightly to these families so a plain internal helper (e.g.
#: ``def _coerce_row(...)``) is never flagged. Each pattern captures the
#: contract symbol name in group ``sym`` so the finding can name the orphan.
#:
#: - ``check_*`` / ``*_gate`` / ``*_lint`` function defs are the gate / lint /
#:   validator family.
#: - a ``@register(...)`` / ``@register_check(...)`` decorator whose argument is
#:   a ``CheckKind`` string token registers a check runner; the decorated name
#:   is the contract.
#: - a new ``OracleTier.T<n>_*`` dispatch arm is an oracle-tier branch.
_CONTRACT_DEF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*def\s+(?P<sym>check_[A-Za-z0-9_]+)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_gate)\s*\("),
    re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+_lint)\s*\("),
)

#: A ``CheckKind``-runner registration: a ``@register(...)`` /
#: ``@register_check(...)`` decorator line that names a ``CheckKind`` token. The
#: decorated def on the following added line carries the contract symbol.
_CHECKKIND_DECORATOR_RE = re.compile(r"^\s*@register(?:_check)?\s*\(.*CheckKind.*\)\s*$")

#: A decorated def line (used to recover the symbol a ``CheckKind`` decorator
#: registers).
_DECORATED_DEF_RE = re.compile(r"^\s*def\s+(?P<sym>[A-Za-z0-9_]+)\s*\(")

#: A new oracle-tier dispatch arm: a reference to an ``OracleTier.T<n>_<NAME>``
#: member in an added line. The member token is the contract symbol.
_ORACLE_TIER_RE = re.compile(r"OracleTier\.(?P<sym>T\d+_[A-Z0-9_]+)")

#: A new ``eawf0##_*.py`` lint rule module under the lint package. The file's
#: rule id (``eawf0##``) is the contract symbol.
_LINT_MODULE_RE = re.compile(r"^src/eawf/platform/lint/(?P<sym>eawf0\d\d)_[A-Za-z0-9_]+\.py$")

#: A unified-diff hunk-header carrying the file path of the *added* side.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$")

#: The unified-diff ``--- /dev/null`` old-side marker, present only when the
#: file is genuinely added (a modified file's old side is ``--- a/<path>``).
#: The lint-module path detector keys on this so a mere edit of an existing
#: ``eawf0##_*.py`` module is not mistaken for a new lint contract -- only a
#: brand-new module file registers the rule-id contract. ``git diff`` also
#: emits a ``new file mode`` line for an addition, but ``--- /dev/null`` is the
#: portable signal both real git and the test fixtures carry.
_NEW_FILE_OLD_SIDE_RE = re.compile(r"^--- /dev/null$")

#: Repo-relative path prefixes a contract may legitimately be DEFINED under.
#: A contract is defined in shipped source (``src/``) or a gate script
#: (``tools/``); a ``tests/`` file that constructs a contract-shaped token is a
#: test fixture (or the discharge itself), never a new contract, so the parser
#: ignores added lines in test files to avoid flagging its own fixtures.
_SOURCE_PREFIXES: tuple[str, ...] = ("src/", "tools/")

#: An added line in a unified diff (``+`` prefix, but not the ``+++`` header).
_ADDED_LINE_RE = re.compile(r"^\+(?!\+\+ )(?P<body>.*)$")


class MissingDischarge(StrEnum):
    """Which idle-contract discharge a finding reports as missing.

    A contract is discharged only when it is both *called* (a call-site outside
    its defining module proves it runs in production) AND *asserted* (a test
    references it so a regression is caught). The order is informational only.
    """

    NO_CALL_SITE = "no_call_site"
    NO_ASSERTING_TEST = "no_asserting_test"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class IdleContractFinding:
    """A newly-defined contract that ships idle (an orphan).

    Attributes:
        symbol: The contract symbol name (function / tier member / rule id).
        module: The repo-relative path of the file defining the contract.
        missing: Which discharge is absent -- no call-site, no asserting test,
            or both.
    """

    symbol: str
    module: str
    missing: MissingDischarge


@dataclass(frozen=True, slots=True)
class _ContractDef:
    """An added contract definition parsed out of a diff (internal).

    Attributes:
        symbol: The contract symbol name to chase for discharges.
        module: The repo-relative path of the defining file.
    """

    symbol: str
    module: str


#: A diff source: returns the unified-diff text for a rev range. Injected so
#: tests feed a synthetic diff without a git repo (mirrors how
#: :func:`check_idle_contract` injects ``profiles`` / ``resolve_fn``).
type DiffFn = Callable[[str], str]

#: A tree-scan source: returns the repo-relative paths of every source / test
#: file in the working tree. Injected alongside :data:`ReadFn` so tests feed a
#: synthetic tree without touching the real one.
type TreeFn = Callable[[], Sequence[str]]

#: A file-read source: returns the text of a repo-relative path. Injected so
#: the discharge scan reads the synthetic tree the test built.
type ReadFn = Callable[[str], str]


def _default_diff(diff_range: str) -> str:
    """Return the unified diff for *diff_range* via git.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or a flag the diff
            subcommand accepts (``--cached``).

    Returns:
        The unified-diff text. Empty when the range has no changes.
    """
    proc = subprocess.run(
        ["git", "diff", "--unified=0", diff_range],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _default_tree() -> list[str]:
    """Return the repo-relative paths of tracked Python sources and tests.

    Returns:
        Every ``.py`` path under ``src/`` and ``tests/`` plus the ``tools/``
        gate scripts, repo-relative and sorted.
    """
    paths: list[str] = []
    for top in ("src", "tests", "tools"):
        root = _REPO_ROOT / top
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            paths.append(str(path.relative_to(_REPO_ROOT)))
    return sorted(paths)


def _default_read(path: str) -> str:
    """Return the text of repo-relative *path*, or empty if it is unreadable.

    Args:
        path: A repo-relative file path.

    Returns:
        The file text, or ``""`` when the file is absent (e.g. a path that was
        renamed away after the diff was taken).
    """
    target = _REPO_ROOT / path
    if not target.is_file():
        return ""
    return target.read_text(encoding="utf-8", errors="replace")


def _family_symbols_on_line(body: str) -> list[str]:
    """Return the contract symbols a single added source line defines.

    Recognizes a :data:`_CONTRACT_DEF_PATTERNS` ``def`` family (``check_*`` /
    ``*_gate`` / ``*_lint``) and an :data:`_ORACLE_TIER_RE` tier arm. A plain
    helper line matches nothing, so it yields an empty list.

    Args:
        body: The added line's text (the ``+`` prefix already stripped).

    Returns:
        The contract symbol names found on the line, in detection order.
    """
    symbols: list[str] = []
    for pattern in _CONTRACT_DEF_PATTERNS:
        family = pattern.match(body)
        if family is not None:
            symbols.append(family.group("sym"))
            break
    tier = _ORACLE_TIER_RE.search(body)
    if tier is not None:
        symbols.append(tier.group("sym"))
    return symbols


def _parse_added_contract_defs(diff_text: str) -> list[_ContractDef]:
    """Parse the contract-family symbols newly defined in *diff_text*.

    Only added lines (``+`` prefixed) under a :data:`_SOURCE_PREFIXES` path are
    considered, and only those that match a :data:`_CONTRACT_DEF_PATTERNS`
    family, a ``CheckKind``-runner decorator, an :data:`_ORACLE_TIER_RE` arm, or
    a new :data:`_LINT_MODULE_RE` module. Plain helpers never match, and added
    lines under ``tests/`` are skipped (a contract-shaped token in a test is a
    fixture, not a new contract), so neither becomes a finding.

    Args:
        diff_text: A unified diff (``git diff`` output).

    Returns:
        The de-duplicated contract definitions, in first-seen order.
    """
    parser = _DiffContractParser()
    for raw in diff_text.splitlines():
        parser.feed(raw)
    return parser.defs


@dataclass(slots=True)
class _DiffContractParser:
    """A line-at-a-time state machine that collects added contract defs.

    The diff walk is a small state machine: a ``--- /dev/null`` old-side marker
    flags the next file as a genuine addition, a ``+++ b/<path>`` header sets
    the file scope (and, when the file is newly added, may itself name a
    lint-module contract), then each added line in a source file is matched
    against the contract families. The ``CheckKind`` decorator/def pairing
    spans two lines, so the pending flag carries that one bit of state between
    :meth:`feed` calls.

    Attributes:
        defs: The de-duplicated contract definitions collected so far.
    """

    defs: list[_ContractDef] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)
    _current_file: str = ""
    _pending_checkkind: bool = False
    _next_file_is_new: bool = False

    def feed(self, raw: str) -> None:
        """Advance the state machine by one raw diff line.

        Args:
            raw: A single line of unified-diff text.
        """
        if _NEW_FILE_OLD_SIDE_RE.match(raw) is not None:
            self._next_file_is_new = True
            return
        file_match = _DIFF_FILE_RE.match(raw)
        if file_match is not None:
            self._enter_file(file_match.group("path"))
            return

        # Only added lines in shipped source / gate scripts define a contract;
        # a contract-shaped token added under tests/ is a fixture, never a new
        # contract (this is what keeps the meta-gate from flagging itself).
        if not self._current_file.startswith(_SOURCE_PREFIXES):
            return
        added = _ADDED_LINE_RE.match(raw)
        if added is not None:
            self._feed_added(added.group("body"))

    def _enter_file(self, path: str) -> None:
        self._current_file = path
        self._pending_checkkind = False
        # A lint-module contract is keyed on the file path, so it is "new" only
        # when the module file itself is newly added -- editing an existing
        # eawf0##_*.py module adds no new rule-id contract.
        is_new = self._next_file_is_new
        self._next_file_is_new = False
        if not is_new:
            return
        lint_match = _LINT_MODULE_RE.match(path)
        if lint_match is not None:
            self._record(lint_match.group("sym"))

    def _feed_added(self, body: str) -> None:
        # A def on the line right after a CheckKind decorator is the registered
        # runner; a non-def line breaks the adjacency and falls through.
        if self._pending_checkkind:
            self._pending_checkkind = False
            decorated = _DECORATED_DEF_RE.match(body)
            if decorated is not None:
                self._record(decorated.group("sym"))
                return
        if _CHECKKIND_DECORATOR_RE.match(body) is not None:
            self._pending_checkkind = True
            return
        for symbol in _family_symbols_on_line(body):
            self._record(symbol)

    def _record(self, symbol: str) -> None:
        key = (symbol, self._current_file)
        if key not in self._seen:
            self._seen.add(key)
            self.defs.append(_ContractDef(symbol=symbol, module=self._current_file))


def _wired_through_own_main(symbol: str, defining_module: str, read_fn: ReadFn) -> bool:
    """Return whether a ``tools/`` gate script wires *symbol* through its own ``main``.

    A ``src/`` contract proves it runs via a production caller in another module,
    but a ``tools/`` gate script's production caller IS its own ``main`` (the
    pre-commit hook invokes ``python tools/<gate>.py``, which runs ``main``).
    A gate-check function referenced inside that module's ``main`` body is
    therefore genuinely wired on, not idle -- so this same-module wiring counts
    as a call-site for a ``tools/`` script (and only there; a ``src/`` module's
    ``main`` does not, since shipped contracts must run from production code).

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when *defining_module* is a ``tools/`` script whose ``main``
        function body references *symbol*.
    """
    if not defining_module.startswith("tools/"):
        return False
    text = read_fn(defining_module)
    main_match = re.search(r"^def main\(", text, flags=re.MULTILINE)
    if main_match is None:
        return False
    # The main body runs to the next top-level def/class or the module guard.
    tail = text[main_match.start() :]
    end_match = re.search(r"\n(?:def |class |if __name__)", tail[1:])
    main_body = tail if end_match is None else tail[: end_match.start() + 1]
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    # The def line itself is not a call; only a reference in the body counts.
    body_after_signature = main_body.split("\n", 1)[1] if "\n" in main_body else ""
    return needle.search(body_after_signature) is not None


def _has_call_site(symbol: str, defining_module: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether *symbol* is referenced in a non-test file other than its module.

    A call-site outside the defining module proves the contract runs in
    production (a same-module self-reference does not discharge a ``src/``
    contract, and a test reference is counted separately as the asserting-test
    discharge). The one same-module exception is a ``tools/`` gate script that
    wires the check through its own ``main`` -- that IS the production caller for
    a gate script (see :func:`_wired_through_own_main`).

    Args:
        symbol: The contract symbol to chase.
        defining_module: The repo-relative path of the file that defines it.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some non-test, non-defining file references *symbol*, or
        when a ``tools/`` gate script wires it through its own ``main``.
    """
    if _wired_through_own_main(symbol, defining_module, read_fn):
        return True
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in tree:
        if path == defining_module:
            continue
        if path.startswith("tests/") or "/tests/" in path:
            continue
        if needle.search(read_fn(path)):
            return True
    return False


def _has_asserting_test(symbol: str, tree: Iterable[str], read_fn: ReadFn) -> bool:
    """Return whether a test file references *symbol*.

    Pragmatically, a test under ``tests/`` that imports or references the
    symbol name discharges the asserting-test contract: the symbol is pulled
    into a test module's namespace, so a regression has somewhere to fail.

    Args:
        symbol: The contract symbol to chase.
        tree: The repo-relative paths to scan.
        read_fn: Reader for a repo-relative path.

    Returns:
        ``True`` when some ``tests/`` file references *symbol*.
    """
    needle = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in tree:
        if not (path.startswith("tests/") or "/tests/" in path):
            continue
        if needle.search(read_fn(path)):
            return True
    return False


def detect_idle_contracts(
    diff_range: str = "--cached",
    *,
    diff_fn: DiffFn = _default_diff,
    tree_fn: TreeFn = _default_tree,
    read_fn: ReadFn = _default_read,
) -> list[IdleContractFinding]:
    """Flag every newly-defined contract in *diff_range* that ships idle.

    The meta-gate parses the contract-family symbols a diff adds (see
    :func:`_parse_added_contract_defs`), then for each chases two discharges in
    the resulting working tree: a call-site outside the defining module
    (proves it runs) and an asserting test that references it (proves a
    regression is caught). A contract missing either discharge is an orphan and
    yields one :class:`IdleContractFinding`; a contract with BOTH yields none.

    The diff, tree, and read sources are injectable so tests feed a synthetic
    diff + tree without a git repo, mirroring how :func:`check_idle_contract`
    injects its ``profiles`` / ``resolve_fn``. The function mutates nothing --
    it only reads.

    Args:
        diff_range: A git rev range (``HEAD~1..HEAD``) or the staged-diff flag
            (``--cached``, the default so the pre-commit hook needs no args).
        diff_fn: Diff source; defaults to ``git diff --unified=0 <range>``.
        tree_fn: Tree-scan source; defaults to the tracked ``src`` / ``tests``
            / ``tools`` Python files.
        read_fn: File reader; defaults to reading the working-tree file.

    Returns:
        One :class:`IdleContractFinding` per orphan contract, in the order the
        defs appear in the diff. Empty when every added contract is discharged
        (or the diff adds no contract).
    """
    contract_defs = _parse_added_contract_defs(diff_fn(diff_range))
    if not contract_defs:
        return []

    tree = list(tree_fn())
    findings: list[IdleContractFinding] = []
    for contract in contract_defs:
        has_call = _has_call_site(contract.symbol, contract.module, tree, read_fn)
        has_test = _has_asserting_test(contract.symbol, tree, read_fn)
        if has_call and has_test:
            continue
        if not has_call and not has_test:
            missing = MissingDischarge.BOTH
        elif not has_call:
            missing = MissingDischarge.NO_CALL_SITE
        else:
            missing = MissingDischarge.NO_ASSERTING_TEST
        findings.append(
            IdleContractFinding(
                symbol=contract.symbol,
                module=contract.module,
                missing=missing,
            )
        )
    return findings


def _render_findings(findings: Sequence[IdleContractFinding]) -> str:
    """Render *findings* as a human-readable multi-line failure message.

    Args:
        findings: The orphan contracts to describe.

    Returns:
        One line per finding, each naming the symbol, its module, and the
        missing discharge.
    """
    lines = [f"idle-contract meta-gate: {len(findings)} new contract(s) ship idle:"]
    for finding in findings:
        lines.append(
            f"  - {finding.symbol} (in {finding.module}) is missing {finding.missing.value}; "
            "a new contract needs a call-site outside its module AND an asserting test"
        )
    return "\n".join(lines)


def _report_result(result: GateResult) -> bool:
    """Print *result* to the right stream and report whether it failed.

    A passing result prints its message to stdout; a failing one prints to
    stderr. Folding the print + stream choice here keeps :func:`main` a flat
    sequence of ``failed |= _report_result(...)`` calls instead of repeating the
    ``if result.passed: ... else: ...`` block once per gate.

    Args:
        result: The gate outcome to report.

    Returns:
        ``True`` when *result* failed (so the caller can fold it into the
        aggregate exit status), ``False`` on a pass.
    """
    if result.passed:
        print(result.message)
        return False
    print(result.message, file=sys.stderr)
    return True


def main(argv: list[str]) -> int:
    """Run all idle-contract gates over the staged diff and current tree.

    The original B091 single-contract check (:func:`check_idle_contract`) runs
    first, then the emit-validation binding-proof probe
    (:func:`check_skill_body_binding`), then the I03 contract probes
    (:func:`check_i03_contracts`), then the resolve_routing wiring probe
    (:func:`check_resolve_routing_wired`), then the four I09 jury-validation
    binding probes (:func:`check_spec_jury_ballot_fn_wired`,
    :func:`check_jury_reliability_map_wired`,
    :func:`check_jury_block_authority_wired`,
    :func:`check_validate_jury_cli_wired`), then the two I11 Track binding probes
    (:func:`check_track_rpc_wired`, :func:`check_phase_track_tag_wired`), then the
    two I17 live-autopilot binding probes (:func:`check_drive_ladders_wired`,
    :func:`check_live_output_text_wired`), then the two P30-I18 campaign-run
    binding probes (:func:`check_campaign_claim_fold_wired`,
    :func:`check_campaign_carryover_prune_wired`), then the
    runtime-gate binding check (:func:`check_runtime_gate_is_not_idle`), then the
    registry-wide audit-DSL wired-on sweep
    (:func:`check_audit_dsl_kinds_wired`), then the meta-gate
    (:func:`detect_idle_contracts`) over the staged diff. All must pass; the
    exit code is non-zero when any fails.

    Args:
        argv: Process argv. ``argv[1]``, when present, overrides the default
            ``--cached`` diff range fed to the meta-gate (e.g. ``HEAD~1..HEAD``
            for a CI range check).

    Returns:
        ``0`` when all gates pass; ``1`` when any fails.
    """
    diff_range = argv[1] if len(argv) > 1 else "--cached"

    failed = False
    failed |= _report_result(check_idle_contract())

    # Pass the module-level run_skill explicitly so a test (or a future caller)
    # can patch it via attribute assignment -- a default-bound parameter would
    # snapshot the unpatched function at def time (see the same pattern below
    # for the meta-gate's diff / tree / read sources).
    failed |= _report_result(check_skill_body_binding(run_skill_fn=_run_skill))

    failed |= _report_result(
        check_i03_contracts(
            plan_wave_fn=_plan_wave,
            ui_require_gate_fn=_require_affordance_parity_for_ui_scope,
            registry=CHECK_REGISTRY,
            tier_for_gate_kind_fn=_tier_for_gate_kind,
        )
    )

    # The live-dispatch module is read off the working tree (not the
    # monkeypatchable _default_read), so this gate stays immune to the
    # meta-gate's injected reader stub -- mirroring check_runtime_gate_is_not_idle.
    failed |= _report_result(check_resolve_routing_wired())

    # The four I09 jury-validation bindings each read their live call-site off
    # the working tree (immune to the meta-gate's reader stub, like the routing
    # probe above): the spec-jury ballot fn (W05), the reputation reliability map
    # (W06), the earned block authority (W04), and the validate_jury CLI caller
    # (W07). Any re-idle of one fails its row and reds the gate.
    #
    # The two I11 Track bindings read their live source off the working tree the
    # same way: the daemon-registered track.add / track.switch RPCs (W02) and the
    # silent open_phase track-tag stamp (W03). Either re-idle fails its row.
    #
    # The two I17 live-autopilot bindings read their live source off the working
    # tree the same way: the drive-arming classify + repair kwargs (W11) and the
    # live-spawn output_text fan (W11). Either re-dormant fails its row.
    #
    # The two P30-I18 campaign-run bindings read their live source off the working
    # tree the same way: run_campaign's state.claims fold via the canonical writer
    # (W05/W06) and its L1 carryover prune call between rounds (W06). Either
    # re-idle fails its row.
    for source_scan_check in (
        check_spec_jury_ballot_fn_wired(),
        check_jury_reliability_map_wired(),
        check_jury_block_authority_wired(),
        check_validate_jury_cli_wired(),
        check_track_rpc_wired(),
        check_phase_track_tag_wired(),
        check_drive_ladders_wired(),
        check_live_output_text_wired(),
        check_campaign_claim_fold_wired(),
        check_campaign_carryover_prune_wired(),
    ):
        failed |= _report_result(source_scan_check)

    failed |= _report_result(check_runtime_gate_is_not_idle())

    # Pass the module-level wired-on sources explicitly so a test (or a future
    # caller) can patch them via attribute assignment.
    failed |= _report_result(
        check_audit_dsl_kinds_wired(
            registered_fn=registered_audit_dsl_kinds,
            wired_fn=wired_audit_dsl_kinds,
        )
    )

    # Pass the module-level default sources explicitly so a test (or a future
    # caller) can patch them via attribute assignment -- a default-bound
    # parameter would snapshot the unpatched function at def time.
    findings = detect_idle_contracts(
        diff_range,
        diff_fn=_default_diff,
        tree_fn=_default_tree,
        read_fn=_default_read,
    )
    if findings:
        print(_render_findings(findings), file=sys.stderr)
        failed = True
    else:
        print("idle-contract meta-gate: ok (no new contract ships idle)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
