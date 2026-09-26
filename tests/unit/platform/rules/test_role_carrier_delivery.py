"""Role carriers reach the agents that run each role.

Gate-fire proof for the carriers this repository renders: its own rule graph
renders a carrier for every role its builtin rules scope, each carrier holds
every rule scoped to its role, and the policy projection leaves those rules to
the carriers. Embedding the carrier text in agent definitions is proven in
``test_role_carrier_embedding.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.rules.carriers import (
    builtin_carrier_roles,
    carrier_roles,
    carrier_target,
)
from eawf.platform.rules.modules import builtin_rule_modules
from eawf.platform.rules.render import CARD_TARGET, POLICY_TARGET, plan_rule_projections

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ROLES_MODULE = "eawf.core.roles"


@pytest.fixture(scope="module")
def repository_outputs() -> dict[str, str]:
    """Every output the render of this repository's rule graph writes."""
    return dict(plan_rule_projections(_REPO_ROOT).outputs)


# ---- gate-fire proof: carriers are rendered ---------------------------------


def test_plan_rule_projections_renders_a_carrier_per_role_in_this_repository(
    repository_outputs: dict[str, str],
) -> None:
    carriers = sorted(target for target in repository_outputs if target.endswith("/SKILL.md"))
    assert carriers, "this repository's rule graph renders no role carrier"
    assert carriers == sorted(carrier_target(role) for role in builtin_carrier_roles())


def test_render_role_carriers_delivers_each_role_rule_in_this_repository(
    repository_outputs: dict[str, str],
) -> None:
    module = builtin_rule_modules()[_ROLES_MODULE]
    for record in module.records:
        for role in record.scope.roles:
            assert record.instruction in repository_outputs[carrier_target(role)], record.rule_id


def test_policy_projection_leaves_role_rules_to_the_carriers(
    repository_outputs: dict[str, str],
) -> None:
    module = builtin_rule_modules()[_ROLES_MODULE]
    for target in (CARD_TARGET, POLICY_TARGET):
        for record in module.records:
            assert record.instruction not in repository_outputs[target], (target, record.rule_id)


# ---- builtin_carrier_roles -------------------------------------------------


def test_builtin_carrier_roles_is_sorted_and_stable() -> None:
    roles = builtin_carrier_roles()
    assert roles == tuple(sorted(roles))
    assert builtin_carrier_roles() is roles
    assert {"executor", "auditor"} <= set(roles)


def test_builtin_carrier_roles_matches_the_graph_carrier_roles() -> None:
    from eawf.platform.rules.render import _policy_graph

    _selection, graph = _policy_graph(_REPO_ROOT, home=None)
    assert carrier_roles(graph) == builtin_carrier_roles()
