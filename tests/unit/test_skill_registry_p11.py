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

from eawf.config.defaults import BUILT_IN_DEFAULTS
from eawf.render.skills import SKILL_REGISTRY

_FLOW_STAGES: tuple[str, ...] = (
    "research",
    "prep",
    "audit",
    "ship",
    "review",
    "polish",
)


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


def test_prep_body_documents_plan_mode_gate() -> None:
    body = _spec("prep").body
    assert "planning.auto_plan" in body
    assert "EnterPlanMode" in body
    assert "ExitPlanMode" in body


def test_prep_argument_hint_advertises_auto_plan_flag() -> None:
    assert _spec("prep").argument_hint == "<phase-id> [wave-id] [--auto-plan]"


def test_flow_body_documents_per_stage_gate_and_ask_user_question() -> None:
    body = _spec("flow").body
    assert "flow.auto_accept" in body
    assert "AskUserQuestion" in body


def test_flow_argument_hint_advertises_auto_accept_flag() -> None:
    assert _spec("flow").argument_hint == ("<task-slug> [--auto-accept=<stage>[,<stage>...]]")


def test_remaining_core_skill_bodies_mention_ask_user_question() -> None:
    for name in ("research", "audit", "ship", "review", "polish"):
        body = _spec(name).body
        assert "AskUserQuestion" in body, (
            f"skill {name!r} body must reference AskUserQuestion so discrete"
            " operator decisions surface through the UI prompt"
        )
