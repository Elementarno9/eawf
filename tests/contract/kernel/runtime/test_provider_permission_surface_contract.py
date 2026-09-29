"""The negative fixture a surface binds against: no hold and no hidden repository approval.

RUN-052: a surface that offers a hold, or offers a repository approval where the record
denies one, on a provider permission fails here. The console's Attention frame draws the
repository affordance disabled with the approving authority named rather than leaving it
out, its card never previews a hold, and the one write it sends for a permission is the
permission's own decide verb as the operator -- never the pending-action seal. The daemon
refuses the hold on its own, asserted in
``tests/integration/runtime/daemon/test_provider_permission.py``, so passing this cannot
stand in for that.
"""

from __future__ import annotations

from typing import Final

import pytest

from eawf.kernel.projection.registers import ATTENTION_ROUTE, RegisterView
from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console.attention import VERB
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.mutation import answer_card
from eawf.surfaces.tui.console.operations import (
    PERMISSION_DECIDE_METHOD,
    PERMISSION_VERBS,
    AnswerRequest,
    ConsoleOperation,
    OperationResult,
    OperationStatus,
    Operator,
    PermissionDecision,
    address,
    settled,
)
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from tests.unit.kernel.projection.test_attention_permission import permission_row, register

OPERATOR: Final = "OP-0001"
URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/permission/PERM-0001"


def frame(held: RegisterView) -> str:
    """Render the Attention route of a linked console acting as the operator."""
    session = Session()
    session.route = ATTENTION_ROUTE
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=160,
        h=30,
        register=held,
        attention=held,
        linked=True,
        principal=OPERATOR,
    )
    return "\n".join(render_route(view))


def test_run_052_no_attention_verb_is_a_hold() -> None:
    assert "hold" not in {verb.name for verb in VERB.values()}
    assert "hold" not in PERMISSION_VERBS.values()


def test_run_052_a_hold_decision_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="does not decide a provider permission"):
        PermissionDecision(target="PERM-0001", verb="hold")


def test_run_052_the_frame_draws_repository_approval_disabled_naming_the_authority() -> None:
    text = frame(register(permission_row(action_class="credential")))

    assert "repository approve disabled · approve is operator only" in text
    assert "repository approve enabled" not in text
    assert "no hold: the provider owns the deadline" in text


def test_run_052_the_frame_draws_repository_approval_only_where_the_record_grants_it() -> None:
    granted = {"approve": ["operator", "repository"], "deny": ["operator"]}

    text = frame(register(permission_row(approval_authority=granted)))

    assert "repository approve enabled" in text
    assert "repository approve disabled" not in text


def test_run_052_the_card_previews_no_hold() -> None:
    (row,) = register(permission_row()).rows

    card = answer_card(row, "a", principal=OPERATOR, now=0.0)

    (item,) = card.items
    assert card.noun == "provider permission"
    assert item.request == PermissionDecision(target="PERM-0001", verb="approve")
    assert "no hold is placed: the provider owns the deadline and it expires" in item.not_effects


@pytest.mark.parametrize("key", ["z", "v"])
def test_run_052_a_verb_that_decides_nothing_is_refused_on_the_card(key: str) -> None:
    (row,) = register(permission_row()).rows

    (item,) = answer_card(row, key, principal=OPERATOR, now=0.0).items

    assert item.request is None
    assert item.refusal is not None
    assert item.refusal.code == "unbound_verb"


def test_run_052_a_verb_the_operator_class_lacks_is_refused_on_the_card() -> None:
    split = {"approve": ["repository"], "deny": ["operator"]}
    (row,) = register(permission_row(approval_authority=split)).rows

    (approve,) = answer_card(row, "a", principal=OPERATOR, now=0.0).items
    (deny,) = answer_card(row, "x", principal=OPERATOR, now=0.0).items

    assert approve.refusal is not None
    assert approve.refusal.code == "authority_denied"
    assert "approve is for repository only" in approve.refusal.reason
    assert deny.request == PermissionDecision(target="PERM-0001", verb="deny")


def test_run_052_a_decision_is_sent_to_the_decide_verb_as_the_operator() -> None:
    operation = address(
        PermissionDecision(target="PERM-0001", verb="deny"),
        urn=URN,
        revision=1,
        operator=Operator(principal=OPERATOR),
    )

    assert isinstance(operation, ConsoleOperation)
    assert operation.method == PERMISSION_DECIDE_METHOD
    assert dict(operation.params) == {
        "urn": URN,
        "verb": "deny",
        "principal_class": "operator",
        "actor": OPERATOR,
        "expected_revision": 1,
    }


def test_run_052_an_answer_without_a_receipt_is_still_not_sent_as_a_seal() -> None:
    refused = address(
        AnswerRequest(target="ACT-0001", option_id="approve"),
        urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001",
        revision=1,
        operator=Operator(principal=OPERATOR),
    )

    assert isinstance(refused, OperationResult)
    assert refused.status is OperationStatus.REFUSED


def test_run_052_the_decide_answer_settles_as_confirmed() -> None:
    operation = address(
        PermissionDecision(target="PERM-0001", verb="approve"),
        urn=URN,
        revision=1,
        operator=Operator(principal=OPERATOR),
    )
    assert isinstance(operation, ConsoleOperation)

    result = settled(operation, {"permission": {"key": "PERM-0001", "revision": 2}})

    assert (result.status, result.disposition) == (
        OperationStatus.APPLIED,
        ControlDisposition.CONFIRMED,
    )
    assert result.detail.startswith("PERM-0001 approve confirmed")
