"""Plan-mode defaults and the registry skills' option surface.

Pins the SKILL_REGISTRY and BUILT_IN_DEFAULTS contracts:

- ``planning.auto_plan`` exists and defaults to ``False``.
- ``flow.advance_after`` exists with a per-transition ``bool`` map,
  each defaulting to ``False`` (i.e. "ask the operator between every
  stage" out of the box).
- The ``/research`` body mentions ``AskUserQuestion`` so operators do
  not get bounced to free-text prompts.
- Each registry skill that takes runtime options advertises them in its
  ``argument_hint`` and documents them in a ``## Options`` body section.
"""

from __future__ import annotations

import pytest

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.surfaces.render.skills import SKILL_REGISTRY

_FLOW_STAGES: tuple[str, ...] = (
    "research",
    "prep",
    "audit",
    "polish",
)


#: The runtime-option flag tokens each registry skill's ``argument_hint``
#: MUST advertise, so the hint an operator reads names every option the
#: body documents.
_SECTION4_HINT_OPTIONS: dict[str, tuple[str, ...]] = {
    "research": ("--depth", "--final", "--rounds", "--agents", "--budget"),
    "spike": ("--rounds", "--axes-per-round", "--worktree"),
}

#: The skills whose bodies carry a ``## Options`` section: exactly the
#: skills with advertised options, so hint and body stay paired.
_OPTIONS_SECTION_SKILLS: tuple[str, ...] = tuple(_SECTION4_HINT_OPTIONS)


def _spec(name: str):
    return next(s for s in SKILL_REGISTRY if s.skill_name == name)


def test_flow_advance_after_covers_every_transition_default_false() -> None:
    flow = BUILT_IN_DEFAULTS["flow"]
    assert "advance_after" in flow
    advance_after = flow["advance_after"]
    assert set(advance_after) == set(_FLOW_STAGES)
    for stage, value in advance_after.items():
        assert value is False, f"flow.advance_after.{stage} should default to False"


def test_flow_removed_ask_on_decisions_knob_is_absent() -> None:
    assert "ask_on_decisions" not in BUILT_IN_DEFAULTS["flow"]


def test_research_body_mentions_ask_user_question() -> None:
    assert "AskUserQuestion" in _spec("research").body, (
        "the research body must reference AskUserQuestion so discrete"
        " operator decisions surface through the UI prompt"
    )


@pytest.mark.parametrize("name", sorted(_SECTION4_HINT_OPTIONS))
def test_argument_hint_advertises_section4_runtime_options(name: str) -> None:
    """Each skill's ``argument_hint`` carries every option it takes."""
    hint = _spec(name).argument_hint
    for opt in _SECTION4_HINT_OPTIONS[name]:
        assert opt in hint, f"skill {name!r} argument_hint is missing the runtime option {opt!r}"


@pytest.mark.parametrize("name", sorted(_OPTIONS_SECTION_SKILLS))
def test_touched_body_carries_options_section(name: str) -> None:
    """Every option-taking body documents its options in a ``## Options`` block."""
    assert "## Options" in _spec(name).body, (
        f"skill {name!r} body must carry a ## Options section documenting its flags"
    )


def test_skill_authoring_docs_name_the_overlay_limitation() -> None:
    from pathlib import Path

    concepts = (Path(__file__).resolve().parents[2] / "docs" / "concepts.md").read_text(
        encoding="utf-8"
    )
    assert "reach `eawf skill run`, not the Claude slash surface" in concepts
