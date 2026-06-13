"""Tests for the ``tui_flow`` audit kind + the 7 operator-journey flows (RS-27).

The ``tui_flow`` kind
(:func:`~eawf.workflow.audit_dsl.kinds.tui_flow.check_tui_flow`) drives a named
key sequence through the real key->Binding path and asserts the journey's
TERMINAL observable state -- the app chrome and state-backed lifecycle signals
the behaviour probe samples -- equals a declared ``terminal_state``. It is the
multi-step-journey complement to the per-key ``affordance_parity`` gate and the
per-surface snapshot gate: neither of those proves a journey REACHED its
terminal state.

Coverage:

* passing flow -- a real journey reaches its declared terminal observable
  state (``status="pass"``).
* missing-terminal-state fail-path -- a journey whose declared
  ``terminal_state`` does not match where it lands fails, NAMING the divergent
  observable field (actual vs expected).
* the 7 G1-G7 operator-journey flow specs (:data:`FLOW_SPECS`) each run GREEN
  against the current reskinned surfaces and G1/G3/G5/G6 pin the scenario-
  register terminal state facts -- this IS the I00 acceptance wiring for the
  flow-gated journeys.
* malformed args -- a missing / mistyped ``flow`` / ``key_sequence`` /
  ``terminal_state``, an unknown terminal-state field, a bad ``size``, a
  missing fixture all DEGRADE to ``status="fail"`` rather than raising.
* registry dispatch + ``CheckKind`` Literal source.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from eawf.surfaces.tui.snapshot.behaviour_probe import OBSERVABLE_FIELDS
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl import models as models_module
from eawf.workflow.audit_dsl.kinds.tui_flow import check_tui_flow

# tests/snapshots/tui/test_tui_flow.py -> parents[3] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_STATE_REL = "tests/fixtures/states/valid/03-phase-iter-wave-active.json"
_WORKSPACE_STATE_REL = "tests/fixtures/states/valid/05-workspace-state.json"
_REGISTER_PLANNED_DAG = (
    "P01-I02:P01-I02-W01:root,"
    "P01-I02-W01->P01-I02-W02,"
    "P01-I02-W01->P01-I02-W03,"
    "P01-I02-W02->P01-I02-W03"
)
_REGISTER_AUTHORITY_DECISION = "D-AUTH-GATE"
_REGISTER_FOLLOWUPS = "B-FOLLOW-01|B-FOLLOW-02"
_REGISTER_STATE: dict[str, object] = {
    "schema_version": "1.12",
    "scope_kind": "repo",
    "urn": "urn:eawf:v1:state:QR",
    "updated_at": "2026-06-01T12:00:00Z",
    "project": {
        "code": "QR",
        "slug": "quant-research",
        "title": "Quant Research",
        "description": "",
        "domains": ["quant"],
        "default_branch": "main",
        "status": "active",
        "repo_urn": "urn:eawf:v1:repo:QR",
    },
    "current": {
        "project_code": "QR",
        "track_id": None,
        "phase_id": "P01",
        "iter_id": "P01-I01",
        "active_wave_ids": [],
        "active_session_ids": [],
    },
    "workspace": None,
    "phases": {
        "P01": {
            "id": "P01",
            "scope_id": "QR",
            "title": "Register phase",
            "status": "active",
            "iter_ids": ["P01-I01", "P01-I02"],
            "opened_at": "2026-06-01T12:00:00Z",
        }
    },
    "iters": {
        "P01-I01": {
            "id": "P01-I01",
            "phase_id": "P01",
            "title": "Active close gate iter",
            "status": "active",
            "wave_ids": ["P01-I01-W01", "P01-I01-W02"],
            "opened_at": "2026-06-01T12:00:00Z",
        },
        "P01-I02": {
            "id": "P01-I02",
            "phase_id": "P01",
            "title": "Planned revised DAG iter",
            "status": "planned",
            "wave_ids": ["P01-I02-W01", "P01-I02-W02", "P01-I02-W03"],
            "opened_at": "2026-06-01T12:00:00Z",
        },
    },
    "waves": {
        "P01-I01-W01": {
            "id": "P01-I01-W01",
            "iter_id": "P01-I01",
            "title": "Register P01-I01-W01",
            "status": "closed",
            "deps": [],
            "opened_at": "2026-06-01T12:00:00Z",
        },
        "P01-I01-W02": {
            "id": "P01-I01-W02",
            "iter_id": "P01-I01",
            "title": "Register P01-I01-W02",
            "status": "pending",
            "deps": ["P01-I01-W01"],
            "opened_at": "2026-06-01T12:00:00Z",
        },
        "P01-I02-W01": {
            "id": "P01-I02-W01",
            "iter_id": "P01-I02",
            "title": "Register P01-I02-W01",
            "status": "pending",
            "deps": [],
            "opened_at": "2026-06-01T12:00:00Z",
        },
        "P01-I02-W02": {
            "id": "P01-I02-W02",
            "iter_id": "P01-I02",
            "title": "Register P01-I02-W02",
            "status": "pending",
            "deps": ["P01-I02-W01"],
            "opened_at": "2026-06-01T12:00:00Z",
        },
        "P01-I02-W03": {
            "id": "P01-I02-W03",
            "iter_id": "P01-I02",
            "title": "Register P01-I02-W03",
            "status": "pending",
            "deps": ["P01-I02-W01", "P01-I02-W02"],
            "opened_at": "2026-06-01T12:00:00Z",
        },
    },
    "actuals": {
        "ACT-P01-I01-W01": {
            "id": "ACT-P01-I01-W01",
            "scope_id": "P01-I01-W01",
            "status": "done",
            "elapsed_eu": 1.25,
            "current_store_record_id": "ACT-P01-I01-W01-store",
            "updated_at": "2026-06-01T12:00:00Z",
        }
    },
    "audits": {
        "A-CLOSE-PASS": {
            "id": "A-CLOSE-PASS",
            "scope_id": "P01-I01-W01",
            "kind": "evaluation",
            "status": "complete",
            "created_at": "2026-06-01T12:00:00Z",
            "verdict": "pass",
        }
    },
    "decisions": {
        _REGISTER_AUTHORITY_DECISION: {
            "id": _REGISTER_AUTHORITY_DECISION,
            "scope_id": "P01",
            "title": "Record authority transition",
            "rationale": "Jury authority moved from advisory to blocking for register proof.",
            "status": "active",
            "created_at": "2026-06-01T12:00:00Z",
        }
    },
    "backlog": {
        "B-FOLLOW-01": {
            "id": "B-FOLLOW-01",
            "scope_id": "P01-I01",
            "title": "File follow-up for polish review",
            "priority": "P1",
            "status": "open",
            "created_at": "2026-06-01T12:00:00Z",
        },
        "B-FOLLOW-02": {
            "id": "B-FOLLOW-02",
            "scope_id": "P01-I01",
            "title": "File follow-up for audit review",
            "priority": "P2",
            "status": "in_progress",
            "created_at": "2026-06-01T12:00:00Z",
        },
    },
    "artifacts": {},
    "agent_sessions": {},
    "plugins": {},
    "indexes": {},
}


#: The seven G1-G7 operator-journey flows the scenario->gate register (doc
#: section G) enumerates, authored as ``tui_flow`` spec ``args`` rows. Each
#: drives a real key sequence through the reskinned TUI and pins the terminal
#: observable state the journey lands in. Asserting all seven run GREEN is the
#: I00 acceptance wiring for the flow-gated journeys -- a journey that no longer
#: lands where it should fails the gate, naming the divergent field.
#:
#: The journeys map onto the G1-G7 themes against drivable surfaces: G1 opens
#: the autopilot frontier (where a refused close is re-dispatched), G2 proves
#: the command palette is reachable + dismissable on the home surface, G3 opens
#: the research board (the propose/revise/apply surface), G4 switches to the
#: workspace scope (cross-repo), G5 opens the trust pane (the authority
#: surface), G6 opens the doctor pane (the audit/polish health surface), and G7
#: walks a research->config-modal->dismiss->home recovery loop (the mending).
FLOW_SPECS: tuple[dict[str, object], ...] = (
    {
        "flow": "G1-recover-refused-close",
        "key_sequence": ["2"],
        "terminal_state": {
            "current_mode": "autopilot",
            "top_screen": "AutopilotModeScreen",
            "modal_depth": 0,
            "close_gate_pass_count": 1,
            "elapsed_eu_total": 1.25,
        },
        "scope": "repo",
        "state": _REGISTER_STATE,
    },
    {
        "flow": "G2-onboard-init-phase-wave",
        "key_sequence": ["slash", "escape"],
        "terminal_state": {
            "current_mode": "home",
            "top_screen": "RepoScreen",
            "modal_depth": 0,
        },
        "scope": "repo",
        "state_path": _REPO_STATE_REL,
    },
    {
        "flow": "G3-roadmap-propose-revise-apply",
        "key_sequence": ["3"],
        "terminal_state": {
            "current_mode": "research_board",
            "top_screen": "ResearchBoardModeScreen",
            "modal_depth": 0,
            "planned_iter_count": 1,
            "planned_iter_dag": _REGISTER_PLANNED_DAG,
        },
        "scope": "repo",
        "state": _REGISTER_STATE,
    },
    {
        "flow": "G4-cross-repo-workspace",
        "key_sequence": ["w"],
        "terminal_state": {
            "nav_scope": "workspace",
            "top_screen": "WorkspaceScreen",
        },
        "scope": "repo",
        "state_path": _WORKSPACE_STATE_REL,
    },
    {
        "flow": "G5-trust-earns-authority",
        "key_sequence": ["4"],
        "terminal_state": {
            "current_mode": "trust",
            "top_screen": "TrustModeScreen",
            "authority_transition": _REGISTER_AUTHORITY_DECISION,
        },
        "scope": "repo",
        "state": _REGISTER_STATE,
    },
    {
        "flow": "G6-polish-audit-ship",
        "key_sequence": ["5"],
        "terminal_state": {
            "current_mode": "doctor",
            "top_screen": "DoctorModeScreen",
            "followup_ids": _REGISTER_FOLLOWUPS,
        },
        "scope": "repo",
        "state": _REGISTER_STATE,
    },
    {
        "flow": "G7-incident-the-mending",
        "key_sequence": ["3", "c", "escape", "1"],
        "terminal_state": {
            "current_mode": "home",
            "top_screen": "RepoScreen",
            "modal_depth": 0,
        },
        "scope": "repo",
        "state_path": _REPO_STATE_REL,
    },
)


def _flow_ids() -> list[str]:
    """Return the parametrize ids for :data:`FLOW_SPECS` (the journey names)."""
    return [str(spec["flow"]) for spec in FLOW_SPECS]


# --------------------------------------------------------------------------
# passing flow: a real journey reaches its declared terminal observable state
# --------------------------------------------------------------------------


def test_check_tui_flow_passing_flow_reaches_terminal_state() -> None:
    # The simplest passing journey: digit '2' opens the autopilot mode, so the
    # terminal observable state pins the autopilot mode screen with no modal.
    spec = CheckSpec(
        kind="tui_flow",
        name="autopilot-open",
        args={
            "flow": "open-autopilot",
            "key_sequence": ["2"],
            "terminal_state": {
                "current_mode": "autopilot",
                "top_screen": "AutopilotModeScreen",
            },
            "state_path": _REPO_STATE_REL,
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


def test_check_tui_flow_modal_open_close_returns_home() -> None:
    # A multi-step journey: open the config modal then dismiss it -- the
    # terminal modal depth is back to 0 and the home surface is on top, so a
    # full open->dismiss round-trip is wave-provable.
    spec = CheckSpec(
        kind="tui_flow",
        name="config-roundtrip",
        args={
            "flow": "config-open-dismiss",
            "key_sequence": ["c", "escape"],
            "terminal_state": {
                "current_mode": "home",
                "top_screen": "RepoScreen",
                "modal_depth": 0,
            },
            "state_path": _REPO_STATE_REL,
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


# --------------------------------------------------------------------------
# missing-terminal-state fail-path: names the divergent observable field
# --------------------------------------------------------------------------


def test_check_tui_flow_wrong_terminal_state_fails_naming_field() -> None:
    # The journey opens autopilot but the spec wrongly claims it lands in
    # doctor mode -- the gate fails, NAMING the divergent observable field
    # (current_mode) with the actual-vs-expected values.
    spec = CheckSpec(
        kind="tui_flow",
        name="wrong-terminal",
        args={
            "flow": "open-autopilot-wrong",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "doctor"},
            "state_path": _REPO_STATE_REL,
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    # The divergent field is named, with both the expected and actual value.
    assert "current_mode" in result.details
    assert "doctor" in result.details
    assert "autopilot" in result.details


def test_check_tui_flow_missing_terminal_modal_fails() -> None:
    # The journey opens a modal (config) but the spec claims it lands with no
    # modal on the stack -- the modal_depth field diverges and is named.
    spec = CheckSpec(
        kind="tui_flow",
        name="missing-modal",
        args={
            "flow": "open-config-claims-no-modal",
            "key_sequence": ["c"],
            "terminal_state": {"modal_depth": 0},
            "state_path": _REPO_STATE_REL,
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "modal_depth" in result.details


# --------------------------------------------------------------------------
# the 7 G1-G7 operator-journey flows run GREEN (I00 acceptance wiring)
# --------------------------------------------------------------------------


def test_flow_specs_count_is_seven() -> None:
    # The scenario->gate register section G enumerates exactly seven
    # operator-journey flows (G1-G7); the authored spec set must cover all.
    assert len(FLOW_SPECS) == 7


def test_register_flows_pin_state_backed_terminal_facts() -> None:
    # G1/G3/G5/G6 are the scenario-register rows that previously thinned to
    # single screen switches. Keep them pinned to the typed terminal facts the
    # register names: close-gate pass + EU, PLANNED revised DAG, authority
    # transition, and filed follow-ups.
    by_flow = {str(spec["flow"]): spec["terminal_state"] for spec in FLOW_SPECS}
    assert by_flow["G1-recover-refused-close"] == {
        "current_mode": "autopilot",
        "top_screen": "AutopilotModeScreen",
        "modal_depth": 0,
        "close_gate_pass_count": 1,
        "elapsed_eu_total": 1.25,
    }
    assert by_flow["G3-roadmap-propose-revise-apply"] == {
        "current_mode": "research_board",
        "top_screen": "ResearchBoardModeScreen",
        "modal_depth": 0,
        "planned_iter_count": 1,
        "planned_iter_dag": _REGISTER_PLANNED_DAG,
    }
    assert by_flow["G5-trust-earns-authority"] == {
        "current_mode": "trust",
        "top_screen": "TrustModeScreen",
        "authority_transition": _REGISTER_AUTHORITY_DECISION,
    }
    assert by_flow["G6-polish-audit-ship"] == {
        "current_mode": "doctor",
        "top_screen": "DoctorModeScreen",
        "followup_ids": _REGISTER_FOLLOWUPS,
    }


def test_flow_specs_are_raw_tui_flow_args() -> None:
    # Authored rows must be runnable through CHECK_REGISTRY without a test-only
    # materialization pass, so only production tui_flow args are allowed here.
    allowed_args = {
        "flow",
        "key_sequence",
        "terminal_state",
        "scope",
        "state_path",
        "state",
        "size",
    }
    for spec_args in FLOW_SPECS:
        extra = set(spec_args) - allowed_args
        assert not extra, f"{spec_args['flow']}: non-tui_flow arg(s): {sorted(extra)}"


@pytest.mark.parametrize("spec_args", FLOW_SPECS, ids=_flow_ids())
def test_operator_journey_flow_runs_green(spec_args: dict[str, object]) -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name=str(spec_args["flow"]),
        args=dict(spec_args),
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "pass", result.details
    assert result.passed is True


@pytest.mark.parametrize("spec_args", FLOW_SPECS, ids=_flow_ids())
def test_operator_journey_flow_dispatches_through_registry(spec_args: dict[str, object]) -> None:
    # The registration in registry.py is the production call-site; dispatch
    # the 7 flows through it so each journey is gate-provable end to end.
    spec = CheckSpec(
        kind="tui_flow",
        name=str(spec_args["flow"]),
        args=dict(spec_args),
    )
    fn = CHECK_REGISTRY["tui_flow"]
    result = fn(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_flow_specs_only_pin_known_observable_fields() -> None:
    # Every authored terminal_state pins only fields the probe actually
    # samples -- a typo'd field would silently never assert, so guard it.
    for spec_args in FLOW_SPECS:
        terminal = spec_args["terminal_state"]
        assert isinstance(terminal, dict)
        for field in terminal:
            assert field in OBSERVABLE_FIELDS, f"{spec_args['flow']}: unknown field {field!r}"


# --------------------------------------------------------------------------
# loop-safe: the gate passes inside a running event loop (daemon close path)
# --------------------------------------------------------------------------


def test_check_tui_flow_passes_inside_running_event_loop() -> None:
    # The deterministic close gate scores the floor synchronously while the
    # daemon JSON-RPC handler's event loop is running; a bare ``asyncio.run``
    # would raise "cannot be called from a running event loop". The
    # thread-offload path keeps it passing.
    spec = CheckSpec(
        kind="tui_flow",
        name="loop-safe",
        args={
            "flow": "open-autopilot-in-loop",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "state_path": _REPO_STATE_REL,
        },
    )

    async def body() -> CheckResult:
        return check_tui_flow(spec, _REPO_ROOT)

    result = asyncio.run(body())
    assert result.status == "pass", result.details


# --------------------------------------------------------------------------
# malformed args degrade-not-raise
# --------------------------------------------------------------------------


def test_check_tui_flow_missing_flow_arg_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="no-flow",
        args={"key_sequence": ["2"], "terminal_state": {"current_mode": "autopilot"}},
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "flow" in result.details


def test_check_tui_flow_non_list_key_sequence_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="bad-keys",
        args={
            "flow": "bad",
            "key_sequence": "2",
            "terminal_state": {"current_mode": "autopilot"},
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "key_sequence" in result.details


def test_check_tui_flow_non_str_key_in_sequence_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="non-str-key",
        args={
            "flow": "bad",
            "key_sequence": ["2", 1],
            "terminal_state": {"current_mode": "autopilot"},
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "key_sequence" in result.details


def test_check_tui_flow_missing_terminal_state_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="no-terminal",
        args={"flow": "bad", "key_sequence": ["2"]},
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "terminal_state" in result.details


def test_check_tui_flow_empty_terminal_state_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="empty-terminal",
        args={"flow": "bad", "key_sequence": ["2"], "terminal_state": {}},
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "terminal_state" in result.details


def test_check_tui_flow_unknown_terminal_field_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="unknown-field",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"bogus_signal": "x"},
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "bogus_signal" in result.details


def test_check_tui_flow_bad_scope_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="bad-scope",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "scope": "galaxy",
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "scope" in result.details


def test_check_tui_flow_bad_size_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="bad-size",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "size": [120],
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "size" in result.details


def test_check_tui_flow_missing_state_file_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="missing-state",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "state_path": "does/not/exist.json",
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "does/not/exist.json" in result.details


def test_check_tui_flow_invalid_inline_state_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="bad-inline-state",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "state": {"schema_version": "1.12"},
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "state" in result.details
    assert "validation" in result.details


def test_check_tui_flow_state_path_and_inline_state_fails() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="duplicate-state-source",
        args={
            "flow": "bad",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "state_path": _REPO_STATE_REL,
            "state": _REGISTER_STATE,
        },
    )
    result = check_tui_flow(spec, _REPO_ROOT)
    assert result.status == "fail"
    assert result.details is not None
    assert "mutually exclusive" in result.details


# --------------------------------------------------------------------------
# registry dispatch + CheckKind Literal source
# --------------------------------------------------------------------------


def test_check_registry_tui_flow_dispatches() -> None:
    spec = CheckSpec(
        kind="tui_flow",
        name="registry",
        args={
            "flow": "registry-dispatch",
            "key_sequence": ["2"],
            "terminal_state": {"current_mode": "autopilot"},
            "state_path": _REPO_STATE_REL,
        },
    )
    fn = CHECK_REGISTRY["tui_flow"]
    result = fn(spec, _REPO_ROOT)
    assert result.status == "pass", result.details


def test_tui_flow_registered_in_registry_module() -> None:
    # The registry reference is the non-test production call-site that
    # discharges the idle-contract gate; assert it is wired.
    from eawf.workflow.audit_dsl import registry as registry_module

    assert "tui_flow" in registry_module.CHECK_REGISTRY
    source = inspect.getsource(registry_module)
    assert "check_tui_flow" in source


def test_check_kind_literal_source_contains_tui_flow() -> None:
    source = inspect.getsource(models_module)
    assert '"tui_flow"' in source
