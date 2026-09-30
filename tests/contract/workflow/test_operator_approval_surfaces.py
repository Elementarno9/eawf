"""SURF-073, SURF-091 and SURF-111 on the instructions eawf ships to agents.

The daemon only binds a choice that reaches it: an agent told to ask through a
bare ``AskUserQuestion``, or a Codex agent told to print a numbered prompt of its
own making, asks a question no pending action records, and its answer is free
text nobody can replay. So every shipped paragraph that tells an agent how to
put a choice to the operator must route it through ``eawf question
open-decision``. The surfaces checked are the ones a live session reads: the
rendered ``AGENTS.md`` with its reference expansions for the profiles this
repository enables, the role bodies the plugin installs, and every skill page.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from eawf.platform.profiles import compose, load_profile
from eawf.surfaces.render.agents import AGENT_REGISTRY, AgentSpec
from eawf.surfaces.render.agents_md import render_agents_md
from eawf.surfaces.render.manifest import Manifest
from eawf.workflow.skills.bodies.chassis import shipped_skill_page
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry

pytestmark = pytest.mark.contract

#: The profiles this repository enables, as the committed AGENTS.md renders them.
_REPO_PROFILES: Final = ("core", "python", "research", "agent_driven", "quality")

#: What marks a paragraph as telling an agent how to ask the operator.
_ASKS: Final = re.compile(r"AskUserQuestion|numbered (?:text )?prompt")

#: What binds the ask to a pending action.
_BINDS: Final = "question open-decision"


def _paragraphs(text: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", text) if part.strip()]


def _unbound(text: str) -> list[str]:
    return [p for p in _paragraphs(text) if _ASKS.search(p) and _BINDS not in p]


@pytest.fixture(scope="module")
def rendered_rules(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Render AGENTS.md and its reference expansions for the repository's profiles."""
    root = tmp_path_factory.mktemp("agents")
    composed = compose([load_profile(name) for name in _REPO_PROFILES])
    render_agents_md(composed, root / "AGENTS.md", Manifest(version=1, generated={}))
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
    }


def test_surf_073_the_rendered_rules_carry_an_ask(rendered_rules: dict[str, str]) -> None:
    """Boundary: the sweep below is not vacuous; the prep and decision rules ask."""
    asking = [name for name, text in rendered_rules.items() if _ASKS.search(text)]
    assert "AGENTS.md" in asking
    assert str(Path("docs/rules/orchestrator-decision-surface.md")) in asking


def test_surf_073_every_rendered_rule_that_asks_binds_the_ask(
    rendered_rules: dict[str, str],
) -> None:
    unbound = {name: _unbound(text) for name, text in rendered_rules.items()}
    assert {name: found for name, found in unbound.items() if found} == {}


def test_surf_111_the_decision_rule_gives_codex_the_bound_prompt(
    rendered_rules: dict[str, str],
) -> None:
    """Codex has no picker: it prints the answer's numbered prompt and relays the reply."""
    rule = rendered_rules[str(Path("docs/rules/orchestrator-decision-surface.md"))]
    assert "numbered prompt printed verbatim in Codex" in rule
    assert "eawf question answer" in rule
    assert "first answer wins" in rule
    assert "equivalent numbered text prompt" not in rule


@pytest.mark.parametrize("spec", AGENT_REGISTRY, ids=lambda spec: spec.role)
def test_surf_073_every_role_body_that_asks_binds_the_ask(spec: AgentSpec) -> None:
    assert _unbound(spec.body) == []


@pytest.mark.parametrize("entry", SKILL_CATALOG.entries, ids=lambda entry: entry.skill_id)
def test_surf_091_every_skill_page_that_asks_binds_the_ask(entry: SkillCatalogEntry) -> None:
    assert _unbound(shipped_skill_page(entry)) == []
