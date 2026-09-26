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
  daemon close path binds the LIVE per-item spec-jury ballot fn:
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
  BEFORE it raises ``LifecycleError``, so a veto blocks only an EARNED
  jury. Bypassing the authority gate reds this row.
- :func:`check_validate_jury_cli_wired` -- a source-scan probe asserting
  ``validate_jury`` has a live CLI caller: the ``eawf metrics
  jury-validation`` command binds it directly. Orphaning the reducer reds this
  row.
- :func:`check_phase_track_tag_wired` -- a source-scan probe asserting
  ``open_phase`` silently stamps each phase with ``track_id=state.current.track_id``
  (I11-W03), so phases tag their owning Track. Dropping the stamp re-idles the
  silent phase-tag binding and reds this row.
- :func:`check_drive_ladders_wired` -- a source-scan probe asserting the live
  drive (``start_background_drive``) arms ``arm_drive`` with both
  ``classify=_live_lane_error_classifier`` and ``repair=repair_hook``,
  so the bounded spawn ladder (DL-11) and the grounded repair ladder (DL-7) fire
  on a real autopilot run instead of staying dormant until a test injects them.
  Dropping either kwarg reds this row.
- :func:`check_live_output_text_wired` -- a source-scan probe asserting the live
  wave spawn (``_spawn_and_dispatch``) threads ``output_text=spawn_result.text``
  into ``run_dispatch``, so the W08 stdout producer fires on a real
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
  outside its own module AND no asserting test references it. The call-site
  probe is AST-based (call / import / name detection), so a comment or docstring
  mention no longer discharges the call-site contract the way the prior
  word-boundary regex did. The meta-gate generalizes the B091 lesson from one
  hardcoded contract to *every* future contract a diff introduces, so a fresh
  dead verifier is caught the same commit it lands.
- :func:`check_contract_exercised` -- a *dynamic* leg (armed under
  ``--phase-close``) that proves a bound contract actually RAN, not just that it
  is called + tested. It reads the stores each bound contract writes and reds the
  phase close on a contract that captured zero runtime output -- the exact blind
  spot behind EU capture passing while writing no rows. A jury-gated contract
  (the ballot) is skipped until the jury has convened at least once.

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

This module keeps the band contract, the dynamic-exercise leg, and :func:`main`;
the other checks live in sibling modules it re-exports: ``idle_contract_probes``
(in-process binding probes), ``idle_contract_wiring`` (source-scan and liveness
checks), ``idle_contract_meta`` (the meta-gate), and ``idle_contract_common``
(the shared result types).

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

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eawf.kernel.spec.common import _tier_for_gate_kind
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    ReportSource,
    WaveStatus,
)
from eawf.kernel.state.models import Wave
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.models import ProfileBody, VerifyBlock
from eawf.runtime.daemon.methods.spec_sync_lints import (
    require_affordance_parity_for_ui_scope as _require_affordance_parity_for_ui_scope,
)
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY, registered_audit_dsl_kinds
from eawf.workflow.dispatch.spec_jury import (
    SPEC_JURY_FINDINGSET_EVENT_TYPE,
    produce_spec_jury_verdict,  # noqa: F401
)
from eawf.workflow.lifecycle.wave import plan_wave as _plan_wave
from eawf.workflow.skills.engine import run_skill as _run_skill
from eawf.workflow.verify.readiness import (
    resolve_wave_verify_block,
    wired_audit_dsl_kinds,
)

# The sibling modules below live beside this file, not in an installed package;
# a caller that imports this gate as ``tools.idle_contract_gate`` or by file path
# has no ``tools/`` on ``sys.path`` until this runs.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from idle_contract_common import (  # noqa: E402
    _NON_UI_SCOPE,
    _PROBE_OPENED_AT,
    _REPO_ROOT,
    _UI_SCOPE,
    GateFailure,
    GateResult,
)
from idle_contract_meta import (  # noqa: E402
    IdleContractFinding,
    MissingDischarge,
    _default_diff,
    _default_read,
    _default_tree,
    detect_idle_contracts,
)
from idle_contract_meta import _ast_references_symbol as _ast_references_symbol  # noqa: E402
from idle_contract_meta import _has_asserting_test as _has_asserting_test  # noqa: E402
from idle_contract_meta import _has_call_site as _has_call_site  # noqa: E402
from idle_contract_probes import check_i03_contracts, check_skill_body_binding  # noqa: E402
from idle_contract_wiring import (  # noqa: E402
    _EAWF023_BAD_PATH as _EAWF023_BAD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF023_GOOD_PATH as _EAWF023_GOOD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF024_BAD_SOURCE as _EAWF024_BAD_SOURCE,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF024_GOOD_SOURCE as _EAWF024_GOOD_SOURCE,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF025_BAD_PATH as _EAWF025_BAD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF025_GOOD_PATH as _EAWF025_GOOD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    check_audit_dsl_kinds_wired,
    check_campaign_carryover_prune_wired,
    check_campaign_claim_fold_wired,
    check_campaign_producer_class_non_stub,
    check_console_app_construction_wired,
    check_coverage_gate_helpers_wired,
    check_drive_ladders_wired,
    check_eawf023_artifact_placement_wired,
    check_eawf024_test_tier_wired,
    check_eawf025_test_placement_wired,
    check_jury_block_authority_wired,
    check_jury_reliability_map_wired,
    check_live_output_text_wired,
    check_phase_track_tag_wired,
    check_resolve_routing_wired,
    check_runtime_gate_is_not_idle,
    check_spec_jury_ballot_fn_wired,
    check_validate_jury_cli_wired,
)

#: A resolver with the shape of
#: :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`. Injected so
#: the global-flip failure mode is testable with a stub resolver that returns
#: an always-enforcing block.
type ResolveFn = Callable[[VerifyBlock | None, Wave], VerifyBlock | None]


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
# Dynamic-exercise leg: a bound contract must produce runtime output per phase.
# =========================================================================== #

#: A store-row reader: returns the parsed records of ``.ea/store/<stem>.jsonl``
#: for a store *stem*. Enveloped stores are unwrapped to their ``payload`` so a
#: predicate reads record fields directly; plain (non-enveloped) stores are
#: returned as parsed. Injected so a test feeds synthetic rows without a real
#: store.
type StoreRowsFn = Callable[[str], list[dict[str, Any]]]

#: A jury-convened predicate: ``True`` once the cross-vendor jury has convened at
#: least once (read from the event store). Injected so a test drives both the
#: gated-off and gated-on ballot outcomes.
type JuryConvenedFn = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class _BoundContract:
    """A contract whose runtime exercise is proved by a row in a store.

    Attributes:
        name: The contract name a finding reports. This is a bound-contract
            label, not a source symbol -- the dynamic leg proves runtime output,
            not a static call-site.
        store_stem: The ``.ea/store/<stem>.jsonl`` store file the contract writes.
        counts_row: Predicate deciding whether one store record proves exercise
            (e.g. an actuals row counts only when ``elapsed_eu > 0``).
        gated_by_jury: When ``True`` the contract is SKIPPED until the jury has
            convened at least once, so an un-exercised-by-design contract cannot
            wedge a phase close (the ballot contract, reserved for P31).
    """

    name: str
    store_stem: str
    counts_row: Callable[[Mapping[str, Any]], bool]
    gated_by_jury: bool = False


def _row_has_authored_verdict(record: Mapping[str, Any]) -> bool:
    """Count an auditor report only when its agent authored a typed verdict.

    A daemon-synthesized body is minted after the agent's own output failed
    validation, so it proves the producer ran without producing a verdict.

    Args:
        record: A parsed store record (the unwrapped auditor-report payload).

    Returns:
        ``True`` when ``body.verdict`` is an :class:`AgentReportVerdict` value
        and ``body.report_source`` is not ``synthesized``.
    """
    body = record.get("body")
    if not isinstance(body, dict):
        return False
    verdicts = {verdict.value for verdict in AgentReportVerdict}
    return (
        body.get("verdict") in verdicts
        and body.get("report_source") != ReportSource.SYNTHESIZED.value
    )


def _row_has_ground_truth(record: Mapping[str, Any]) -> bool:
    """Count a gold label only when it pins a boolean ground truth to a wave.

    Args:
        record: A parsed gold-label store record.

    Returns:
        ``True`` when ``wave_id`` is a non-empty string and ``ground_truth`` a bool.
    """
    wave_id = record.get("wave_id")
    return (
        isinstance(wave_id, str) and bool(wave_id) and isinstance(record.get("ground_truth"), bool)
    )


def _row_has_cast_verdict(record: Mapping[str, Any]) -> bool:
    """Count a juror ballot only when the juror cast a verdict, not an abstention.

    Args:
        record: A parsed jury-ballot store record (the unwrapped payload).

    Returns:
        ``True`` when ``verdict`` is a non-empty string.
    """
    verdict = record.get("verdict")
    return isinstance(verdict, str) and bool(verdict)


def _row_has_positive_elapsed_eu(record: Mapping[str, Any]) -> bool:
    """Count an actuals record only when it captured a positive ``elapsed_eu``.

    EU capture is the canonical "wired but writes nothing" blind spot: a row
    could exist with ``elapsed_eu == 0`` and still have measured nothing, so the
    contract is exercised only by a strictly-positive elapsed-EU row.

    Args:
        record: A parsed store record (the unwrapped actuals payload).

    Returns:
        ``True`` when the record carries a numeric ``elapsed_eu`` greater than 0.
    """
    value = record.get("elapsed_eu", 0.0)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0.0


#: The three I22 bound contracts the dynamic leg enforces at phase close: the
#: verdict producer (an ``auditor_report`` row with an authored verdict), EU
#: capture (an ``actual`` row with a positive ``elapsed_eu``), and calibration (a
#: ``gold_label`` row pinning a boolean ground truth). The
#: juror-ballot contract is deliberately absent -- see :data:`_BALLOT_CONTRACT`.
_I22_BOUND_CONTRACTS: tuple[_BoundContract, ...] = (
    _BoundContract(
        name="verdict_producer",
        store_stem="auditor_report",
        counts_row=_row_has_authored_verdict,
    ),
    _BoundContract(
        name="eu_capture",
        store_stem="actual",
        counts_row=_row_has_positive_elapsed_eu,
    ),
    _BoundContract(
        name="calibration",
        store_stem="gold_label",
        counts_row=_row_has_ground_truth,
    ),
)

#: The juror-ballot contract, EXCLUDED from the I22 enforced set. The
#: cross-vendor jury has never convened in repo history, so enforcing a
#: ballot-output contract now would wedge the phase re-close on a row nothing in
#: I22 produces. It is gated behind the "jury convened >= 1" predicate and earns
#: its contract in P31 calibration -- adding it to the enforced set stays inert
#: until a real jury run records its event.
_BALLOT_CONTRACT = _BoundContract(
    name="juror_ballot",
    store_stem="jury_ballot",
    counts_row=_row_has_cast_verdict,
    gated_by_jury=True,
)


def _default_store_rows(store_stem: str) -> list[dict[str, Any]]:
    """Read parsed records from ``.ea/store/<store_stem>.jsonl``, honest-empty when absent.

    Enveloped stores (:class:`eawf.kernel.store.envelope.Envelope`) wrap the
    kind-specific record in a ``payload`` dict; this reader unwraps it so a
    predicate reads record fields directly. Plain stores (e.g. the gold-label
    store, which carries no envelope) are returned as parsed. A missing file, a
    blank line, or a malformed line is skipped rather than raising, so the leg is
    honest-empty on a store no wave has written yet.

    Args:
        store_stem: The store file stem under ``.ea/store/`` (``auditor_report``,
            ``actual``, ``gold_label``, ...).

    Returns:
        The parsed records, oldest-first; ``[]`` when the store file is absent.
    """
    path = _REPO_ROOT / ".ea" / "store" / f"{store_stem}.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        records.append(payload if isinstance(payload, dict) else record)
    return records


def _default_jury_convened(store_rows_fn: StoreRowsFn = _default_store_rows) -> bool:
    """Return whether the cross-vendor jury has convened at least once.

    A convened jury persists its raw per-juror FindingSets as an evidence-store
    row typed :data:`~eawf.workflow.dispatch.spec_jury.SPEC_JURY_FINDINGSET_EVENT_TYPE`;
    only that exact type counts. Other jury-named events (an operator's
    ``jury_gold_label``, for one) record no convening, so a substring match on
    "jury" would ungate the ballot contract before any ballot could exist.

    Args:
        store_rows_fn: Reader returning the parsed records for a store stem.

    Returns:
        ``True`` when a FindingSet row exists in the evidence store; ``False``
        otherwise.
    """
    return any(
        record.get("event_type") == SPEC_JURY_FINDINGSET_EVENT_TYPE
        for record in store_rows_fn("evidence")
    )


def check_contract_exercised(
    *,
    store_rows_fn: StoreRowsFn = _default_store_rows,
    jury_convened_fn: JuryConvenedFn = _default_jury_convened,
    contracts: Sequence[_BoundContract] = _I22_BOUND_CONTRACTS,
) -> list[IdleContractFinding]:
    """Flag every bound contract that produced NO runtime output this phase.

    The static meta-gate (:func:`detect_idle_contracts`) proves a contract is
    *called* and *tested*; this dynamic leg proves it actually *ran*, closing the
    blind spot where a contract is wired + tested yet captures zero data. For each
    bound contract it reads the contract's store and flags a
    :attr:`MissingDischarge.NO_RUNTIME_OUTPUT` finding when no record counts as
    exercise. A jury-gated contract (the ballot) is SKIPPED entirely while the
    jury-convened predicate is ``False`` so an un-exercised-by-design contract
    cannot wedge a phase close.

    The leg reads stores only -- it never mutates state, writes a file, or runs a
    mutating ``eawf`` command. The store reader, jury predicate, and contract set
    are injected (each defaulting to the live production value) so a test drives
    the zero-output, has-output, and jury-gated outcomes without a real store.

    Args:
        store_rows_fn: Reader returning the parsed records for a store stem.
        jury_convened_fn: Predicate gating the ballot contract.
        contracts: The bound contracts to check; defaults to the I22 three.

    Returns:
        One :class:`IdleContractFinding` per bound contract with no exercising
        row, in contract order. Empty when every non-skipped contract has at
        least one exercising row.
    """
    findings: list[IdleContractFinding] = []
    for contract in contracts:
        if contract.gated_by_jury and not jury_convened_fn():
            continue
        rows = store_rows_fn(contract.store_stem)
        if any(contract.counts_row(row) for row in rows):
            continue
        findings.append(
            IdleContractFinding(
                symbol=contract.name,
                module=f".ea/store/{contract.store_stem}.jsonl",
                missing=MissingDischarge.NO_RUNTIME_OUTPUT,
            )
        )
    return findings


def _render_findings(findings: Sequence[IdleContractFinding]) -> str:
    """Render *findings* as a human-readable multi-line failure message.

    Args:
        findings: The orphan contracts to describe.

    Returns:
        One line per finding, each naming the symbol, its module, and the
        missing discharge, with advice specific to the discharge kind.
    """
    lines = [f"idle-contract gate: {len(findings)} contract(s) ship idle:"]
    for finding in findings:
        if finding.missing is MissingDischarge.NO_RUNTIME_OUTPUT:
            advice = (
                "a bound contract must produce at least one row in its store "
                "during the phase -- a wired-but-never-exercised contract captures "
                "nothing"
            )
        else:
            advice = "a new contract needs a call-site outside its module AND an asserting test"
        lines.append(
            f"  - {finding.symbol} (in {finding.module}) is missing "
            f"{finding.missing.value}; {advice}"
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


def _resolve_diff_range(argv: list[str]) -> str:
    """Return the meta-gate diff range from either script-style or direct argv.

    ``main`` historically received ``sys.argv`` (script path at index 0), while
    unit tests and PR-range callers naturally pass argparse-style args (no
    script path). Accept both so the pre-commit hook (``--cached``) and PR range
    invocation (``BASE..HEAD``) drive the same range-aware code path.

    Args:
        argv: Either ``sys.argv`` or an argument list excluding the program.

    Returns:
        ``--cached`` when no explicit range is supplied; otherwise the first
        non-script argument.
    """
    if not argv:
        return "--cached"
    if len(argv) == 1:
        only = argv[0]
        if only.endswith(".py") or only.endswith("idle_contract_gate"):
            return "--cached"
        return only
    return argv[1]


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
    :func:`check_validate_jury_cli_wired`), then the I11 phase-track-tag binding
    probe (:func:`check_phase_track_tag_wired`), then the
    two I17 live-autopilot binding probes (:func:`check_drive_ladders_wired`,
    :func:`check_live_output_text_wired`), then the two P30-I18 campaign-run
    binding probes (:func:`check_campaign_claim_fold_wired`,
    :func:`check_campaign_carryover_prune_wired`), then the
    runtime-gate binding check (:func:`check_runtime_gate_is_not_idle`), then the
    console-app construction-reachability check
    (:func:`check_console_app_construction_wired`), then the
    registry-wide audit-DSL wired-on sweep
    (:func:`check_audit_dsl_kinds_wired`), then the meta-gate
    (:func:`detect_idle_contracts`) over the staged diff. All must pass; the
    exit code is non-zero when any fails.

    Under ``--phase-close`` one further leg runs after the static gates: the
    dynamic-exercise leg (:func:`check_contract_exercised`) reads the stores each
    bound contract writes and reds the close on a contract that captured no
    runtime output this phase. It is flag-gated because runtime output accrues
    across a whole phase, not on any single staged commit, so it must not fire on
    every pre-commit run.

    Args:
        argv: Process argv (with or without the script path). The first
            non-script argument overrides the default ``--cached`` diff range
            fed to the meta-gate (e.g. ``HEAD~1..HEAD`` for a CI range check);
            the ``--phase-close`` flag additionally arms the dynamic-exercise
            leg and is stripped before the diff range is resolved.

    Returns:
        ``0`` when all gates pass; ``1`` when any fails.
    """
    phase_close = "--phase-close" in argv
    diff_range = _resolve_diff_range([arg for arg in argv if arg != "--phase-close"])

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
    # probe above): the spec-jury ballot fn, the reputation reliability map
    # (W06), the earned block authority, and the validate_jury CLI caller
    # (W07). Any re-idle of one fails its row and reds the gate.
    #
    # The I11 Track binding reads its live source off the working tree the same
    # way: the silent open_phase track-tag stamp. A re-idle fails its row.
    #
    # The two I17 live-autopilot bindings read their live source off the working
    # tree the same way: the drive-arming classify + repair kwargs and the
    # live-spawn output_text fan. Either re-dormant fails its row.
    #
    # The two P30-I18 campaign-run bindings read their live source off the working
    # tree the same way: run_campaign's state.claims fold via the canonical writer
    # (W05/W06) and its L1 carryover prune call between rounds. Either
    # re-idle fails its row.
    #
    # The P30-I20 campaign producer class seam is checked opportunistically: W06
    # owns adding the class, and this row reds it if it lands as a stub.
    for source_scan_check in (
        check_spec_jury_ballot_fn_wired(),
        check_jury_reliability_map_wired(),
        check_jury_block_authority_wired(),
        check_validate_jury_cli_wired(),
        check_phase_track_tag_wired(),
        check_drive_ladders_wired(),
        check_live_output_text_wired(),
        check_campaign_claim_fold_wired(),
        check_campaign_carryover_prune_wired(),
        check_campaign_producer_class_non_stub(),
    ):
        failed |= _report_result(source_scan_check)

    # P30-I20 honest-close rows: close the meta-gate holes for EAWF023's
    # single-path helper and the coverage-gate private matcher/runner. EAWF025
    # joins them because it is diff-scoped: a commit that adds no test never
    # runs the rule, so only an always-on row keeps the checker honest.
    failed |= _report_result(check_eawf023_artifact_placement_wired())
    failed |= _report_result(check_eawf024_test_tier_wired())
    failed |= _report_result(check_eawf025_test_placement_wired())
    failed |= _report_result(check_coverage_gate_helpers_wired())

    failed |= _report_result(check_runtime_gate_is_not_idle())

    failed |= _report_result(check_console_app_construction_wired())

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

    # Dynamic-exercise leg (per-phase, not per-commit): a bound contract can be
    # statically called + tested yet still capture nothing at runtime. Under
    # --phase-close this reads the stores each bound contract writes and reds the
    # close on a zero-output contract. It is gated behind the flag because runtime
    # output accrues across a whole phase, not on any single staged commit, so
    # firing it on every pre-commit would red on stores no commit has written yet.
    if phase_close:
        # Pass the module-level sources explicitly (same reason as the checks
        # above): a default-bound parameter would snapshot the unpatched function
        # at def time, so a test could not redirect the store reader.
        runtime_findings = check_contract_exercised(
            store_rows_fn=_default_store_rows,
            jury_convened_fn=_default_jury_convened,
        )
        if runtime_findings:
            print(_render_findings(runtime_findings), file=sys.stderr)
            failed = True
        else:
            print("idle-contract dynamic leg: ok (every bound contract has runtime output)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
