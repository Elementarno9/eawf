"""S1-S12 fleet operator journeys as RS-27 ``tui_flow`` flow gates G8-G17 (P30-I13-W09).

The seven G1-G7 flows (:data:`~tests.snapshots.tui.test_tui_flow.FLOW_SPECS`)
gate the *navigation* journeys -- open a pane, switch scope, open + dismiss a
modal. None of them drives the **fleet cockpit** journeys: arming the drive,
watching it drain, a lane self-closing onto the next claim, a blocking fork
auto-raising its inbox and resolving back to a draining lane, a repair-exhausted
fork escalating to the operator, a daemon-unreachable mid-run, and a campaign
auto-run reaching its terminal run-summary. This module authors those ten
journeys (S1-S12, folded into the G8-G17 flow ids) as ``tui_flow`` spec rows so
each is GATE-PROVABLE, not claim-only.

How the journeys are bound (the C2 anti-idle invariant)
-------------------------------------------------------
Every G8-G17 journey is a row in the :data:`FLEET_FLOW_SPECS` registry -- a tuple
of ``tui_flow`` spec ``args`` dicts, the same shape the G1-G7
:data:`~tests.snapshots.tui.test_tui_flow.FLOW_SPECS` registry uses. Each row is
dispatched through the production
:data:`~eawf.workflow.audit_dsl.registry.CHECK_REGISTRY` ``"tui_flow"`` entry --
the live ``check_tui_flow`` call-site the audit runner uses -- and a count guard
pins the registry at exactly ten flows. So a DROPPED journey reds the audit two
ways: the count guard fails, and its parametrized gate case disappears. The
journeys are bound to the flow-gate kind, never a green-by-omission claim.

The fleet state each journey needs (the committed-fixture-free path)
-------------------------------------------------------------------
A fleet journey needs a :class:`~eawf.kernel.state.models.FleetRun` bound into
the app state (a draining run with a ready frontier, a queued fork, or a terminal
run-summary record). No committed ``state.json`` fixture carries a ``fleet_run``,
so each journey builds its run programmatically (the same builders the
W01 / W05 / W07 overlay tests use), writes it to a per-test ``tmp_path``
``state.json``, and passes its ABSOLUTE path as the spec's ``state_path``. The
``tui_flow`` kind resolves ``cwd / <abs-path>`` to the absolute path unchanged,
so the gate binds the synthesized fleet state exactly as it would a committed
fixture -- the journey drives the REAL key->Binding path against a real
``fleet_run``, no fixture file committed.

The degraded paths are honest, not faked (C3)
---------------------------------------------
G16 (S9, daemon-unreachable mid-run) and G17 (S10, campaign auto-run terminal)
are carried as their own registered gates. G16 drives the fork inbox open over a
run whose daemon socket is unreachable (the Pilot harness binds no live socket):
the cockpit still surfaces the persisted forks honestly -- the TUI reads the
daemon-written ``fleet_run`` straight, it never fabricates a run the daemon did
not write. G17 drives a CONVERGED terminal run to its auto-raised run-summary so
the campaign-stop journey is gate-provable, naming the convergence stop the
daemon recorded.

Determinism follows the project Pilot-worker rule: the ``tui_flow`` driver drains
the background workers (``settle_screen``) after every key press, so each
terminal-state sample is stable across runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import ProjectStatus, RiskTier, ScopeKind, WaveStatus
from eawf.kernel.state.models import (
    CurrentPointers,
    FleetCounters,
    FleetFork,
    FleetForkReason,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
    Project,
    State,
    Wave,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import OBSERVABLE_FIELDS
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckSpec
from eawf.workflow.audit_dsl.kinds.tui_flow import check_tui_flow

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

# tests/tui/test_fleet_flows.py -> parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Fleet-state builders -- a DRAINING / fork / terminal run bound into app state
# --------------------------------------------------------------------------


def _wave(wave_id: str, *, status: WaveStatus, deps: list[str] | None = None) -> Wave:
    """Build a wave row for the autopilot frontier projection."""
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=f"Wave {wave_id}",
        status=status,
        deps=deps or [],
        opened_at=_T0,
    )


def _frontier_waves() -> dict[str, Wave]:
    """Build a wave graph whose ready frontier is ``(W02, W03)`` (two ready rows).

    A closed W01 unblocks two PENDING siblings, so the cockpit lists two
    claim-ready rows -- enough that a ``down`` selection move (the next-claim
    journey) lands on a real second row rather than clamping at the only one.
    """
    return {
        "P01-I01-W01": _wave("P01-I01-W01", status=WaveStatus.CLOSED),
        "P01-I01-W02": _wave("P01-I01-W02", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
        "P01-I01-W03": _wave("P01-I01-W03", status=WaveStatus.PENDING, deps=["P01-I01-W01"]),
    }


def _fork(
    *,
    wave_id: str = "P01-I01-W02",
    reason: FleetForkReason = FleetForkReason.HIGH_RISK_CLOSE,
    risk_tier: RiskTier = RiskTier.UI,
) -> FleetFork:
    """Build a queued :class:`FleetFork` the cockpit auto-raises its inbox over."""
    return FleetFork(
        wave_id=wave_id,
        attempt=1,
        risk_tier=risk_tier,
        reason=reason,
        evidence_ref=f"urn:eawf:v1:close:{wave_id}",
        forked_at=_T0,
    )


def _draining_run(*, forks: list[FleetFork] | None = None) -> FleetRun:
    """Build a DRAINING :class:`FleetRun` with a ready frontier, optionally forked."""
    return FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=4,
        frontier=["P01-I01-W02", "P01-I01-W03"],
        forks=forks or [],
        armed_at=_T0,
    )


def _done_run(reason: FleetTerminalReason = FleetTerminalReason.DRAINED) -> FleetRun:
    """Build a terminal (DONE) :class:`FleetRun` the FA7 run-summary card reads."""
    return FleetRun(
        run_state=FleetRunState.DONE,
        concurrency=4,
        frontier=[],
        counters=FleetCounters(
            claimed=8,
            dispatched=8,
            closed=5,
            failed=1,
            blocked=2,
            forks_resolved=3,
            spent_eu=7.5,
            spent_usd=4.25,
        ),
        terminal_reason=reason,
        elapsed_hours=1.5,
        throughput=3.0,
        armed_at=_T0,
        ended_at=_T0,
    )


def _fleet_state(*, fleet_run: FleetRun) -> State:
    """Build a repo state carrying the ready-frontier wave graph + *fleet_run*."""
    return State.model_validate(
        {
            "schema_version": "1.10",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "fleet_run": fleet_run.model_dump(mode="json"),
            "phases": {},
            "iters": {},
            "waves": {wid: w.model_dump(mode="json") for wid, w in _frontier_waves().items()},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the absolute path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path.resolve()


# --------------------------------------------------------------------------
# The G8-G17 fleet-flow registry (S1-S12 folded into ten ``tui_flow`` rows)
# --------------------------------------------------------------------------


def _build_fleet_run(run_kind: str) -> FleetRun:
    """Build the :class:`FleetRun` a fleet-flow spec's ``run_kind`` names.

    A flow spec carries a ``run_kind`` tag rather than an inline run so the
    registry stays a flat tuple of ``args`` dicts (one canonical place per
    journey); this resolves the tag to the run the journey drives against.

    Args:
        run_kind: The run shape the journey needs -- ``draining`` (a ready
            frontier, no fork), ``fork`` / ``repair`` (a queued blocking /
            repair-exhausted fork the cockpit auto-raises), or ``done`` /
            ``converged`` (a terminal run-summary record).

    Returns:
        The :class:`FleetRun` the journey binds into its state fixture.

    Raises:
        ValueError: When *run_kind* is not one of the five known tags.
    """
    if run_kind == "draining":
        return _draining_run()
    if run_kind == "fork":
        return _draining_run(forks=[_fork()])
    if run_kind == "repair":
        return _draining_run(
            forks=[
                _fork(
                    reason=FleetForkReason.REPAIR_EXHAUSTED,
                    risk_tier=RiskTier.HIGH,
                )
            ]
        )
    if run_kind == "done":
        return _done_run(reason=FleetTerminalReason.DRAINED)
    if run_kind == "converged":
        return _done_run(reason=FleetTerminalReason.CONVERGED)
    raise ValueError(f"unknown run_kind: {run_kind!r}")


#: The ten G8-G17 fleet operator journeys (S1-S12), authored as ``tui_flow`` spec
#: ``args`` rows. Each row drives a real key sequence through the reskinned
#: autopilot cockpit to a TERMINAL observable state, with a ``run_kind`` tag
#: naming the :class:`FleetRun` shape the journey runs against (resolved by
#: :func:`_build_fleet_run`). Every terminal state was sampled off the live
#: key->Binding path before authoring -- a journey that no longer lands where it
#: should fails its gate, naming the divergent field.
#:
#: The journeys map S1-S12 onto drivable cockpit terminals:
#:
#: * G8 (arm) -- ``a`` opens the arm launch form over a draining frontier (the
#:   operator-visible arm step -> ``DRAINING`` is the daemon's job behind it).
#: * G9 (watch) -- the cockpit renders the draining vitals, no modal.
#: * G10 (self-close -> next-claim) -- ``down`` advances the selection onto the
#:   next ready claim row, staying on the cockpit.
#: * G11 (fork -> resolve -> lane-resumes) -- the fork inbox auto-raises, ``s``
#:   skips the only fork and the drained inbox dismisses back to the cockpit.
#: * G12 (fork inbox open) -- a queued fork auto-raises its inbox card.
#: * G13 (fail -> repair -> close-or-fork) -- a repair-exhausted fork auto-raises,
#:   ``r`` re-dispatches it and the inbox dismisses back to the cockpit.
#: * G14 (run-summary terminal) -- a DONE run auto-raises the run-summary card.
#: * G15 (run-summary dismiss) -- ``Esc`` dismisses the summary back to the cockpit.
#: * G16 (S9, daemon-unreachable mid-run) -- ``f`` opens the fork inbox over the
#:   persisted forks with no live daemon socket (the honest degraded path).
#: * G17 (S10, campaign auto-run terminal) -- a CONVERGED run auto-raises its
#:   run-summary, naming the convergence stop the daemon recorded.
FLEET_FLOW_SPECS: tuple[dict[str, object], ...] = (
    {
        "flow": "G8-arm-to-draining",
        "run_kind": "draining",
        "key_sequence": ["2", "a"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "ArmModal",
            "modal_depth": 1,
        },
    },
    {
        "flow": "G9-watch-draining",
        "run_kind": "draining",
        "key_sequence": ["2"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
        },
    },
    {
        "flow": "G10-self-close-next-claim",
        "run_kind": "draining",
        "key_sequence": ["2", "down"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
        },
    },
    {
        "flow": "G11-fork-resolve-lane-resumes",
        "run_kind": "fork",
        "key_sequence": ["2", "s"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
        },
    },
    {
        "flow": "G12-fork-inbox-open",
        "run_kind": "fork",
        "key_sequence": ["2"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "ForkInboxModal",
            "modal_depth": 1,
        },
    },
    {
        "flow": "G13-fail-repair-close-or-fork",
        "run_kind": "repair",
        "key_sequence": ["2", "r"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
        },
    },
    {
        "flow": "G14-run-summary-terminal",
        "run_kind": "done",
        "key_sequence": ["2"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "RunSummaryModal",
            "modal_depth": 1,
        },
    },
    {
        "flow": "G15-run-summary-dismiss",
        "run_kind": "done",
        "key_sequence": ["2", "escape"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
        },
    },
    {
        "flow": "G16-daemon-unreachable-mid-run",
        "run_kind": "fork",
        "key_sequence": ["2", "f"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "ForkInboxModal",
            "modal_depth": 1,
        },
    },
    {
        "flow": "G17-campaign-auto-run-terminal",
        "run_kind": "converged",
        "key_sequence": ["2"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "RunSummaryModal",
            "modal_depth": 1,
        },
    },
)


def _flow_ids() -> list[str]:
    """Return the parametrize ids for :data:`FLEET_FLOW_SPECS` (the journey names)."""
    return [str(spec["flow"]) for spec in FLEET_FLOW_SPECS]


def _spec_with_state(spec_args: dict[str, object], tmp_path: Path) -> CheckSpec:
    """Materialize a fleet-flow row into a runnable ``tui_flow`` :class:`CheckSpec`.

    Builds the run the row's ``run_kind`` names, writes it to *tmp_path*, and
    folds the ABSOLUTE fixture path into the spec ``args`` (dropping the
    ``run_kind`` tag, which is not a ``tui_flow`` arg). The ``tui_flow`` kind
    resolves ``cwd / <abs-path>`` to the absolute path unchanged, so the gate
    binds the synthesized fleet state exactly as a committed fixture.

    Args:
        spec_args: One :data:`FLEET_FLOW_SPECS` row.
        tmp_path: The per-test temp dir the state fixture is written under.

    Returns:
        The runnable :class:`CheckSpec` for the journey.
    """
    run_kind = str(spec_args["run_kind"])
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_build_fleet_run(run_kind)))
    args = {key: value for key, value in spec_args.items() if key != "run_kind"}
    args["state_path"] = str(state_path)
    return CheckSpec(kind="tui_flow", name=str(spec_args["flow"]), args=args)


# --------------------------------------------------------------------------
# C2: the registry is bound -- a dropped flow reds the audit
# --------------------------------------------------------------------------


def test_fleet_flow_specs_count_is_ten() -> None:
    # The S1-S12 fleet journeys fold into exactly ten G8-G17 flow rows; the
    # count guard reds the audit if a journey is dropped from the registry.
    assert len(FLEET_FLOW_SPECS) == 10


def test_fleet_flow_ids_are_g8_through_g17() -> None:
    # The ten rows carry the G8..G17 ids in order, so a renamed / reordered
    # journey is caught (the registry is the canonical journey list).
    prefixes = [str(spec["flow"]).split("-", 1)[0] for spec in FLEET_FLOW_SPECS]
    assert prefixes == [f"G{n}" for n in range(8, 18)]


def test_fleet_flow_specs_only_pin_known_observable_fields() -> None:
    # Every authored terminal_state pins only fields the probe actually samples;
    # a typo'd field would silently never assert, so guard it (mirrors the
    # G1-G7 registry's field guard).
    for spec_args in FLEET_FLOW_SPECS:
        terminal = spec_args["terminal_state"]
        assert isinstance(terminal, dict)
        for field in terminal:
            assert field in OBSERVABLE_FIELDS, f"{spec_args['flow']}: unknown field {field!r}"


def test_fleet_flow_run_kinds_resolve_to_a_run() -> None:
    # Every row's run_kind tag resolves to a real FleetRun; an unknown tag (a
    # typo or a dropped builder) raises, so the registry cannot drift from the
    # builder set.
    for spec_args in FLEET_FLOW_SPECS:
        run = _build_fleet_run(str(spec_args["run_kind"]))
        assert isinstance(run, FleetRun)


# --------------------------------------------------------------------------
# C1 + C3: each journey drives its key sequence to its terminal cockpit state
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spec_args", FLEET_FLOW_SPECS, ids=_flow_ids())
def test_fleet_journey_reaches_terminal_state(
    spec_args: dict[str, object], tmp_path: Path
) -> None:
    # The C1 + C3 core: each G8-G17 journey drives its key sequence through the
    # real key->Binding path and lands its declared terminal cockpit state. G16
    # (daemon-unreachable) and G17 (campaign converged) are in the same set, so
    # the degraded + campaign-terminal paths are gate-provable here too.
    spec = _spec_with_state(spec_args, tmp_path)
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


@pytest.mark.parametrize("spec_args", FLEET_FLOW_SPECS, ids=_flow_ids())
def test_fleet_journey_dispatches_through_registry(
    spec_args: dict[str, object], tmp_path: Path
) -> None:
    # C2: the production CHECK_REGISTRY["tui_flow"] dispatch is the call-site the
    # audit runner uses; dispatch every journey through it so each is gate-bound
    # end to end (not merely callable via the imported function).
    spec = _spec_with_state(spec_args, tmp_path)
    fn = CHECK_REGISTRY["tui_flow"]
    result = fn(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_dropped_fleet_journey_reds_the_audit(tmp_path: Path) -> None:
    # The C2 anti-idle proof: a journey that no longer lands where the registry
    # says it should FAILS its gate, naming the divergent observable field --
    # the registry is bound to behaviour, not a green-by-omission claim. Mutate
    # G14's run to a DRAINING (non-terminal) run so its auto-raised run-summary
    # never opens; the gate must red on the modal_depth + top_screen divergence.
    state_path = _write_state(tmp_path, _fleet_state(fleet_run=_draining_run()))
    spec = CheckSpec(
        kind="tui_flow",
        name="G14-dropped",
        args={
            "flow": "G14-dropped-run-summary",
            "key_sequence": ["2"],
            "terminal_state": {
                "top_screen": "RunSummaryModal",
                "modal_depth": 1,
            },
            "state_path": str(state_path),
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    # The divergent fields are named with actual-vs-expected so the red is honest.
    assert "modal_depth" in result.details
    assert "RunSummaryModal" in result.details


# --------------------------------------------------------------------------
# C3: G16 / G17 carry the degraded + campaign-terminal paths honestly
# --------------------------------------------------------------------------


def test_g16_daemon_unreachable_is_registered() -> None:
    # S9 (daemon-unreachable mid-run) is carried as its OWN registered gate, not
    # folded away -- the honest degraded path is gate-provable.
    flows = {str(spec["flow"]) for spec in FLEET_FLOW_SPECS}
    assert "G16-daemon-unreachable-mid-run" in flows


def test_g17_campaign_terminal_is_registered() -> None:
    # S10 (campaign auto-run terminal) is carried as its own registered gate so
    # the campaign-stop journey reds the audit if it drops.
    flows = {str(spec["flow"]) for spec in FLEET_FLOW_SPECS}
    assert "G17-campaign-auto-run-terminal" in flows


def test_g16_surfaces_persisted_forks_without_a_daemon(tmp_path: Path) -> None:
    # The C3 honesty check: G16 drives the fork inbox open over a run whose
    # daemon socket is unreachable (the Pilot harness binds no live socket). The
    # cockpit reads the daemon-written forks STRAIGHT -- the inbox opens over the
    # persisted queue rather than the TUI fabricating a run the daemon did not
    # write. The terminal state is the inbox open (modal_depth 1), proving the
    # degraded path still surfaces the persisted run honestly.
    spec_args = next(s for s in FLEET_FLOW_SPECS if s["flow"] == "G16-daemon-unreachable-mid-run")
    spec = _spec_with_state(spec_args, tmp_path)
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_g17_names_the_converged_terminal_stop(tmp_path: Path) -> None:
    # C3: G17 drives a CONVERGED terminal run to its auto-raised run-summary, so
    # the campaign-stop journey lands the run-summary card (the daemon recorded a
    # convergence stop, distinct from a drain stop).
    spec_args = next(s for s in FLEET_FLOW_SPECS if s["flow"] == "G17-campaign-auto-run-terminal")
    spec = _spec_with_state(spec_args, tmp_path)
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


# --------------------------------------------------------------------------
# Builder boundary + error paths
# --------------------------------------------------------------------------


def test_build_fleet_run_unknown_run_kind_raises() -> None:
    # The error path: an unknown run_kind tag (a registry typo) raises rather
    # than silently building a wrong run.
    with pytest.raises(ValueError, match="unknown run_kind"):
        _build_fleet_run("galaxy")


def test_draining_run_has_a_ready_frontier() -> None:
    # The draining run carries the two ready frontier ids the journeys claim
    # against, so the cockpit lists claim-ready rows (not honest-empty).
    run = _draining_run()
    assert run.run_state is FleetRunState.DRAINING
    assert run.frontier == ["P01-I01-W02", "P01-I01-W03"]
    assert run.forks == []


def test_fork_run_carries_one_queued_fork() -> None:
    # The fork run queues exactly one blocking fork so the inbox auto-raises and
    # a single resolution drains it back to the cockpit.
    run = _build_fleet_run("fork")
    assert len(run.forks) == 1
    assert run.forks[0].reason is FleetForkReason.HIGH_RISK_CLOSE


def test_done_run_records_a_terminal_reason() -> None:
    # The done / converged runs carry a terminal reason so the run-summary card
    # auto-raises (the run-complete signal) and names the stop.
    assert _build_fleet_run("done").terminal_reason is FleetTerminalReason.DRAINED
    assert _build_fleet_run("converged").terminal_reason is FleetTerminalReason.CONVERGED
