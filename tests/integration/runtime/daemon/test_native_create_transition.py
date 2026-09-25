"""A new epoch-2 record is admitted through the transaction, or not at all.

Each of the five driven machines -- Track, Milestone, Batch, Task, Run --
has a create verb. An accepted create is counted the way an accepted
transition is: one WAL record, one document rewrite carrying the record at
revision one in its machine's first status, one firehose row, and a receipt
filed for the idempotency key. A refused create writes none of them.

The compare-and-swap token of a create is the tree's committed
``canonical_sequence``, because the record it would compare does not exist
yet. A stale cursor, a taken key, a missing parent, an invalid create
document and a leak-shaped free text are each refused with the document,
the WAL, the firehose and the ledgers exactly as they were. A retry under
the same key is a no-op answered with the original receipt.

The canary rehearsal once had to write its Track straight into the
generation document before a plan could be submitted. The last test walks
that precondition through the create verb instead.
"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import patches_for_event
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_create import (
    CREATE_EVENT_NAMES,
    CreateRequest,
    run_create,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods.domain_create import DOMAIN_CREATE_METHODS
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.methods.planning import PLAN_SUBMIT_METHOD
from eawf.runtime.daemon.wal import WalStatus, list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"

TRACK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"

#: The fields a stored row carries that no create document may name: the
#: daemon fills the identity head and the status, and every transition-owned
#: fact starts absent.
DAEMON_OWNED: Final = frozenset(
    {
        "uid",
        "urn",
        "origin",
        "revision",
        "created_at",
        "updated_at",
        "status",
        "milestone_refs",
        "campaign_refs",
        "contract_revision",
    }
)

#: Each machine's entity token, URN and first status, in nesting order.
CHAIN: Final[tuple[tuple[str, str, str], ...]] = (
    ("track", TRACK_URN, "ACTIVE"),
    ("milestone", MILESTONE_URN, "PLANNED"),
    ("batch", BATCH_URN, "PLANNED"),
    ("task", TASK_URN, "DRAFT"),
    ("run", RUN_URN, "QUEUED"),
)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary whose document holds no record at all."""
    return provision(tmp_path / "repo", code="CREATE")


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def create_spec(entity: str) -> dict[str, Any]:
    """Return the create document of one seeded record.

    The seed of the machine's first status is stripped of what the daemon
    owns, so the document describes exactly the record the reducers accept.
    """
    status = next(first for token, _urn, first in CHAIN if token == entity)
    return {
        key: value for key, value in seed_row(entity, status).items() if key not in DAEMON_OWNED
    }


def request(entity: str = "track", **overrides: Any) -> CreateRequest:
    """Return a create request for one seeded record, with *overrides* applied."""
    urn = next(urn for token, urn, _first in CHAIN if token == entity)
    payload: dict[str, Any] = {
        "urn": urn,
        "expected_revision": 0,
        "idempotency_key": f"req-create-{entity}",
        "actor": ACTOR,
        "spec": create_spec(entity),
    }
    payload.update(overrides)
    return CreateRequest.model_validate(payload)


def firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def admit_chain(context: Epoch2RootContext, *, upto: int = len(CHAIN)) -> None:
    """Create the first *upto* records of the chain, each on the cursor the last left."""
    for cursor, (entity, _urn, _first) in enumerate(CHAIN[:upto]):
        run_create(context=context, request=request(entity, expected_revision=cursor), now=AT)


def snapshot(canary: CanaryProvision, context: Epoch2RootContext) -> tuple[bytes, int, int]:
    """Return the document bytes, the WAL record count and the firehose row count."""
    return (
        document_path(canary).read_bytes(),
        len(list_records(context.wal_dir)),
        len(firehose_rows(canary)),
    )


def refusal(context: Epoch2RootContext, create: CreateRequest) -> TransactionRefusedError:
    with pytest.raises(TransactionRefusedError) as caught:
        run_create(context=context, request=create, now=AT)
    return caught.value


# ---- accepted creates -------------------------------------------------------


@pytest.mark.parametrize(("position", "entity"), [(i, row[0]) for i, row in enumerate(CHAIN)])
def test_each_create_verb_commits_one_intent_one_row_and_one_event(
    context: Epoch2RootContext, canary: CanaryProvision, position: int, entity: str
) -> None:
    admit_chain(context, upto=position)

    committed = run_create(
        context=context, request=request(entity, expected_revision=position), now=AT
    )

    urn, first = CHAIN[position][1], CHAIN[position][2]
    receipt = committed.receipt
    assert (receipt.revision_before, receipt.revision_after) == (None, 1)
    assert receipt.canonical_sequence == position + 1
    assert receipt.event_name == f"admission.{entity}.created"
    assert receipt.event_name in CREATE_EVENT_NAMES
    document = read_document(document_path(canary))
    row = document[entity][urn.rsplit("/", 1)[1]]
    assert (row["status"], row["revision"], row["urn"]) == (first, 1, urn)
    assert row["origin"]["kind"] == "native"
    assert document[CANONICAL_SEQUENCE_KEY] == position + 1
    records = list_records(context.wal_dir)
    assert len(records) == position + 1
    assert all(path.name.endswith(f".{WalStatus.FSYNCED.value}.json") for path in records)
    rows = firehose_rows(canary)
    assert len(rows) == position + 1
    payload = rows[-1]["payload"]
    assert payload["name"] == receipt.event_name
    assert (payload["entity_ref"], payload["to_status"]) == (urn, first)
    assert (payload["revision_before"], payload["revision_after"]) == (None, 1)
    assert payload["actor_ref"] == ACTOR
    assert rows[-1]["id"] == receipt.event_id


def test_a_create_event_patches_the_projection_from_the_event_alone(
    context: Epoch2RootContext,
) -> None:
    committed = run_create(context=context, request=request(), now=AT)

    assert isinstance(committed.envelope, Envelope)
    patches = patches_for_event(committed.envelope)
    assert patches
    assert {entry.key for patch in patches for entry in patch.entries} == {"TRK-RUNTIME"}
    assert {entry.status for patch in patches for entry in patch.entries} == {"ACTIVE"}


def test_a_created_record_moves_through_its_lifecycle_verbs(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A create lands a record the transaction can move on the next request."""
    admit_chain(context, upto=2)

    committed = run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": "req-activate",
                "actor": ACTOR,
            }
        ),
        now=AT,
    )

    assert committed.receipt.event_name == "domain.milestone.activated"
    assert committed.receipt.canonical_sequence == 3


# ---- replay -----------------------------------------------------------------


def test_a_replayed_create_writes_nothing_and_returns_the_original_receipt(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    first = run_create(context=context, request=request(), now=AT)
    before = snapshot(canary, context)

    again = run_create(context=context, request=request(), now=AT)

    assert again.replayed is True
    assert again.envelope is None
    assert again.receipt == first.receipt
    assert snapshot(canary, context) == before


def test_a_replay_after_the_tree_moved_still_answers_from_its_receipt(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The replay is decided before the cursor, so a later commit cannot stale it."""
    first = run_create(context=context, request=request(), now=AT)
    run_create(context=context, request=request("milestone", expected_revision=1), now=AT)
    before = snapshot(canary, context)

    again = run_create(context=context, request=request(), now=AT)

    assert again.receipt == first.receipt
    assert snapshot(canary, context) == before


def test_one_key_naming_two_creates_is_an_idempotency_conflict(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_create(context=context, request=request(), now=AT)
    before = snapshot(canary, context)
    changed = create_spec("track")
    changed["title"] = "Harden the runtime against a second outage"

    caught = refusal(context, request(spec=changed))

    assert caught.code is TransactionRefusalCode.IDEMPOTENCY_CONFLICT
    assert snapshot(canary, context) == before


# ---- stale cursor -----------------------------------------------------------


@pytest.mark.parametrize("expected", [0, 2], ids=["behind-by-one", "ahead-by-one"])
def test_a_stale_cursor_is_refused_with_zero_mutation(
    context: Epoch2RootContext, canary: CanaryProvision, expected: int
) -> None:
    run_create(context=context, request=request(), now=AT)
    before = snapshot(canary, context)

    caught = refusal(context, request("milestone", expected_revision=expected))

    assert caught.code is TransactionRefusalCode.REVISION_CONFLICT
    assert caught.guard == "tree_cursor_current"
    assert "canonical sequence 1" in caught.detail
    assert snapshot(canary, context) == before


def test_a_create_on_an_untouched_tree_expects_cursor_zero(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = snapshot(canary, context)

    caught = refusal(context, request(expected_revision=1))

    assert caught.code is TransactionRefusalCode.REVISION_CONFLICT
    assert snapshot(canary, context) == before


# ---- taken key and missing parent ---------------------------------------------


def test_a_key_the_document_holds_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_create(context=context, request=request(), now=AT)
    before = snapshot(canary, context)

    caught = refusal(context, request(expected_revision=1, idempotency_key="req-again"))

    assert caught.code is TransactionRefusalCode.REVISION_CONFLICT
    assert caught.guard == "record_key_free"
    assert caught.revision == 1
    assert snapshot(canary, context) == before


def test_a_key_compacted_into_its_ledger_is_never_reused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A cancelled Milestone left the document; its key is still taken."""
    admit_chain(context, upto=2)
    run_transaction(
        context=context,
        request=TransitionRequest.model_validate(
            {
                "urn": MILESTONE_URN,
                "to_status": "CANCELLED",
                "expected_revision": 1,
                "idempotency_key": "req-cancel",
                "actor": ACTOR,
                "reason_code": "scope-withdrawn",
            }
        ),
        now=AT,
    )
    ledger = ledger_path(document_path(canary), Epoch2Collection.MILESTONE)
    assert [line.record_key for line in read_ledger_records(ledger)] == ["MLS-0030"]
    assert "MLS-0030" not in read_document(document_path(canary)).get("milestone", {})
    before = snapshot(canary, context)

    caught = refusal(
        context, request("milestone", expected_revision=3, idempotency_key="req-reuse")
    )

    assert caught.code is TransactionRefusalCode.REVISION_CONFLICT
    assert caught.guard == "record_key_free"
    assert snapshot(canary, context) == before


@pytest.mark.parametrize(
    ("entity", "admitted"),
    [("milestone", 0), ("batch", 1), ("run", 3)],
    ids=["milestone-without-track", "batch-without-milestone", "run-without-task"],
)
def test_a_create_under_an_absent_parent_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision, entity: str, admitted: int
) -> None:
    admit_chain(context, upto=admitted)
    before = snapshot(canary, context)

    caught = refusal(context, request(entity, expected_revision=admitted))

    assert caught.code is TransactionRefusalCode.IDENTITY_NOT_FOUND
    assert caught.guard == "parent_record_live"
    assert snapshot(canary, context) == before


# ---- invalid documents --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "RETIRED"), ("revision", 7), ("milestone_refs", [MILESTONE_URN])],
    ids=["status", "revision", "derived-index"],
)
def test_a_create_document_naming_a_daemon_owned_field_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision, field: str, value: Any
) -> None:
    before = snapshot(canary, context)
    spec = {**create_spec("track"), field: value}

    caught = refusal(context, request(spec=spec))

    assert caught.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert field in caught.detail
    assert snapshot(canary, context) == before


def test_an_empty_create_document_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = snapshot(canary, context)

    caught = refusal(context, request(spec={}))

    assert caught.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert snapshot(canary, context) == before


def test_a_create_document_keyed_off_its_urn_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = snapshot(canary, context)

    caught = refusal(context, request(spec={**create_spec("track"), "key": "TRK-OTHER"}))

    assert caught.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert "TRK-OTHER" in caught.detail
    assert snapshot(canary, context) == before


def test_a_create_document_carrying_a_leak_shape_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = snapshot(canary, context)
    token = "ghp_" + "a" * 36

    caught = refusal(context, request(spec={**create_spec("track"), "charter": token}))

    assert caught.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert token not in caught.detail
    assert snapshot(canary, context) == before


def test_a_kind_without_a_machine_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = snapshot(canary, context)
    evidence = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"

    caught = refusal(context, request(urn=evidence))

    assert caught.code is TransactionRefusalCode.IDENTITY_KIND_MISMATCH
    assert snapshot(canary, context) == before


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"expected_revision": -1}, id="negative-cursor"),
        pytest.param({"expected_revision": True}, id="boolean-cursor"),
        pytest.param({"idempotency_key": ""}, id="empty-idempotency-key"),
        pytest.param({"idempotency_key": "k" * 129}, id="oversized-idempotency-key"),
        pytest.param({"actor": "not a principal"}, id="free-text-actor"),
        pytest.param({"unknown": "field"}, id="unknown-field"),
    ],
)
def test_create_request_parameters_are_strict(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        request(**overrides)


def test_a_create_request_without_a_create_document_is_refused() -> None:
    payload = request().model_dump(mode="json")
    del payload["spec"]

    with pytest.raises(ValueError, match="spec"):
        CreateRequest.model_validate(payload)


# ---- the RPC surface ------------------------------------------------------------


def dispatch(method: str, canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    published: list[Envelope] = []
    ctx.bus.publish = published.append  # type: ignore[method-assign]
    answer = asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))
    answer["published"] = [envelope.payload["name"] for envelope in published]
    return answer


def test_every_create_verb_is_registered_on_the_server() -> None:
    import eawf.runtime.daemon.server  # noqa: F401

    methods.ensure_all_methods_registered()
    assert set(DOMAIN_CREATE_METHODS.values()) <= set(methods.registered_methods())
    assert sorted(DOMAIN_CREATE_METHODS.values()) == sorted(
        f"domain.{token}.create" for token, _urn, _first in CHAIN
    )


def test_the_rpc_commits_and_publishes_after_the_commit(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = dispatch(
        "domain.track.create",
        canary,
        tmp_path,
        urn=TRACK_URN,
        expected_revision=0,
        idempotency_key="req-rpc",
        actor=ACTOR,
        spec=create_spec("track"),
    )

    assert answer["status"] == "ok"
    assert (answer["revision_before"], answer["revision_after"]) == (None, 1)
    assert answer["result"]["event_name"] == "admission.track.created"
    assert answer["published"] == ["admission.track.created"]
    assert len(firehose_rows(canary)) == 1


def test_the_rpc_replays_without_republishing(canary: CanaryProvision, tmp_path: Path) -> None:
    params = {
        "urn": TRACK_URN,
        "expected_revision": 0,
        "idempotency_key": "req-rpc",
        "actor": ACTOR,
        "spec": create_spec("track"),
    }
    first = dispatch("domain.track.create", canary, tmp_path, **params)

    again = dispatch("domain.track.create", canary, tmp_path, **params)

    assert again["result"] == first["result"]
    assert again["published"] == []
    assert len(firehose_rows(canary)) == 1


def test_the_rpc_refuses_a_urn_of_another_kind(canary: CanaryProvision, tmp_path: Path) -> None:
    before = document_path(canary).read_bytes()

    answer = dispatch(
        "domain.milestone.create",
        canary,
        tmp_path,
        urn=TRACK_URN,
        expected_revision=0,
        idempotency_key="req-rpc",
        actor=ACTOR,
        spec=create_spec("track"),
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_KIND_MISMATCH.value
    assert document_path(canary).read_bytes() == before


def test_the_rpc_answers_a_stale_cursor_with_an_error_envelope(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    path = document_path(canary)
    write_document(path, {**read_document(path), CANONICAL_SEQUENCE_KEY: 4})
    before = path.read_bytes()

    answer = dispatch(
        "domain.track.create",
        canary,
        tmp_path,
        urn=TRACK_URN,
        expected_revision=3,
        idempotency_key="req-rpc",
        actor=ACTOR,
        spec=create_spec("track"),
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert answer["errors"][0]["guard"] == "tree_cursor_current"
    assert path.read_bytes() == before


def test_the_rpc_refuses_an_unparsable_request_without_leaking_values(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = dispatch(
        "domain.track.create", canary, tmp_path, urn=TRACK_URN, spec=create_spec("track")
    )

    assert answer["status"] == "error"
    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "expected_revision" in row["message"]
    assert str(tmp_path) not in row["message"]


# ---- the rehearsal precondition -------------------------------------------------


def test_the_rehearsal_admits_its_track_through_the_create_verb(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """A plan submits over a Track the create verb admitted, not one written in.

    The repository row the plan also binds is still seeded: no machine
    governs a repository, so no create verb admits one.
    """
    from tests.integration.workflow.planning.test_plan_revision_apply import (
        BODY,
        HEAD,
        OPERATOR,
        REVISION_KEY,
    )

    created = dispatch(
        "domain.track.create",
        canary,
        tmp_path,
        urn=TRACK_URN,
        expected_revision=0,
        idempotency_key="req-track",
        actor=ACTOR,
        spec=create_spec("track"),
    )
    seed(canary, {"repository": {"REP-EAWF": {"key": "REP-EAWF", "head_sha": HEAD}}})

    submitted = dispatch(
        PLAN_SUBMIT_METHOD,
        canary,
        tmp_path,
        proposal={"key": REVISION_KEY, "author": OPERATOR, "body": copy.deepcopy(BODY)},
        actor=ACTOR,
        idempotency_key="req-submit",
    )

    assert created["status"] == "ok"
    assert submitted["status"] == "ok", submitted["errors"]
    rows = firehose_rows(canary)
    assert [row["payload"]["canonical_sequence"] for row in rows] == [1, 2]
    assert rows[0]["payload"]["name"] == "admission.track.created"
