"""The live path: a question the approval verb opens is one principal's and not another's.

A disposable epoch-2 canary is seeded with a Milestone in acceptance review, its verified
Batch and the evidence its journey cites. The acceptance-approval verb is called through
the daemon's own dispatch with an assignee, and the Attention register is then read back
through ``projection.attention.read`` exactly as a console reads it.

UI-017: the question lands under ``mine`` for the principal it is addressed to and not
for another, the all-principals count holds it once, and the seal -- an attributable,
compare-and-swap-checked resolution -- takes it out of every principal's count.

UI-018: the question's own firehose rows, replayed as keyed patches onto the projection
held before it was asked, give the projection a clean read gives, audience included, and
a console that attaches after it was asked delivers nothing for it.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.attention import (
    attention_all,
    attention_mine,
    delivered_revisions,
    deliveries,
)
from eawf.kernel.projection.compute import RouteProjection, patches_for_event
from eawf.kernel.projection.connection import apply_patches
from eawf.kernel.projection.registers import ATTENTION_ROUTE, RegisterView, build_register_view
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.delivery_approval import (
    DELIVERY_OPEN_APPROVAL_METHOD,
    DELIVERY_SEAL_APPROVAL_METHOD,
    ApprovalOpenParams,
    open_acceptance_approval,
)
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, needs_count
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 9, 18, 13, 0, tzinfo=UTC)
CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
MILESTONE: Final = f"{CONTAINER}/milestone/MLS-0030"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0001"
EVIDENCE: Final = f"{CONTAINER}/evidence/EVD-0002"
RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0001"
ASSIGNEE: Final = "OP-0001"
OTHER: Final = "OP-0002"
REQUESTER: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0009"}


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A canary holding MLS-0030 in review, its verified Batch and two evidence rows."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    provisioned = provision(tmp_path / "tree", code="ATTN")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            "batch": {"BAT-0007": seed_row("batch", "READY_TO_MERGE")},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE]) as session:
        for key, kind in (("EVD-0001", "decision"), ("EVD-0002", "artifact")):
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE),
                LedgerRecord(
                    collection=Epoch2Collection.EVIDENCE,
                    record_key=key,
                    status="recorded",
                    recorded_at=AT,
                    payload={
                        "id": key,
                        "kind": kind,
                        "summary": "an observation",
                        "recorded_at": AT.isoformat(),
                    },
                ),
            )
    return provisioned


def _open_params(*, assignee: str | None) -> dict[str, Any]:
    """Return the verify stage's request, addressed to ``assignee``."""
    return {
        "urn": MILESTONE,
        "actor": "SKILL-VERIFY",
        "requested_by": dict(REQUESTER),
        "steps": [
            {
                "step_id": "AS-01",
                "passed": True,
                "observation": "the install completed and reported the version",
                "evidence_kinds": ["artifact"],
                "evidence_refs": [EVIDENCE],
            }
        ],
        "accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"],
        "assignee": assignee,
    }


def _call(tree: CanaryProvision, runtime: Path, method: str, params: dict[str, Any]) -> Any:
    """Call one daemon method against the canary, the way a client does."""
    return asyncio.run(
        methods.dispatch(method, method_context(runtime), {"repo_root": str(tree.root), **params})
    )


def _attention(tree: CanaryProvision, runtime: Path) -> RegisterView:
    """Read the Attention register through the daemon's projection verb."""
    answer = _call(tree, runtime, f"projection.{ATTENTION_ROUTE}.read", {})
    return build_register_view(RouteProjection.model_validate(answer))


def _frame(register: RegisterView, *, principal: str | None) -> list[str]:
    """Render the Attention route of a linked console acting as ``principal``."""
    session = Session()
    session.route = ATTENTION_ROUTE
    view = View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=120,
        h=30,
        register=register,
        attention=register,
        linked=True,
        principal=principal,
    )
    assert needs_count(view) == int(attention_mine(register, principal=principal).value or 0)
    return render_route(view)


def test_ui_017_the_opened_question_is_mine_for_its_principal_only(
    tree: CanaryProvision, tmp_path: Path
) -> None:
    """The live register counts the question for OP-0001 and not for OP-0002."""
    runtime = tmp_path / "runtime"
    opened = _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=ASSIGNEE))
    assert opened["created"] is True
    register = _attention(tree, runtime)

    assert register.withheld == ()
    assert [row.assignee_ref for row in register.rows] == [ASSIGNEE]
    assert attention_mine(register, principal=ASSIGNEE).value == "1"
    assert attention_mine(register, principal=OTHER).value == "0"
    assert attention_all(register).value == "1"
    mine = _frame(register, principal=ASSIGNEE)
    assert "!1 NEEDS YOU" in mine[0]
    assert mine[1].startswith(" 1 mine · 1 all principals")
    other = _frame(register, principal=OTHER)
    assert "NEEDS YOU" not in other[0]
    assert other[1].startswith(" 0 mine · 1 all principals")


def test_ui_017_an_unaddressed_question_is_every_principals(
    tree: CanaryProvision, tmp_path: Path
) -> None:
    """With no assignee the question is open to each principal, and counted once overall."""
    runtime = tmp_path / "runtime"
    _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=None))
    register = _attention(tree, runtime)
    for principal in (ASSIGNEE, OTHER):
        assert attention_mine(register, principal=principal).value == "1"
    assert attention_all(register).value == "1"


def test_ui_017_the_attributable_seal_leaves_every_count(
    tree: CanaryProvision, tmp_path: Path
) -> None:
    """A stale answer is refused, the winning seal names its resolver, and no count holds it."""
    runtime = tmp_path / "runtime"
    _call(tree, runtime, DELIVERY_OPEN_APPROVAL_METHOD, _open_params(assignee=ASSIGNEE))
    seal = {
        "urn": ACTION,
        "idempotency_key": "req-seal-0001",
        "actor": ASSIGNEE,
        "resolver": {"principal_kind": "human", "principal_id": ASSIGNEE},
        "option_id": "approve",
        "receipt_ref": RECEIPT,
    }
    with pytest.raises(Exception, match="revision"):
        _call(tree, runtime, DELIVERY_SEAL_APPROVAL_METHOD, {**seal, "expected_revision": 7})
    sealed = _call(tree, runtime, DELIVERY_SEAL_APPROVAL_METHOD, {**seal, "expected_revision": 1})
    assert sealed["outcome"] == "sealed"
    assert [row["principal_id"] for row in sealed["dispositions"]] == [ASSIGNEE]
    register = _attention(tree, runtime)
    for principal in (ASSIGNEE, OTHER):
        assert attention_mine(register, principal=principal).value == "0"


def test_ui_018_a_replay_of_the_open_equals_a_clean_read_and_delivers_nothing_twice(
    tree: CanaryProvision, tmp_path: Path
) -> None:
    """The question's patches rebuild the clean read's projection, audience included."""
    runtime = tmp_path / "runtime"
    before = RouteProjection.model_validate(
        _call(tree, runtime, f"projection.{ATTENTION_ROUTE}.read", {})
    )
    commit = open_acceptance_approval(
        root_context(tree, runtime),
        ApprovalOpenParams.model_validate(_open_params(assignee=ASSIGNEE)),
        now=AT,
    )
    # the bundle's ledger line moves no record, so only the question's own row patches
    moved = [e for e in commit.envelopes if "entity_ref" in e.payload]
    patches = [p for e in moved for p in patches_for_event(e) if ATTENTION_ROUTE in p.routes]
    clean = RouteProjection.model_validate(
        _call(tree, runtime, f"projection.{ATTENTION_ROUTE}.read", {})
    )
    replayed = apply_patches(
        before,
        patches,
        cursor=int(clean.header.source_cursor),
        scope_id=clean.header.scope_id,
        generated_at=clean.header.generated_at,
    )
    assert replayed.digest == clean.digest
    assert replayed.rows == clean.rows
    clean_view, replayed_view = build_register_view(clean), build_register_view(replayed)
    assert attention_mine(replayed_view, principal=ASSIGNEE) == attention_mine(
        clean_view, principal=ASSIGNEE
    )
    announced = deliveries(replayed_view, principal=ASSIGNEE, delivered=())
    assert [item.key for item in announced] == ["ACT-0001"]
    seen = delivered_revisions(clean_view)
    assert deliveries(replayed_view, principal=ASSIGNEE, delivered=seen) == ()
