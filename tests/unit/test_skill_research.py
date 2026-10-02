"""Unit tests for :class:`eawf.workflow.skills.research.ResearchSkill`.

Pin the headless ``/research`` contract:

- A headless run has no agent to answer questions, so it refuses with
  ``status=blocked``, a ``campaign_required`` warning, and a repair route
  through ``eawf campaign new`` / ``eawf campaign run``.
- It invents no placeholder rows: no synthetic question slots or
  persisted brief, at any depth.
- The scope's live OpenQuestion rows surface in the body and become the
  route's ``--question`` values.
- ``--depth`` / ``--agents`` (and their config leaves) shape the route;
  ``--rounds`` / ``--budget`` are recorded on the resolve-scope trace.
- Probe-blocked path → ``status=blocked`` with the probe's repair commands.

The tests use ``EA_STATE`` to redirect the active state path under a
``tmp_path`` so the engine appends events into a sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import orjson
import pytest

from eawf.surfaces.render.envelope import EnvelopeWarning, OutputEnvelope
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.engine import ProbeOutcome, SkillContext, run_skill
from eawf.workflow.skills.research import ResearchSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    # Isolate the global config layer so the no-flag depth resolves to the
    # built-in ``medium`` default rather than the developer's machine-global
    # ``research.default_depth`` leaf (the stage now reads that leaf).
    from eawf.kernel.config import layered

    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "absent-global.yaml")
    return state_dir


def _ctx(args: dict[str, object] | None = None) -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
        args=dict(args or {}),
    )


def _body(env: OutputEnvelope) -> ResearchBody:
    return ResearchBody.model_validate(cast(dict, env.body))


def _route(env: OutputEnvelope) -> str:
    """Return the ``eawf campaign new`` repair command of a refused run."""
    assert env.footer.repair_commands
    return env.footer.repair_commands[0]


def test_research_headless_refuses_with_campaign_route(state_dir: Path) -> None:
    env = run_skill(ResearchSkill(), _ctx({"topic": "dispatch planning"}))

    assert env.header.skill == "/research"
    assert env.header.status == "blocked", env.body
    refusal = next(w for w in env.footer.warnings if w.code == "campaign_required")
    assert "eawf campaign new" in refusal.detail
    assert env.footer.repair_commands == [
        "eawf campaign new 'dispatch planning' --track <track-urn>"
        " --question 'dispatch planning' --depth medium --agents 4"
        " --actor <principal-key> --run",
        "eawf campaign run <CAM-####> --actor <principal-key>",
    ]
    assert env.footer.next_valid_actions == ["eawf campaign new", "eawf campaign run"]


@pytest.mark.parametrize("depth", ["shallow", "medium", "deep", "exhaustive"])
def test_research_headless_invents_no_placeholder_rows(state_dir: Path, depth: str) -> None:
    env = run_skill(ResearchSkill(), _ctx({"depth": depth}))

    body = _body(env)
    assert body.brief_id.startswith("BR-")
    assert body.questions == []
    assert body.persisted_brief is None
    assert f"--depth {depth} " in _route(env)


def test_research_names_no_retired_verb(state_dir: Path) -> None:
    env = run_skill(ResearchSkill(), _ctx())
    actions = [*env.footer.next_valid_actions, *env.footer.repair_commands]
    assert not any(
        a.startswith(("eawf prep", "eawf migrate", "eawf skill run /blitz")) for a in actions
    )


def test_research_save_persists_no_brief(state_dir: Path) -> None:
    env = run_skill(ResearchSkill(), _ctx({"topic": "demo topic", "final": True}))
    assert _body(env).persisted_brief is None
    assert not (state_dir / "store" / "research.jsonl").exists()


def test_research_topic_defaults_to_scope(state_dir: Path) -> None:
    route = _route(run_skill(ResearchSkill(), _ctx()))
    assert route.startswith("eawf campaign new urn:eawf:v1:state:QR/P00 --track")


def test_research_invalid_depth_falls_back_to_medium(state_dir: Path) -> None:
    route = _route(run_skill(ResearchSkill(), _ctx({"depth": "wat"})))
    assert "--depth medium " in route


def test_research_emits_scope_and_refusal_events(state_dir: Path) -> None:
    env = run_skill(ResearchSkill(), _ctx())
    lines = (state_dir / "store" / "event.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [orjson.loads(raw)["payload"]["event_type"] for raw in lines]
    assert kinds == ["research.resolve_scope", "research.campaign_required"]
    assert len(env.footer.persisted_store_records) == 2


def test_research_probe_blocked_when_hard_tool_missing(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the probe to report a blocked outcome and verify the engine
    short-circuits to ``status=blocked`` with non-empty repair commands."""
    from eawf.workflow.skills import research as research_module

    def _blocked_probe(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=False,
            instrument_probe={"git": "missing"},
            repair_commands=["brew install git"],
            warnings=[EnvelopeWarning(code="instrument_missing", detail="git absent")],
        )

    monkeypatch.setattr(research_module.ResearchSkill, "probe", _blocked_probe)
    env = run_skill(research_module.ResearchSkill(), _ctx())
    assert env.header.status == "blocked"
    assert env.footer.repair_commands == ["brew install git"]
    # No events written when probe blocks.
    events_path = state_dir / "store" / "event.jsonl"
    assert not events_path.exists() or events_path.read_text(encoding="utf-8") == ""


def test_research_skill_registered_with_canonical_name() -> None:
    from eawf.workflow.skills import registry

    cls = registry.lookup("/research")
    assert cls is ResearchSkill


# --- runtime options (rounds / agents / budget) ------------------------------


def _event_payload(state_dir: Path, event_type: str) -> dict:
    """Return the first ``event.jsonl`` payload whose ``event_type`` matches."""
    lines = (state_dir / "store" / "event.jsonl").read_text(encoding="utf-8").splitlines()
    for raw in lines:
        payload = orjson.loads(raw)["payload"]
        if payload["event_type"] == event_type:
            return cast(dict, payload)
    raise AssertionError(f"no {event_type} event in {lines}")


def test_research_agents_defaults_to_agent_count_leaf(state_dir: Path) -> None:
    """No ``--agents`` resolves the research.agent_count leaf (4)."""
    env = run_skill(ResearchSkill(), _ctx())
    payload = _event_payload(state_dir, "research.resolve_scope")
    assert payload["agents"] == 4  # research.agent_count built-in default
    assert " --agents 4 " in _route(env)


def test_research_agents_flag_shapes_the_route(state_dir: Path) -> None:
    env = run_skill(ResearchSkill(), _ctx({"depth": "deep", "agents": 2}))
    assert "--depth deep --agents 2 " in _route(env)
    assert _event_payload(state_dir, "research.campaign_required")["agents"] == 2


@pytest.mark.parametrize(("raw", "expected"), [(99, 12), (0, 1), ("wat", 4)])
def test_research_agents_flag_clamped_or_defaulted(
    state_dir: Path, raw: object, expected: int
) -> None:
    """An out-of-band ``--agents`` clamps to [1, 12]; a malformed one takes the leaf."""
    env = run_skill(ResearchSkill(), _ctx({"agents": raw}))
    assert _event_payload(state_dir, "research.resolve_scope")["agents"] == expected
    assert f" --agents {expected} " in _route(env)


def test_research_rounds_recorded_in_resolve_scope(state_dir: Path) -> None:
    """``--rounds`` is parsed and recorded on the resolve-scope trace."""
    run_skill(ResearchSkill(), _ctx({"rounds": 3}))
    payload = _event_payload(state_dir, "research.resolve_scope")
    assert payload["rounds"] == 3


def test_research_rounds_below_floor_defaults_to_one(state_dir: Path) -> None:
    """A below-floor / malformed ``--rounds`` degrades to a single round."""
    run_skill(ResearchSkill(), _ctx({"rounds": "nope"}))
    payload = _event_payload(state_dir, "research.resolve_scope")
    assert payload["rounds"] == 1


def test_research_budget_recorded_in_resolve_scope(state_dir: Path) -> None:
    """``--budget`` is parsed and recorded; absent it stays ``None`` (uncapped)."""
    run_skill(ResearchSkill(), _ctx({"budget": 5000}))
    payload = _event_payload(state_dir, "research.resolve_scope")
    assert payload["budget"] == 5000


def test_research_budget_absent_is_none(state_dir: Path) -> None:
    run_skill(ResearchSkill(), _ctx())
    payload = _event_payload(state_dir, "research.resolve_scope")
    assert payload["budget"] is None


def _write_state_with_open_questions(state_dir: Path, *, live: int = 2) -> None:
    """Write a valid state.json carrying *live* open questions plus one dropped."""
    titles = ["which curve model fits the short tenor", "is the venue feed authoritative"]
    titles += [f"extra question {i}" for i in range(live - len(titles))]
    rows = {
        f"OQ-{i + 1}": {
            "id": f"OQ-{i + 1}",
            "scope_id": "QR",
            "title": title,
            "status": "blocked" if i == 1 else "open",
            "blocking": i == 1,
            "urgency": "normal",
            "created_at": "2026-06-11T12:00:00+00:00",
        }
        for i, title in enumerate(titles[:live])
    }
    rows["OQ-99"] = {
        "id": "OQ-99",
        "scope_id": "QR",
        "title": "should we drop the stale source",
        "status": "dropped",
        "blocking": False,
        "urgency": "low",
        "created_at": "2026-06-11T12:00:00+00:00",
    }
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-06-11T12:00:00+00:00",
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "open_questions": rows,
    }
    (state_dir / "state.json").write_bytes(orjson.dumps(payload))


@pytest.fixture
def ok_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.workflow.skills import research as research_module

    def _ok_probe(self: object, ctx: SkillContext) -> ProbeOutcome:
        return ProbeOutcome(
            ok=True, instrument_probe={"git": "ok"}, repair_commands=[], warnings=[]
        )

    monkeypatch.setattr(research_module.ResearchSkill, "probe", _ok_probe)


@pytest.mark.usefixtures("ok_probe")
def test_research_surfaces_live_open_questions_into_the_route(state_dir: Path) -> None:
    """Live OpenQuestion rows surface in the body and become the route's questions."""
    _write_state_with_open_questions(state_dir)
    env = run_skill(ResearchSkill(), _ctx({"topic": "curves"}))
    body = _body(env)
    assert [(q.q, q.answer) for q in body.questions] == [
        ("which curve model fits the short tenor", "(open)"),
        ("is the venue feed authoritative", "(blocking)"),
    ]
    assert (
        "--question 'which curve model fits the short tenor'"
        " --question 'is the venue feed authoritative' --depth"
    ) in _route(env)
    assert "--question curves" not in _route(env)


@pytest.mark.usefixtures("ok_probe")
def test_research_route_caps_questions_at_campaign_limit(state_dir: Path) -> None:
    """A Campaign takes at most 12 questions; the body still lists every live row."""
    _write_state_with_open_questions(state_dir, live=13)
    env = run_skill(ResearchSkill(), _ctx())
    assert len(_body(env).questions) == 13
    assert _route(env).count("--question ") == 12


@pytest.mark.usefixtures("ok_probe")
def test_research_explicit_question_leads_the_route(state_dir: Path) -> None:
    """The invocation's ``--question`` comes first, then the scope's live rows."""
    _write_state_with_open_questions(state_dir, live=1)
    env = run_skill(ResearchSkill(), _ctx({"topic": "caches", "question": "does LRU win"}))
    assert (
        "--question 'does LRU win' --question 'which curve model fits the short tenor' --depth"
    ) in _route(env)
