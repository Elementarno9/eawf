"""P30-I21-W35 (G9): scope-wide questions get their own topic-tree section.

The topic tree grouped scope-wide (project-scoped) questions under an arbitrary
first campaign node, which misread them as that one campaign's research gaps.
This wave renders them as their own scope-level ``scope questions`` section after
the campaigns, so project-scoped questions never appear under a campaign node.
"""

from __future__ import annotations

from eawf.surfaces.tui.modes.research_board import NodeKind, build_tree_nodes
from tests.tui.test_modes_research_board import _campaign_row, _question


def test_scope_questions_never_nest_under_a_campaign() -> None:
    """No question node carries a campaign_id; each hangs at the scope level."""
    campaigns = (_campaign_row(campaign_id="RC-0001"), _campaign_row(campaign_id="RC-0002"))
    questions = (_question("OQ-0001"), _question("OQ-0002"))
    nodes = build_tree_nodes(campaigns, questions)
    question_nodes = [node for node in nodes if node.kind is NodeKind.QUESTION]
    assert len(question_nodes) == 2
    assert all(node.campaign_id is None for node in question_nodes)


def test_scope_questions_render_in_own_section_after_campaigns() -> None:
    """The scope-questions round is a depth-0 section rendered after the campaigns."""
    campaigns = (_campaign_row(campaign_id="RC-0001"),)
    nodes = build_tree_nodes(campaigns, (_question("OQ-0001"),))
    scope_rounds = [n for n in nodes if n.kind is NodeKind.ROUND and n.campaign_id is None]
    assert len(scope_rounds) == 1
    assert scope_rounds[0].depth == 0
    assert scope_rounds[0].label.startswith("scope questions")
    # It renders after the campaign's own (campaign_id-carrying) round.
    campaign_round_index = next(
        i for i, n in enumerate(nodes) if n.kind is NodeKind.ROUND and n.campaign_id == "RC-0001"
    )
    scope_round_index = nodes.index(scope_rounds[0])
    assert campaign_round_index < scope_round_index


def test_campaign_less_scope_still_renders_scope_questions() -> None:
    """A question-only scope (no campaign) still surfaces the scope-questions section."""
    nodes = build_tree_nodes((), (_question("OQ-0001"),))
    assert nodes[0].kind is NodeKind.ROUND
    assert nodes[0].campaign_id is None
    assert nodes[0].depth == 0
    assert nodes[1].kind is NodeKind.QUESTION


def test_no_questions_emits_no_scope_section() -> None:
    """A scope with campaigns but no questions emits no scope-questions round."""
    nodes = build_tree_nodes((_campaign_row(campaign_id="RC-0001"),), ())
    assert not any(n.kind is NodeKind.ROUND and n.campaign_id is None for n in nodes)
    assert not any(n.kind is NodeKind.QUESTION for n in nodes)
