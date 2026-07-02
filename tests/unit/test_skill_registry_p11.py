"""Phase 11 acceptance: plan-mode prep + AskUserQuestion flow gates.

Pins the SKILL_REGISTRY and BUILT_IN_DEFAULTS contracts introduced in
P11:

- ``planning.auto_plan`` exists and defaults to ``False`` — the
  ``/prep`` skill body relies on this gate to decide whether to enter
  Claude Code plan mode before dispatching waves.
- ``flow.auto_accept`` exists with a per-stage ``bool`` map covering
  every step of the six-skill canonical flow, each defaulting to
  ``False`` (i.e. "ask the operator between every stage" out of the
  box).
- ``flow.ask_on_decisions`` defaults to ``True`` so skill bodies
  remain encouraged to surface discrete decisions through
  ``AskUserQuestion``.
- ``_PREP_BODY`` references ``planning.auto_plan`` and the Claude
  Code plan-mode primitive.
- ``_FLOW_BODY`` references ``flow.auto_accept`` and surfaces the
  inter-stage gate through ``AskUserQuestion``.
- Every other core skill body (``_RESEARCH_BODY``, ``_AUDIT_BODY``,
  ``_SHIP_BODY``, ``_REVIEW_BODY``, ``_POLISH_BODY``) mentions
  ``AskUserQuestion`` so operators do not get bounced to free-text
  prompts.
- ``prep`` and ``flow`` ``argument_hint`` strings expose the new
  ``--auto-plan`` and ``--auto-accept`` flags respectively.
"""

from __future__ import annotations

import pytest

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.surfaces.render.skills import SKILL_REGISTRY
from eawf.surfaces.render.skills.registry import (
    _GOTCHA_DAEMON_RESTART,
    _GOTCHA_DISPATCH_RESUME,
    _GOTCHA_FULL_TREE_GAUNTLET,
    _GOTCHA_NO_EAWF_IN_WORKTREE,
    _GOTCHA_OUT_OF_ORDER,
    _GOTCHA_RECONCILE_FILE_SCOPES,
    _GOTCHA_STATE_BOOKKEEPING,
)

_FLOW_STAGES: tuple[str, ...] = (
    "research",
    "prep",
    "audit",
    "ship",
    "review",
    "polish",
)


#: The operator-gotcha clauses each named skill's frozen registry body must
#: carry, per the belt-and-braces homes in the skills-agents-hardening spec
#: (P30-I23-W42). Asserted through ``SKILL_REGISTRY`` so the shipped surface
#: -- the body ``eawf plugin install`` renders -- is what is checked, not
#: just the source constant.
_GOTCHA_REGISTRY_HOMES: dict[str, tuple[str, ...]] = {
    "prep": (
        _GOTCHA_DISPATCH_RESUME,
        _GOTCHA_OUT_OF_ORDER,
        _GOTCHA_NO_EAWF_IN_WORKTREE,
        _GOTCHA_STATE_BOOKKEEPING,
        _GOTCHA_RECONCILE_FILE_SCOPES,
        _GOTCHA_DAEMON_RESTART,
    ),
    "flow": (_GOTCHA_DISPATCH_RESUME, _GOTCHA_STATE_BOOKKEEPING),
    "agent-dispatch": (_GOTCHA_DISPATCH_RESUME, _GOTCHA_OUT_OF_ORDER),
    "ship": (_GOTCHA_STATE_BOOKKEEPING, _GOTCHA_FULL_TREE_GAUNTLET),
    "audit": (_GOTCHA_FULL_TREE_GAUNTLET,),
}


#: P30-I23-W44 (SKH-7): the runtime-option flag tokens each touched skill's
#: ``argument_hint`` MUST advertise, per section 4 of the
#: skills-agents-hardening spec. Existing-engine options (research ``--depth``
#: / ``--final``; flow ``--stop-after`` / ``--resume`` / ``--args-per-step``;
#: audit ``--kind``) match their verified parse sites; the rest document the
#: interface the sibling engine waves (SKH-8a/8b) wire.
_SECTION4_HINT_OPTIONS: dict[str, tuple[str, ...]] = {
    "research": ("--depth", "--final", "--rounds", "--agents", "--budget"),
    "prep": ("--auto-resume", "--out-of-order", "--ceremony", "--runtime"),
    "audit": ("--kind", "--level", "--enforce"),
    "ship": ("--dry-run", "--gauntlet", "--release", "--skip-pr-pass"),
    "review": ("--level", "--criteria"),
    "polish": ("--scope", "--auto-apply-safe", "--category"),
    "roadmap": ("--dry-run", "--criteria-from-brief"),
    "spike": ("--rounds", "--axes-per-round", "--worktree"),
    "flow": (
        "--auto-accept",
        "--stop-after",
        "--resume",
        "--args-per-step",
        "--caps",
        "--max-repair-cycles",
    ),
    "agent-dispatch": ("--runtime", "--headless", "--model"),
}

#: The skills whose bodies gain a ``## Options`` section in this wave (CR-02).
#: Exactly the ``argument_hint``-touched skills, so hint and body stay paired.
_OPTIONS_SECTION_SKILLS: tuple[str, ...] = tuple(_SECTION4_HINT_OPTIONS)


def _spec(name: str):
    return next(s for s in SKILL_REGISTRY if s.skill_name == name)


def test_planning_auto_plan_defaults_false() -> None:
    planning = BUILT_IN_DEFAULTS["planning"]
    assert "auto_plan" in planning
    assert planning["auto_plan"] is False


def test_flow_auto_accept_covers_every_stage_default_false() -> None:
    flow = BUILT_IN_DEFAULTS["flow"]
    assert "auto_accept" in flow
    auto = flow["auto_accept"]
    assert set(auto) == set(_FLOW_STAGES)
    for stage, value in auto.items():
        assert value is False, f"flow.auto_accept.{stage} should default to False"


def test_flow_ask_on_decisions_defaults_true() -> None:
    assert BUILT_IN_DEFAULTS["flow"]["ask_on_decisions"] is True


def test_prep_body_documents_planned_scope_activation() -> None:
    """P19-W07: prep body describes the PLANNED -> ACTIVE flow."""
    body = _spec("prep").body
    assert "PLANNED" in body
    assert "EnterPlanMode" in body
    assert "eawf phase activate" in body
    assert "/roadmap" in body


def test_flow_body_documents_per_stage_gate_and_ask_user_question() -> None:
    body = _spec("flow").body
    assert "flow.auto_accept" in body
    assert "AskUserQuestion" in body


def test_flow_argument_hint_advertises_auto_accept_flag() -> None:
    hint = _spec("flow").argument_hint
    assert "<task-slug>" in hint
    assert "--auto-accept=<stage>[,<stage>...]" in hint


def test_remaining_core_skill_bodies_mention_ask_user_question() -> None:
    for name in ("research", "audit", "ship", "review", "polish"):
        body = _spec(name).body
        assert "AskUserQuestion" in body, (
            f"skill {name!r} body must reference AskUserQuestion so discrete"
            " operator decisions surface through the UI prompt"
        )


def test_skill_registry_bodies_carry_operator_gotchas() -> None:
    """P30-I23-W42: each named skill's frozen body carries its gotcha clauses.

    Reads through ``SKILL_REGISTRY`` (the shipped surface) rather than the
    source constant so a body that resolves to a stale spec fails here.
    """
    for name, clauses in _GOTCHA_REGISTRY_HOMES.items():
        body = _spec(name).body
        for clause in clauses:
            assert clause in body, (
                f"skill {name!r} registry body is missing an operator-gotcha clause"
            )


# --- P30-I23-W44 (SKH-7): runtime-option hints + ## Options bodies ----------


@pytest.mark.parametrize("name", sorted(_SECTION4_HINT_OPTIONS))
def test_argument_hint_advertises_section4_runtime_options(name: str) -> None:
    """CR-01: each touched skill's ``argument_hint`` carries its section-4 flags."""
    hint = _spec(name).argument_hint
    for opt in _SECTION4_HINT_OPTIONS[name]:
        assert opt in hint, f"skill {name!r} argument_hint is missing the runtime option {opt!r}"


def test_ship_argument_hint_retains_dry_run() -> None:
    """CR-01: ``/ship`` RETAINS ``--dry-run`` alongside the new options."""
    assert "--dry-run" in _spec("ship").argument_hint


def test_agent_dispatch_hint_omits_sandbox_profile() -> None:
    """CR-01: ``--sandbox-profile`` is P31 (no receiving seam) and stays off the hint."""
    assert "--sandbox-profile" not in _spec("agent-dispatch").argument_hint


@pytest.mark.parametrize("name", sorted(_OPTIONS_SECTION_SKILLS))
def test_touched_body_carries_options_section(name: str) -> None:
    """CR-02: every hint-touched body documents its options in a ``## Options`` block."""
    assert "## Options" in _spec(name).body, (
        f"skill {name!r} body must carry a ## Options section documenting its flags"
    )


def test_flow_options_documents_model_executed_auto_accept() -> None:
    """CR-02: the flow ``## Options`` note that auto-accept is model-executed."""
    body = _spec("flow").body
    options = body.split("## Options", 1)[1]
    assert "flow.auto_accept" in options
    assert "executed by YOU (the model)" in options
    assert "does NOT enforce auto-accept" in options


def test_ship_options_documents_model_executed_dry_run() -> None:
    """CR-02: the ship ``## Options`` note that ``--dry-run`` is model-executed prose."""
    options = _spec("ship").body.split("## Options", 1)[1]
    assert "--dry-run" in options
    assert "model-executed" in options
    # The engine parses commit/push/pr, not a dry_run arg.
    assert "workflow/skills/ship.py" in options


# ---- W47: tense-accurate enforcement prose + live paths ---------------------


def test_audit_and_ship_bodies_teach_live_enforcement() -> None:
    """CR-01: no 'advisory by default' overclaim survives; the tiered truth
    (daemon gates execute; only an unearned jury veto stays advisory) and
    the green-close caveat are taught instead."""
    import inspect

    from eawf.surfaces.render.skills import registry as skill_registry

    source = inspect.getsource(skill_registry)
    assert "Enforcement is advisory by default" not in source
    assert "Enforcement is profile-driven" in source
    assert source.count("Do not treat a green close") >= 2


def test_prep_body_points_at_rendered_planner_path() -> None:
    import inspect

    from eawf.surfaces.render.skills import registry as skill_registry

    source = inspect.getsource(skill_registry)
    assert ".claude/agents/planner.md" in source
    assert "build/eawf-plugin/agents/planner.md" not in source


def test_skill_authoring_docs_name_the_overlay_limitation() -> None:
    from pathlib import Path

    concepts = (Path(__file__).resolve().parents[2] / "docs" / "concepts.md").read_text(
        encoding="utf-8"
    )
    assert "reach `eawf skill run`, not the Claude slash surface" in concepts
