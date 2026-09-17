"""An accepted native mutation writes four things; a denied one writes none.

The contract of the seven-step path is countable. One accepted transition
leaves exactly one WAL record, exactly one rewrite of the generation
document, exactly one firehose row, and that row carries exactly one
``domain.<entity>.<verb>`` event name drawn from the closed vocabulary. A
denied edge leaves none of them: the denial is decided against the
re-read record, before the first byte is written.

The lock files a session takes are not counted as writes. They are the
mechanism that makes the count trustworthy and they live outside the
generation on purpose, so what is asserted here is the three artifacts and
the document bytes themselves.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.epoch2.domain_events import DOMAIN_EVENT_NAMES
from eawf.kernel.state.epoch2.transitions import ObservedFact
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods.domain_envelope import (
    DOMAIN_TRANSITION_METHOD,
    ENTITY_REF_WIDTH,
    UNNAMED_SUBJECT,
    DomainErrorCode,
)
from eawf.runtime.daemon.wal import WalStatus, list_records, read_record
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    legacy_task_row,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

ACTOR = "OP-0001"
KEY = "req-0001"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one planned Milestone."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _request(**overrides: Any) -> TransitionRequest:
    payload: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "to_status": "ACTIVE",
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
    }
    payload.update(overrides)
    return TransitionRequest.model_validate(payload)


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_accepted_transition_writes_one_wal_record(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)

    records = list_records(context.wal_dir)
    assert len(records) == 1
    assert records[0].name.endswith(f".{WalStatus.FSYNCED.value}.json")
    assert read_record(records[0]).idempotency_key == KEY


def test_accepted_transition_mutates_the_document_once(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    document = read_document(document_path(canary))
    row = document["milestone"]["MLS-0030"]
    assert (row["status"], row["revision"]) == ("ACTIVE", 2)
    assert document[CANONICAL_SEQUENCE_KEY] == committed.receipt.canonical_sequence
    assert list(document["milestone"]) == ["MLS-0030"]


def test_accepted_transition_appends_one_firehose_row(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    rows = _firehose_rows(canary)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["name"] == "domain.milestone.activated"
    assert payload["name"] in DOMAIN_EVENT_NAMES
    assert payload["entity_ref"] == MILESTONE_URN
    assert (payload["from_status"], payload["to_status"]) == ("PLANNED", "ACTIVE")
    assert (payload["revision_before"], payload["revision_after"]) == (1, 2)
    assert payload["actor_ref"] == ACTOR
    assert payload["canonical_sequence"] == committed.receipt.canonical_sequence
    assert rows[0]["id"] == committed.receipt.event_id


def test_accepted_transition_returns_a_publishable_envelope(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The envelope comes back unpublished so the caller publishes it unlocked."""
    committed = run_transaction(context=context, request=_request(), now=AT)

    assert isinstance(committed.envelope, Envelope)
    assert committed.receipt.event_name == "domain.milestone.activated"
    assert committed.receipt.revision_before == 1
    assert committed.receipt.revision_after == 2
    assert committed.receipt.canonical_sequence == 1


def test_denied_illegal_edge_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(to_status="COMPLETED"), now=AT)

    assert caught.value.code is TransactionRefusalCode.ILLEGAL_TRANSITION
    assert caught.value.revision == 1
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []


def test_denied_guard_writes_nothing(tmp_path: Path) -> None:
    """A merging Batch stays shut until the host merge is observed."""
    canary = provision(tmp_path / "batchrepo", code="BATCH")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "MERGING")}})
    context = root_context(canary, tmp_path / "runtime")
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(urn=BATCH_URN, to_status="MERGED_PENDING_RECONCILIATION"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.TRANSITION_GUARD_FAILED
    assert caught.value.guard == "host_merge_observed"
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []


def test_presented_observation_admits_the_guarded_edge(tmp_path: Path) -> None:
    canary = provision(tmp_path / "batchrepo", code="BATCH")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "MERGING")}})
    context = root_context(canary, tmp_path / "runtime")

    committed = run_transaction(
        context=context,
        request=_request(
            urn=BATCH_URN,
            to_status="MERGED_PENDING_RECONCILIATION",
            observations=(ObservedFact.HOST_MERGE_OBSERVED.value,),
        ),
        now=AT,
    )

    assert committed.receipt.event_name == "domain.batch.merge_observed"
    assert committed.receipt.event_name in DOMAIN_EVENT_NAMES
    assert len(_firehose_rows(canary)) == 1


def test_absent_record_is_refused_as_identity_not_found(tmp_path: Path) -> None:
    """A document holding no rows at all is the empty boundary of the re-read."""
    canary = provision(tmp_path / "bare", code="BARE")
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(), now=AT)

    assert caught.value.code is TransactionRefusalCode.IDENTITY_NOT_FOUND
    assert caught.value.revision is None
    assert list_records(context.wal_dir) == []


def test_stale_revision_is_refused_as_revision_conflict(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Off by one against the stored revision is a conflict, not an overwrite."""
    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(expected_revision=2), now=AT)

    assert caught.value.code is TransactionRefusalCode.REVISION_CONFLICT
    assert caught.value.revision == 1
    assert list_records(context.wal_dir) == []


def test_legacy_origin_record_is_refused_read_only(tmp_path: Path) -> None:
    canary = provision(tmp_path / "legacyrepo", code="LEGACY")
    seed(canary, {"task": {"EAWF-0042": legacy_task_row()}})
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(urn=TASK_URN, to_status="CLAIMED"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.LEGACY_IDENTITY_READ_ONLY
    assert list_records(context.wal_dir) == []


def test_missing_required_update_is_refused(tmp_path: Path) -> None:
    """A target status that makes a field a fact refuses a request without it."""
    canary = provision(tmp_path / "activerepo", code="ACTIVE")
    seed(canary, {"milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")}})
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(to_status="ACCEPTANCE_REVIEW"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert "acceptance_bundle_revision" in str(caught.value)
    assert list_records(context.wal_dir) == []


def test_unknown_status_token_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(to_status="SHIPPED"), now=AT)

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert list_records(context.wal_dir) == []


def test_entity_kind_without_a_machine_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    evidence = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(urn=evidence), now=AT)

    assert caught.value.code is TransactionRefusalCode.IDENTITY_KIND_MISMATCH
    assert list_records(context.wal_dir) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"urn": "not-a-urn"}, id="unparsable-urn"),
        pytest.param({"expected_revision": 0}, id="revision-below-one"),
        pytest.param({"idempotency_key": ""}, id="empty-idempotency-key"),
        pytest.param({"actor": "not a principal"}, id="free-text-actor"),
        pytest.param({"unknown": "field"}, id="unknown-field"),
    ],
)
def test_request_parameters_are_strict(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _request(**overrides)


def test_rpc_commits_and_publishes_after_the_commit(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    published: list[Envelope] = []
    ctx.bus.publish = published.append  # type: ignore[method-assign]

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["status"] == "ok"
    assert answer["errors"] == []
    assert answer["result"]["event_name"] == "domain.milestone.activated"
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert [envelope.payload["name"] for envelope in published] == ["domain.milestone.activated"]
    assert len(_firehose_rows(canary)) == 1


def test_rpc_returns_an_error_envelope_for_a_denied_edge(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    before = document_path(canary).read_bytes()

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "COMPLETED",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["status"] == "error"
    assert answer["result"] is None
    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert answer["errors"][0]["entity_ref"] == MILESTONE_URN
    assert answer["revision_before"] == answer["revision_after"] == 1
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []


def test_rpc_refuses_an_unparsable_request_without_leaking_values(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {"repo_root": str(canary.root), "urn": MILESTONE_URN, "to_status": "ACTIVE"},
        )
    )

    assert answer["status"] == "error"
    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "expected_revision" in row["message"]
    assert str(tmp_path) not in row["message"]


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        pytest.param(None, UNNAMED_SUBJECT, id="no-urn"),
        pytest.param("", UNNAMED_SUBJECT, id="empty-urn"),
        pytest.param(7, UNNAMED_SUBJECT, id="non-string-urn"),
        pytest.param("x" * (ENTITY_REF_WIDTH + 1), "x" * ENTITY_REF_WIDTH, id="oversized-urn"),
    ],
)
def test_rpc_bounds_the_subject_of_an_unparsable_request(
    canary: CanaryProvision, tmp_path: Path, named: Any, expected: str
) -> None:
    """An oversized or missing subject must not make the refusal unbuildable."""
    ctx = method_context(tmp_path / "runtime")
    params: dict[str, Any] = {"repo_root": str(canary.root)}
    if named is not None:
        params["urn"] = named

    answer = asyncio.run(methods.dispatch(DOMAIN_TRANSITION_METHOD, ctx, params))

    assert answer["status"] == "error"
    assert answer["errors"][0]["entity_ref"] == expected


def test_the_transition_method_is_registered_on_the_server() -> None:
    """Importing the server registers the native transition verb."""
    import eawf.runtime.daemon.server  # noqa: F401

    assert DOMAIN_TRANSITION_METHOD in methods.registered_methods()
