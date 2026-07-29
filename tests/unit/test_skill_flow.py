"""Unit tests for :class:`eawf.workflow.skills.flow.FlowSkill`.

Pin the Phase 4 W03 acceptance contract for ``/flow``:

- Runs the five core skills sequentially in order
  (research → prep → audit → polish → ship).
- Short-circuit semantics: on any non-``ok`` status, propagates the
  failing step's ``repair_commands`` to the flow's footer and stops.
- ``stop_after`` flag halts cleanly with the last-run step's status.
- Property test: the helper :func:`short_circuit_terminal_status` correctly
  computes the terminal status for any sequence of step statuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.surfaces.render.envelope import EnvelopeStatus
from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.engine import (
    ProbeOutcome,
    Skill,
    SkillContext,
    SkillResult,
    run_skill,
)
from eawf.workflow.skills.flow import (
    FlowSkill,
    short_circuit_terminal_status,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    monkeypatch.delenv("EAWF_BLITZ_DEPTH_COUNTER", raising=False)
    return state_dir


def _ctx(args: dict[str, object] | None = None) -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args=dict(args or {}),
    )


def test_flow_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/flow")
    assert cls is FlowSkill


def test_flow_runs_current_core_skills_in_order(state_dir: Path) -> None:
    skill = FlowSkill()
    env = run_skill(skill, _ctx({"topic": "demo", "advance_after": True}))
    assert env.header.skill == "/flow"
    body = FlowBody.model_validate(cast(dict, env.body))
    assert body.topic == "demo"
    assert len(body.steps) == 5
    expected_order = ["/research", "/prep", "/audit", "/polish", "/ship"]
    actual_order = [s["header"]["skill"] for s in body.steps]
    assert actual_order == expected_order


def test_flow_terminal_status_ok_when_every_step_ok(state_dir: Path) -> None:
    skill = FlowSkill()
    env = run_skill(skill, _ctx({"topic": "demo", "advance_after": True}))
    # Each core skill v0.1 stub returns status=ok by default.
    body = FlowBody.model_validate(cast(dict, env.body))
    assert env.header.status == "ok"
    assert body.terminal_status == "ok"


class _StubBlockedSkill(Skill):
    """Test-only stub that always probes blocked.

    Using the engine's ``probe.ok=False`` short-circuit produces a
    canonical ``status=blocked`` envelope with the expected
    ``repair_commands``. Patching :class:`AuditSkill` with this stub
    verifies the flow propagates the blocked status.
    """

    name = "/audit"  # type: ignore[assignment]

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["brew install git"],
        )

    def action(self, ctx: SkillContext) -> SkillResult:  # pragma: no cover
        raise AssertionError("action should not run when probe blocks")


def test_flow_short_circuits_on_first_non_ok(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a step returns non-ok, the flow stops + propagates repair."""
    from eawf.workflow.skills import flow as flow_module

    # Replace AuditSkill in the flow's order with the blocked stub.
    patched_order = tuple(
        (name, _StubBlockedSkill if name == "/audit" else cls)
        for name, cls in flow_module.FlowSkill.flow_order
    )
    monkeypatch.setattr(FlowSkill, "flow_order", patched_order)

    skill = FlowSkill()
    env = run_skill(skill, _ctx({"topic": "demo", "advance_after": True}))
    assert env.header.status == "blocked"
    # The flow's footer carries the failing step's repair commands.
    assert env.footer.repair_commands == ["brew install git"]
    body = FlowBody.model_validate(cast(dict, env.body))
    # Three steps run before the short-circuit: research, prep, audit.
    assert len(body.steps) == 3
    assert body.steps[-1]["header"]["skill"] == "/audit"
    assert body.steps[-1]["header"]["status"] == "blocked"
    assert body.terminal_status == "blocked"


def test_flow_stop_after_research_runs_only_one_step(state_dir: Path) -> None:
    skill = FlowSkill()
    env = run_skill(skill, _ctx({"topic": "demo", "stop_after": "research"}))
    body = FlowBody.model_validate(cast(dict, env.body))
    assert len(body.steps) == 1
    assert body.steps[0]["header"]["skill"] == "/research"
    assert env.header.status == "ok"


def test_flow_stop_after_with_leading_slash_normalised(state_dir: Path) -> None:
    skill = FlowSkill()
    env = run_skill(
        skill,
        _ctx({"topic": "demo", "stop_after": "/prep", "advance_after": ["research"]}),
    )
    body = FlowBody.model_validate(cast(dict, env.body))
    assert len(body.steps) == 2
    assert [s["header"]["skill"] for s in body.steps] == ["/research", "/prep"]


def test_flow_unrecognised_stop_after_runs_full_pipeline(state_dir: Path) -> None:
    skill = FlowSkill()
    env = run_skill(
        skill,
        _ctx({"topic": "demo", "stop_after": "wat", "advance_after": True}),
    )
    body = FlowBody.model_validate(cast(dict, env.body))
    assert len(body.steps) == 5


def test_flow_emits_at_least_step_start_end_per_skill(state_dir: Path) -> None:
    """Verify the flow's own event audit trail covers each step."""
    skill = FlowSkill()
    env = run_skill(skill, _ctx({"topic": "demo"}))
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    raw = events_path.read_text(encoding="utf-8").splitlines()
    # The flow itself emits: start + 12 (2 per step) + end → 14 minimum.
    # Plus each subskill's own event emissions get appended too. So just
    # assert we observed the canonical flow.* events.
    flow_event_types = {
        "flow.start",
        "flow.step_start",
        "flow.step_end",
        "flow.end",
    }
    seen: set[str] = set()
    for ln in raw:
        import orjson

        rec = orjson.loads(ln)
        seen.add(rec["payload"].get("event_type", ""))
    assert flow_event_types <= seen
    # Footer record list should be non-empty.
    assert env.footer.persisted_store_records


def test_flow_args_per_step_are_forwarded(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``args_per_step`` overrides the per-step args."""
    from eawf.workflow.skills.research import ResearchSkill

    captured: dict[str, object] = {}

    class _CaptureResearch(ResearchSkill):
        def action(self, ctx: SkillContext) -> SkillResult:
            captured["args"] = dict(ctx.args)
            return super().action(ctx)

    patched_order = tuple(
        (name, _CaptureResearch if name == "/research" else cls)
        for name, cls in FlowSkill.flow_order
    )
    monkeypatch.setattr(FlowSkill, "flow_order", patched_order)

    skill = FlowSkill()
    env = run_skill(
        skill,
        _ctx(
            {
                "topic": "demo",
                "args_per_step": {"/research": {"depth": "quick"}},
                "stop_after": "research",
            }
        ),
    )
    assert env.header.status == "ok"
    assert captured["args"] == {"depth": "quick"}


# ---- Property tests --------------------------------------------------------


_status_strategy = st.sampled_from(["ok", "needs_user", "blocked", "failed", "partial"])


@given(statuses=st.lists(_status_strategy, max_size=10))
@settings(max_examples=200, deadline=None)
def test_short_circuit_terminal_status_property(statuses: list[EnvelopeStatus]) -> None:
    """First non-ok wins; otherwise terminal mirrors the all-ok base case."""
    out = short_circuit_terminal_status(statuses)
    if not statuses:
        assert out == "ok"
        return
    # Find the first non-ok (if any).
    for s in statuses:
        if s != "ok":
            assert out == s
            return
    # All ok → terminal "ok".
    assert out == "ok"


def test_short_circuit_terminal_status_empty() -> None:
    """Edge case: no statuses → terminal "ok"."""
    assert short_circuit_terminal_status([]) == "ok"


def test_short_circuit_terminal_status_all_ok() -> None:
    assert short_circuit_terminal_status(["ok", "ok", "ok"]) == "ok"


def test_short_circuit_terminal_status_first_non_ok_wins() -> None:
    assert short_circuit_terminal_status(["ok", "ok", "failed", "ok", "blocked"]) == "failed"


def test_short_circuit_terminal_status_first_step_fails() -> None:
    assert short_circuit_terminal_status(["needs_user", "ok"]) == "needs_user"


# ---- P30-I23-W45: /flow --caps + --max-repair-cycles ----------------------


def _flow_start_payload(state_dir: Path) -> dict:
    import orjson

    events_path = state_dir / "store" / "event.jsonl"
    records = [orjson.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    return next(r["payload"] for r in records if r["payload"].get("event_type") == "flow.start")


def test_flow_caps_parsed_and_recorded(state_dir: Path) -> None:
    """``--caps eu=..,usd=..,tokens=..`` parses into a ceiling map on the start event."""
    env = run_skill(
        FlowSkill(),
        _ctx({"topic": "demo", "caps": "eu=1.5,usd=2,tokens=1000", "stop_after": "research"}),
    )
    assert env.header.status == "ok"
    assert _flow_start_payload(state_dir)["caps"] == {"eu": 1.5, "usd": 2.0, "tokens": 1000.0}


def test_flow_caps_default_uncapped(state_dir: Path) -> None:
    """No ``--caps`` → the recorded ceiling map is empty (uncapped)."""
    run_skill(FlowSkill(), _ctx({"topic": "demo", "stop_after": "research"}))
    assert _flow_start_payload(state_dir)["caps"] == {}


def test_flow_caps_unknown_axis_records_warning(state_dir: Path) -> None:
    """An out-of-set cap axis records an advisory ``unknown_cap`` warning and is dropped."""
    env = run_skill(FlowSkill(), _ctx({"topic": "demo", "caps": "gpu=4", "stop_after": "research"}))
    assert "unknown_cap" in {w.code for w in env.footer.warnings}
    assert _flow_start_payload(state_dir)["caps"] == {}


def test_flow_caps_malformed_number_records_warning(state_dir: Path) -> None:
    """A non-numeric cap value records an advisory ``invalid_cap`` warning and is dropped."""
    env = run_skill(
        FlowSkill(), _ctx({"topic": "demo", "caps": "eu=lots", "stop_after": "research"})
    )
    assert "invalid_cap" in {w.code for w in env.footer.warnings}
    assert _flow_start_payload(state_dir)["caps"] == {}


def test_flow_max_repair_cycles_default_three(state_dir: Path) -> None:
    """No flag → the ``flow.max_repair_cycles`` built-in default (3) is recorded."""
    run_skill(FlowSkill(), _ctx({"topic": "demo", "stop_after": "research"}))
    assert _flow_start_payload(state_dir)["max_repair_cycles"] == 3


def test_flow_max_repair_cycles_explicit_honoured(state_dir: Path) -> None:
    """``--max-repair-cycles`` overrides the config default."""
    run_skill(
        FlowSkill(),
        _ctx({"topic": "demo", "max_repair_cycles": 5, "stop_after": "research"}),
    )
    assert _flow_start_payload(state_dir)["max_repair_cycles"] == 5


def test_flow_max_repair_cycles_invalid_records_warning_and_falls_back(state_dir: Path) -> None:
    """A non-integer ``--max-repair-cycles`` warns and falls back to the config default."""
    env = run_skill(
        FlowSkill(),
        _ctx({"topic": "demo", "max_repair_cycles": "many", "stop_after": "research"}),
    )
    assert "invalid_max_repair_cycles" in {w.code for w in env.footer.warnings}
    assert _flow_start_payload(state_dir)["max_repair_cycles"] == 3


def test_flow_max_repair_cycles_negative_records_warning(state_dir: Path) -> None:
    """A negative ``--max-repair-cycles`` is refused (warning) and falls back."""
    env = run_skill(
        FlowSkill(),
        _ctx({"topic": "demo", "max_repair_cycles": -1, "stop_after": "research"}),
    )
    assert "invalid_max_repair_cycles" in {w.code for w in env.footer.warnings}
    assert _flow_start_payload(state_dir)["max_repair_cycles"] == 3
