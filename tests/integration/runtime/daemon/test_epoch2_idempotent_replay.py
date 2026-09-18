"""A retried native mutation happens once; a reused key naming two does not.

The client cannot tell a lost reply from a lost request, so it retries,
and the retry is the same request by definition: same key, same
parameters. The daemon answers it with the receipt the original commit
produced and writes nothing at all -- no second WAL record, no second
firehose row, no second revision on the record.

The same key carrying different parameters is the opposite case. It is
not a retry; it is two requests wearing one name, and answering either
one would be a guess. The transaction refuses it as
``idempotency_conflict`` before it evaluates the transition, so the
refusal costs the tree nothing either.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_recovery import (
    RECEIPT_LOCATOR,
    canonical_params_digest,
    idempotency_receipt_path,
    read_idempotency_receipt,
    record_idempotency_receipt,
)
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
    DomainErrorCode,
)
from eawf.runtime.daemon.wal import list_records
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

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


def _rpc(canary: CanaryProvision, ctx: methods.MethodContext, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": MILESTONE_URN,
        "to_status": "ACTIVE",
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
    }
    params.update(overrides)
    return asyncio.run(methods.dispatch(DOMAIN_TRANSITION_METHOD, ctx, params))


# ---------------------------------------------------------------------------
# The receipt store on its own
# ---------------------------------------------------------------------------


def test_read_idempotency_receipt_returns_none_before_anything_committed(
    context: Epoch2RootContext,
) -> None:
    """An empty store is a miss, not an error: this key has committed nothing."""
    assert read_idempotency_receipt(context, namespaced_key="root-x:req-0001") is None


def test_receipts_live_in_the_root_local_store(context: Epoch2RootContext) -> None:
    """The receipt path is declared under the local store, so no clone carries it."""
    path = idempotency_receipt_path(context, namespaced_key="root-x:req-0001")

    relative = path.relative_to(context.identity.tree_root).as_posix()
    assert relative.startswith(f"{RECEIPT_LOCATOR}/")
    assert path.suffix == ".json"


def test_read_idempotency_receipt_refuses_bytes_that_are_not_json(
    context: Epoch2RootContext,
) -> None:
    key = context.idempotency_key(KEY)
    path = idempotency_receipt_path(context, namespaced_key=key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")

    with pytest.raises(ValueError, match="not valid JSON"):
        read_idempotency_receipt(context, namespaced_key=key)


def test_read_idempotency_receipt_refuses_a_record_that_does_not_validate(
    context: Epoch2RootContext,
) -> None:
    key = context.idempotency_key(KEY)
    path = idempotency_receipt_path(context, namespaced_key=key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps({"schema_version": "1"}))

    with pytest.raises(ValueError):
        read_idempotency_receipt(context, namespaced_key=key)


def test_read_idempotency_receipt_refuses_a_receipt_filed_under_another_key(
    context: Epoch2RootContext,
) -> None:
    """A file name collision must not be answered as this key's own receipt."""
    key = context.idempotency_key(KEY)
    record_idempotency_receipt(
        context,
        namespaced_key=key,
        params_digest="0" * 64,
        receipt={"event_name": "domain.milestone.activated"},
        recorded_at=AT,
    )
    stored = idempotency_receipt_path(context, namespaced_key=key)
    payload = orjson.loads(stored.read_bytes())
    payload["idempotency_key"] = f"{key}-other"
    stored.write_bytes(orjson.dumps(payload))

    with pytest.raises(ValueError, match="another key"):
        read_idempotency_receipt(context, namespaced_key=key)


def test_record_idempotency_receipt_rejects_a_digest_that_is_not_one(
    context: Epoch2RootContext,
) -> None:
    with pytest.raises(ValueError):
        record_idempotency_receipt(
            context,
            namespaced_key=context.idempotency_key(KEY),
            params_digest="not-a-digest",
            receipt={},
            recorded_at=AT,
        )


def test_canonical_params_digest_ignores_key_order() -> None:
    """Two spellings of one parameter set are one request, not two."""
    assert canonical_params_digest({"a": 1, "b": [2, 3]}) == canonical_params_digest(
        {"b": [2, 3], "a": 1}
    )


def test_canonical_params_digest_separates_one_changed_value() -> None:
    assert canonical_params_digest({"a": 1}) != canonical_params_digest({"a": 2})


def test_canonical_params_digest_refuses_a_value_json_cannot_hold() -> None:
    with pytest.raises(TypeError):
        canonical_params_digest({"at": object()})


# ---------------------------------------------------------------------------
# RUN-012: replay returns the original receipt and writes nothing
# ---------------------------------------------------------------------------


def test_replay_returns_the_original_receipt(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    first = run_transaction(context=context, request=_request(), now=AT)

    second = run_transaction(context=context, request=_request(), now=AT)

    assert second.receipt == first.receipt
    assert second.replayed is True
    assert second.envelope is None


def test_replay_writes_nothing_a_second_time(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """One WAL record, one firehose row, one revision -- however many retries."""
    run_transaction(context=context, request=_request(), now=AT)
    before = document_path(canary).read_bytes()

    run_transaction(context=context, request=_request(), now=AT)
    run_transaction(context=context, request=_request(), now=AT)

    document = read_document(document_path(canary))
    assert len(list_records(context.wal_dir)) == 1
    assert len(_firehose_rows(canary)) == 1
    assert document["milestone"]["MLS-0030"]["revision"] == 2
    assert document[CANONICAL_SEQUENCE_KEY] == 1
    assert document_path(canary).read_bytes() == before


def test_replay_precedes_the_revision_check(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A retry still names the revision it read, which the commit has moved past.

    Checking the revision first would answer every honest retry with a
    conflict, which is the one answer a retry must never get.
    """
    run_transaction(context=context, request=_request(), now=AT)

    replayed = run_transaction(context=context, request=_request(expected_revision=1), now=AT)

    assert replayed.replayed is True
    assert (replayed.receipt.revision_before, replayed.receipt.revision_after) == (1, 2)


def test_changed_params_under_a_reused_key_are_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(to_status="ACCEPTANCE_REVIEW", expected_revision=2),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.IDEMPOTENCY_CONFLICT
    assert caught.value.revision == 2


def test_a_reused_key_is_refused_before_the_transition_is_evaluated(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The conflict wins over the illegal edge the changed parameters name."""
    run_transaction(context=context, request=_request(), now=AT)

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(to_status="COMPLETED"), now=AT)

    assert caught.value.code is TransactionRefusalCode.IDEMPOTENCY_CONFLICT


def test_a_refused_reuse_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError):
        run_transaction(context=context, request=_request(correlation_id="thread-2"), now=AT)

    assert len(list_records(context.wal_dir)) == 1
    assert len(_firehose_rows(canary)) == 1
    assert document_path(canary).read_bytes() == before


def test_a_denied_transition_records_no_receipt(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """A key that committed nothing stays free for the corrected request."""
    with pytest.raises(TransactionRefusedError):
        run_transaction(context=context, request=_request(to_status="COMPLETED"), now=AT)

    assert read_idempotency_receipt(context, namespaced_key=context.idempotency_key(KEY)) is None
    committed = run_transaction(context=context, request=_request(), now=AT)
    assert committed.replayed is False


def test_one_key_is_free_on_another_root(tmp_path: Path) -> None:
    """Keys are namespaced per root, so two trees never answer each other."""
    left = provision(tmp_path / "leftrepo", code="LEFT")
    right = provision(tmp_path / "rightrepo", code="RIGHT")
    for provisioned in (left, right):
        seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})

    first = run_transaction(
        context=root_context(left, tmp_path / "runtime"), request=_request(), now=AT
    )
    second = run_transaction(
        context=root_context(right, tmp_path / "runtime"), request=_request(), now=AT
    )

    assert second.replayed is False
    assert second.receipt.event_id != first.receipt.event_id


def test_an_unreadable_receipt_refuses_rather_than_committing_twice(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)
    stored = idempotency_receipt_path(context, namespaced_key=context.idempotency_key(KEY))
    stored.write_bytes(b"{truncated")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(), now=AT)

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert len(list_records(context.wal_dir)) == 1


def test_a_receipt_body_that_does_not_validate_refuses(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)
    stored = idempotency_receipt_path(context, namespaced_key=context.idempotency_key(KEY))
    payload = orjson.loads(stored.read_bytes())
    payload["receipt"] = {"event_name": "domain.milestone.activated"}
    stored.write_bytes(orjson.dumps(payload))

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(), now=AT)

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert "does not validate" in caught.value.detail


# ---------------------------------------------------------------------------
# The same contract over the wire
# ---------------------------------------------------------------------------


def test_rpc_replay_returns_the_original_receipt_and_publishes_once(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    published: list[Any] = []
    ctx.bus = type("_Bus", (), {"publish": staticmethod(published.append)})()

    first = _rpc(canary, ctx)
    second = _rpc(canary, ctx)

    assert (first["status"], second["status"]) == ("ok", "ok")
    assert second["result"] == first["result"]
    assert second["warnings"] == []
    assert len(published) == 1
    assert len(_firehose_rows(canary)) == 1


def test_rpc_reports_a_reused_key_as_idempotency_conflict(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    _rpc(canary, ctx)
    before = document_path(canary).read_bytes()

    answer = _rpc(canary, ctx, correlation_id="thread-2")

    assert answer["status"] == "error"
    assert answer["result"] is None
    assert answer["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert answer["errors"][0]["entity_ref"] == MILESTONE_URN
    assert document_path(canary).read_bytes() == before
    assert len(_firehose_rows(canary)) == 1
