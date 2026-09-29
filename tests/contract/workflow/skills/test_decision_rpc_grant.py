"""A skill that reaches an operator-only choice files it through the decision verb.

SURF-091 and SURF-111: the skill page no longer tells an agent to "prepare" a pending
action by hand; it names the verb that files a presentable operator decision, and it
grants that verb, so the page never names a call its own allowlist would deny. The verb
it names is one the daemon registers, so the page cannot point at nothing.
"""

from __future__ import annotations

import pytest

import eawf.runtime.daemon.server  # noqa: F401  -- registers every daemon method
from eawf.runtime.daemon.methods import registered_methods
from eawf.workflow.skills.bodies.chassis import DECISION_RPC, assemble_skill_page
from eawf.workflow.skills.bodies.prompts import skill_prompt
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry

_ASKING = [entry for entry in SKILL_CATALOG.entries if entry.operator_only_actions]
_NOT_ASKING = [entry for entry in SKILL_CATALOG.entries if not entry.operator_only_actions]


def _authority(entry: SkillCatalogEntry) -> str:
    page = assemble_skill_page(entry, skill_prompt(entry.skill_id), ())
    return page.split("## 1. Authority", 1)[1].split("## 2.", 1)[0]


def test_surf_091_the_named_verb_is_a_registered_daemon_method() -> None:
    assert DECISION_RPC in registered_methods()


def test_surf_091_the_catalog_has_skills_on_both_sides() -> None:
    """Boundary: the two parametrised sets below are neither empty."""
    assert _ASKING
    assert _NOT_ASKING


@pytest.mark.parametrize("entry", _ASKING, ids=lambda entry: entry.skill_id)
def test_surf_091_an_operator_only_action_is_filed_through_the_decision_verb(
    entry: SkillCatalogEntry,
) -> None:
    """The page names the verb, grants it, and still forbids choosing for the operator."""
    authority = _authority(entry)

    assert "prepares a PendingAction" not in authority
    assert f"`eawf question open-decision` (`{DECISION_RPC}`)" in authority
    assert "never chooses the recommended option itself" in authority
    allowed = next(line for line in authority.splitlines() if line.startswith("- Allowed RPCs:"))
    assert f"`{DECISION_RPC}`" in allowed


@pytest.mark.parametrize("entry", _NOT_ASKING, ids=lambda entry: entry.skill_id)
def test_surf_111_a_skill_with_no_operator_only_action_is_granted_no_decision_verb(
    entry: SkillCatalogEntry,
) -> None:
    """Error path: the grant rides the operator-only line, never widening another skill."""
    assert DECISION_RPC not in _authority(entry)
