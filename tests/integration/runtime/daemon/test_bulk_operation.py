"""A bulk control is one operation over many Runs, with one result per Run.

Every call runs through the live daemon verbs against a provisioned epoch-2 canary:
``runtime.bulk.preview`` names the consequence, ``runtime.bulk.control`` opens the
operation by asking each Run through the single-control verbs, and
``runtime.bulk.reconcile`` reads every unsettled item back. The Run's driver is played
by the real ``runtime.run.control.effect`` verb, so an item's outcome is only ever what
the control ledger records.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.bulk import (
    BULK_CONTROL_METHOD,
    BULK_PREVIEW_METHOD,
    BULK_RECONCILE_METHOD,
)
from eawf.runtime.daemon.methods.run import (
    RUN_CONTROL_ACKNOWLEDGE_METHOD,
    RUN_CONTROL_EFFECT_METHOD,
    RUN_CONTROL_REQUEST_METHOD,
)
from eawf.runtime.daemon.native_dispatch import control_facts_of
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
OTHER: Final = "OP-0002"
_RUNS: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run"
FIRST, SECOND, THIRD = (f"{_RUNS}/RUN-0000001{n}" for n in (1, 2, 3))
MISSING: Final = f"{_RUNS}/RUN-00000019"
#: The seeded Run the seeded Task holds as its active Run.
TASK_RUN: Final = f"{_RUNS}/RUN-00000010"
TASK_KEY: Final = "EAWF-0042"
EFFECT_REF: Final = "EFF-0000000e"
RECEIPT_REF: Final = "REC-0000000d"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding four running Runs and the running Task one of them works for.

    The third Run stands at revision 2, so an operation confirmed at revision 1 finds
    it moved.
    """
    provisioned = provision(tmp_path / "repo", code="BULK")
    running = seed_row("run", "RUNNING")
    runs = {
        urn.rsplit("/", 1)[1]: rekeyed(running, key=urn.rsplit("/", 1)[1])
        for urn in (FIRST, SECOND, THIRD)
    }
    runs["RUN-00000013"]["revision"] = 2
    runs["RUN-00000010"] = running
    seed(provisioned, {"run": runs, "task": {TASK_KEY: seed_row("task", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(ctx: MethodContext, canary: CanaryProvision, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb against the canary the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def preview(ctx: MethodContext, canary: CanaryProvision, items: list[str]) -> dict[str, Any]:
    """Preview a bulk cancel over *items*."""
    return call(ctx, canary, BULK_PREVIEW_METHOD, verb="cancel", item_refs=items)


def request(
    ctx: MethodContext, canary: CanaryProvision, items: list[str], *, key: str = "bulk-1"
) -> dict[str, Any]:
    """Return the opening request of a bulk cancel over *items*, anchored at revision 1."""
    shown = preview(ctx, canary, items)
    return {
        "verb": "cancel",
        "item_refs": items,
        "expected_revisions": dict.fromkeys(items, 1),
        "actor": ACTOR,
        "idempotency_key": key,
        "confirmation_digest": shown["confirmation_digest"],
    }


def open_bulk(ctx: MethodContext, canary: CanaryProvision, body: dict[str, Any]) -> dict[str, Any]:
    """Open the operation *body* describes."""
    return call(ctx, canary, BULK_CONTROL_METHOD, **body)


def effect(
    ctx: MethodContext, canary: CanaryProvision, operation: dict[str, Any], urn: str, how: str
) -> None:
    """Play the Run's driver: record what was observed of one item's control."""
    proof = {"effect_ref": EFFECT_REF} if how == "confirmed" else {"receipt_ref": RECEIPT_REF}
    call(
        ctx,
        canary,
        RUN_CONTROL_EFFECT_METHOD,
        urn=urn,
        control_request_ref=operation["item_results"][urn]["control_request_ref"],
        actor=ACTOR,
        disposition=how,
        idempotency_key=f"effect-{urn[-2:]}",
        **proof,
    )


def states(operation: dict[str, Any]) -> dict[str, str]:
    """Return each item's state, keyed by URN."""
    return {urn: row["state"] for urn, row in operation["item_results"].items()}


def document(canary: CanaryProvision) -> dict[str, Any]:
    """Return the canary's current document."""
    return read_document(document_path(canary))


def control_lines(canary: CanaryProvision, tmp_path: Path, urn: str) -> list[tuple[str, str]]:
    """Return one Run's control facts as ``(request, disposition)``, in ledger order."""
    context = root_context(canary, tmp_path / "reader")
    run = parse_qualified_urn(urn)
    with context.session([run]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return [
        (fact.control_request_ref, fact.disposition.value)
        for fact in control_facts_of(records, run)
    ]


def take_lease(ctx: MethodContext, canary: CanaryProvision, urn: str) -> None:
    """Have another principal take the Run's control lease first."""
    asked = {"urn": urn, "control_request_ref": "CTL-0000000b", "actor": OTHER}
    call(ctx, canary, RUN_CONTROL_REQUEST_METHOD, control="interrupt", **asked)
    call(ctx, canary, RUN_CONTROL_ACKNOWLEDGE_METHOD, **asked)


# ---- DEL-021: a bulk verb is authorized per item ----------------------------


def test_del_021_a_denied_item_is_rejected_alone_and_the_operation_still_answers(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    take_lease(ctx, canary, SECOND)
    items = [FIRST, SECOND, THIRD, MISSING]
    body = request(ctx, canary, [FIRST, SECOND, THIRD])
    body["item_refs"] = items
    body["expected_revisions"] = dict.fromkeys(items, 1)
    body["confirmation_digest"] = preview(ctx, canary, items)["confirmation_digest"]

    operation = open_bulk(ctx, canary, body)

    results = operation["item_results"]
    assert states(operation) == {
        FIRST: "accepted",
        SECOND: "rejected",
        THIRD: "rejected",
        MISSING: "rejected",
    }
    assert results[SECOND]["code"] == "superseded"
    assert results[THIRD]["code"] == "revision_conflict"
    assert results[MISSING]["code"] == "identity_not_found"
    # a Run rejected on its anchor was never asked for at all
    assert results[THIRD]["control_request_ref"] is None


def test_del_021_a_denied_item_grants_the_others_nothing_they_lacked(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    take_lease(ctx, canary, SECOND)

    operation = open_bulk(ctx, canary, request(ctx, canary, [FIRST, SECOND]))

    ours = operation["item_results"][SECOND]["control_request_ref"]
    # the other principal keeps the lease; the refused item holds nothing on that Run
    assert control_lines(canary, tmp_path, SECOND) == [
        ("CTL-0000000b", "requesting"),
        ("CTL-0000000b", "accepted"),
        (ours, "requesting"),
        (ours, "superseded"),
    ]
    first = operation["item_results"][FIRST]["control_request_ref"]
    assert control_lines(canary, tmp_path, FIRST) == [
        (first, "requesting"),
        (first, "accepted"),
    ]


def test_del_021_items_are_listed_canonically_whatever_the_selection_order(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    operation = open_bulk(ctx, canary, request(ctx, canary, [SECOND, FIRST]))

    assert operation["item_refs"] == [FIRST, SECOND]


def test_del_021_an_item_named_twice_refuses_the_request(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    body = request(ctx, canary, [FIRST])
    body["item_refs"] = [FIRST, FIRST]

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        open_bulk(ctx, canary, body)


def test_del_021_an_empty_item_set_is_refused(ctx: MethodContext, canary: CanaryProvision) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        preview(ctx, canary, [])


def test_del_021_a_task_is_not_a_bulk_control_item(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    task = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/{TASK_KEY}"

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        preview(ctx, canary, [task])


# ---- DEL-022: partial outcome is first-class ---------------------------------


def test_del_022_a_mixed_outcome_is_a_normal_answer_counted_by_state(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    items = [FIRST, SECOND, THIRD]
    opened = open_bulk(ctx, canary, request(ctx, canary, items))
    effect(ctx, canary, opened, FIRST, "confirmed")
    effect(ctx, canary, opened, SECOND, "unknown")

    now = call(ctx, canary, BULK_RECONCILE_METHOD, **request(ctx, canary, items))

    assert now["aggregate"] == {
        "requested": 0,
        "accepted": 0,
        "rejected": 1,
        "confirmed": 1,
        "unknown": 1,
        "invalidated": 0,
    }
    assert "success" not in now
    assert opened["aggregate"]["accepted"] == 2


# ---- DEL-023: an unknown item stays unknown until reconciled ------------------


def test_del_023_an_unobserved_effect_is_never_reported_terminal(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    body = request(ctx, canary, [FIRST, SECOND])
    opened = open_bulk(ctx, canary, body)
    effect(ctx, canary, opened, SECOND, "unknown")

    first = call(ctx, canary, BULK_RECONCILE_METHOD, **body)
    again = call(ctx, canary, BULK_RECONCILE_METHOD, **body)

    assert states(opened) == {FIRST: "accepted", SECOND: "accepted"}
    assert states(first) == states(again) == {FIRST: "accepted", SECOND: "unknown"}
    assert document(canary)["run"]["RUN-00000012"]["status"] == "RUNNING"


def test_del_023_only_an_observed_effect_confirms_an_item(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    body = request(ctx, canary, [FIRST])
    opened = open_bulk(ctx, canary, body)
    effect(ctx, canary, opened, FIRST, "confirmed")

    reconciled = call(ctx, canary, BULK_RECONCILE_METHOD, **body)

    assert states(reconciled) == {FIRST: "confirmed"}


def test_del_023_reconciling_an_operation_never_opened_is_refused(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(ctx, canary, BULK_RECONCILE_METHOD, **request(ctx, canary, [FIRST], key="never"))


def test_del_023_an_ask_that_fails_in_transit_is_unknown_not_rejected(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = request(ctx, canary, [FIRST, SECOND])
    real = methods.dispatch

    async def flaky(name: str, context: MethodContext, params: dict[str, Any]) -> Any:
        if name == RUN_CONTROL_ACKNOWLEDGE_METHOD and params["urn"] == SECOND:
            raise OSError("the lock file went away")
        return await real(name, context, params)

    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", flaky)
    opened = open_bulk(ctx, canary, body)
    monkeypatch.setattr("eawf.runtime.daemon.methods.bulk.dispatch", real)
    reconciled = call(ctx, canary, BULK_RECONCILE_METHOD, **body)

    assert states(opened) == {FIRST: "accepted", SECOND: "unknown"}
    assert states(reconciled) == {FIRST: "accepted", SECOND: "accepted"}


# ---- DEL-024: confirmation before the operation opens -------------------------


def test_del_024_the_preview_names_count_effects_non_effects_and_invalidation_rule(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    before = document(canary)

    shown = preview(ctx, canary, [SECOND, FIRST, MISSING])

    confirmation = shown["confirmation"]
    assert confirmation["target_count"] == 3
    assert confirmation["effects"]
    assert any("does not release the Runs' Tasks" in line for line in confirmation["non_effects"])
    assert "rejected on its own" in confirmation["invalidation_rule"]
    assert shown["expected_revisions"] == {FIRST: 1, SECOND: 1}
    assert shown["unresolved"] == [MISSING]
    assert document(canary) == before


def test_del_024_an_operation_opened_under_another_confirmation_is_refused(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    body = request(ctx, canary, [FIRST, SECOND])
    body["confirmation_digest"] = preview(ctx, canary, [FIRST])["confirmation_digest"]

    with pytest.raises(DaemonValidationError, match="confirmation_mismatch"):
        open_bulk(ctx, canary, body)
    assert control_lines(canary, tmp_path, FIRST) == []


def test_del_024_cancelling_runs_does_not_release_their_tasks(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    task_before = document(canary)["task"][TASK_KEY]

    opened = open_bulk(ctx, canary, request(ctx, canary, [TASK_RUN]))
    effect(ctx, canary, opened, TASK_RUN, "confirmed")

    assert opened["item_results"][TASK_RUN]["state"] == "accepted"
    assert TASK_RUN.rsplit("/", 1)[1] not in document(canary)["run"]
    assert document(canary)["task"][TASK_KEY] == task_before


# ---- DEL-025: replay under the same idempotency key ---------------------------


def test_del_025_replay_returns_the_original_per_item_results(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    body = request(ctx, canary, [FIRST, SECOND])
    opened = open_bulk(ctx, canary, body)
    effect(ctx, canary, opened, FIRST, "confirmed")

    replayed = open_bulk(ctx, canary, {**body, "item_refs": [SECOND, FIRST]})

    assert replayed == opened


def test_del_025_the_same_key_with_another_item_set_is_an_idempotency_conflict(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    open_bulk(ctx, canary, request(ctx, canary, [FIRST, SECOND]))

    with pytest.raises(DaemonValidationError, match="idempotency_conflict"):
        open_bulk(ctx, canary, request(ctx, canary, [FIRST]))


def test_del_025_a_second_key_opens_a_second_operation(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    first = open_bulk(ctx, canary, request(ctx, canary, [FIRST], key="bulk-1"))
    second = open_bulk(ctx, canary, request(ctx, canary, [FIRST], key="bulk-2"))

    ref = first["item_results"][FIRST]["control_request_ref"]
    assert second["item_results"][FIRST]["control_request_ref"] != ref
    assert second["item_results"][FIRST]["code"] == "superseded"
