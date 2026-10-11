"""A Batch carries an optional title, shown beside its key, and keeps it to the end.

A title is admitted by the create verb and by a plan apply, and a Batch without one
still validates. ``domain.batch.set_title`` titles a Batch the document holds through
the transaction's write step, and a Batch compacted into its ledger by appending a line
that supersedes the standing one; either way the status does not move. The projection
names the Batch ``KEY title``, and an imported epoch-1 Batch falls back to the title
its import kept among the fields with no native home.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.epoch2.lifecycle import LifecycleSourceIndex, map_iter_row
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.state.epoch2.batch import BatchCreateSpec, DeliveryBatch
from eawf.kernel.state.epoch2.plan_revision import PlannedBatch
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    latest_rows,
    read_ledger_records,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_create import run_create
from eawf.runtime.daemon.methods.batch_title import BATCH_SET_TITLE, TITLE_SET_EVENT
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.surfaces.tui.console.renderers.children import reference
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    TASK_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_create_transition import (
    admit_chain,
    request,
)
from tests.integration.workflow.planning.test_plan_revision_apply import (
    BODY,
    apply_plan,
    approve,
    collection,
    planned_canary,
    submit_body,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
KEY: Final = "req-title-0001"
TITLE: Final = "Ship the **wheel** build"
SCOPE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
LONGEST: Final = "x" * 80


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _open_canary(tmp_path: Path, status: str = "PLANNED") -> CanaryProvision:
    """Provision a canary whose document holds BAT-0007 at *status*."""
    canary = provision(tmp_path / "tree", code="TTL")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", status)}})
    return canary


def _compacted_canary(tmp_path: Path) -> tuple[CanaryProvision, Path]:
    """Provision a canary whose COMPLETED BAT-0007 lives only in the batch ledger."""
    canary = provision(tmp_path / "tree", code="TTL")
    path = ledger_path(document_path(canary), Epoch2Collection.BATCH)
    # A compacted line holds the record as its model dumps it.
    row = DeliveryBatch.model_validate(seed_row("batch", "COMPLETED")).model_dump(mode="json")
    # A cycle line under another key shares the ledger and must be passed over.
    append_ledger_record(
        path,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key="CYC-BAT-0007",
            status="COMPLETED",
            recorded_at=AT,
            payload={"batch_ref": BATCH_URN},
        ),
    )
    append_ledger_record(
        path,
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key="BAT-0007",
            status="COMPLETED",
            recorded_at=AT,
            payload=row,
        ),
    )
    return canary, path


def _drive(canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    """Send the verb against *canary* and return its envelope."""
    payload: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": BATCH_URN,
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
        "title": TITLE,
        **params,
    }
    return asyncio.run(
        methods.dispatch(BATCH_SET_TITLE, method_context(tmp_path / "runtime"), payload)
    )


def _stored(canary: CanaryProvision) -> dict[str, Any]:
    row = read_document(document_path(canary))["batch"]["BAT-0007"]
    assert isinstance(row, dict)
    return row


def _firehose(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _spine_title(document: dict[str, Any], ledger: tuple[dict[str, Any], ...] = ()) -> str:
    """Return the Batch child row a console draws for BAT-0007."""
    projection = build_route_projection(
        route="batch.detail",
        document=document,
        cursor=1,
        scope_id=SCOPE,
        generated_at=datetime(2026, 10, 9, tzinfo=UTC),
        ledger_rows={Epoch2Collection.BATCH: ledger},
    )
    row = next(row for row in build_spine_view(projection).rows if row.key in {"BAT-0007"})
    return reference(row)


# ---- the models ----------------------------------------------------------------------


def test_an_untitled_batch_still_validates_and_dumps_no_title() -> None:
    """Old rows load: the field is optional and absent from the dump while unset."""
    batch = DeliveryBatch.model_validate(seed_row("batch", "COMPLETED"))
    planned = PlannedBatch.model_validate(BODY["batches"][0])

    assert batch.title is None and planned.title is None
    assert "title" not in batch.model_dump(mode="json")
    assert "title" not in planned.model_dump(mode="json")


@pytest.mark.parametrize("model", [DeliveryBatch, BatchCreateSpec, PlannedBatch])
@pytest.mark.parametrize("title", ["x" * 81, "", "   ", 42])
def test_every_batch_shape_refuses_a_title_that_is_too_long_blank_or_not_text(
    model: type[DeliveryBatch | BatchCreateSpec | PlannedBatch], title: Any
) -> None:
    fields = set(model.model_fields)
    seed_fields = {**seed_row("batch", "PLANNED"), **BODY["batches"][0]}
    payload = {name: value for name, value in seed_fields.items() if name in fields}

    with pytest.raises(ValidationError, match="title"):
        model.model_validate({**payload, "title": title})


@pytest.mark.parametrize("title", ["x", LONGEST])
def test_a_title_of_one_to_eighty_characters_is_admitted(title: str) -> None:
    batch = DeliveryBatch.model_validate({**seed_row("batch", "PLANNED"), "title": title})
    assert batch.model_dump(mode="json")["title"] == title


# ---- create and plan -----------------------------------------------------------------


def test_create_admits_a_titled_batch(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="CREATE")
    context = root_context(canary, tmp_path / "runtime")
    admit_chain(context, upto=2)
    titled = request("batch", expected_revision=2)
    titled = titled.model_copy(update={"spec": {**titled.spec, "title": TITLE}})

    run_create(context=context, request=titled, now=AT)

    assert _stored(canary)["title"] == TITLE


def test_create_refuses_an_overlong_title(tmp_path: Path) -> None:
    canary = provision(tmp_path / "repo", code="CREATE")
    context = root_context(canary, tmp_path / "runtime")
    admit_chain(context, upto=2)
    titled = request("batch", expected_revision=2)

    with pytest.raises(ValidationError, match="title"):
        BatchCreateSpec.model_validate({**titled.spec, "title": "x" * 81})


def test_a_plan_apply_materialises_the_batch_title(tmp_path: Path) -> None:
    canary = planned_canary(tmp_path, code="TITLED")
    body = json.loads(json.dumps(BODY))
    body["batches"][0]["title"] = TITLE
    assert submit_body(canary, tmp_path, body=body)["status"] == "ok"
    assert approve(canary, tmp_path)["status"] == "ok"

    answer = apply_plan(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert collection(canary, "batch")["BAT-0007"]["title"] == TITLE


def test_an_untitled_plan_apply_leaves_the_batch_untitled(tmp_path: Path) -> None:
    canary = planned_canary(tmp_path, code="UNTITLED")
    assert submit_body(canary, tmp_path, body=BODY)["status"] == "ok"
    assert approve(canary, tmp_path)["status"] == "ok"

    assert apply_plan(canary, tmp_path)["status"] == "ok"

    assert "title" not in collection(canary, "batch")["BAT-0007"]


# ---- set-title on a Batch the document holds -----------------------------------------


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "COMPLETED"])
def test_set_title_titles_a_document_batch_and_leaves_its_status(
    status: str, tmp_path: Path
) -> None:
    canary = _open_canary(tmp_path, status)

    answer = _drive(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert answer["result"]["event_name"] == TITLE_SET_EVENT
    row = _stored(canary)
    assert (row["title"], row["status"], row["revision"]) == (TITLE, status, 2)
    [event] = _firehose(canary)
    assert (event["payload"]["from_status"], event["payload"]["to_status"]) == (status, status)


def test_set_title_replaces_an_earlier_title(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)
    _drive(canary, tmp_path)

    answer = _drive(
        canary, tmp_path, expected_revision=2, idempotency_key="req-title-0002", title=LONGEST
    )

    assert answer["status"] == "ok", answer["errors"]
    assert (_stored(canary)["title"], _stored(canary)["revision"]) == (LONGEST, 3)


def test_set_title_refuses_a_stale_revision_and_writes_nothing(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)
    before = document_path(canary).read_bytes()

    answer = _drive(canary, tmp_path, expected_revision=2)

    assert answer["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert document_path(canary).read_bytes() == before
    assert _firehose(canary) == []


@pytest.mark.parametrize("title", ["x" * 81, "", "  "])
def test_set_title_refuses_a_title_out_of_bounds(title: str, tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)
    before = document_path(canary).read_bytes()

    answer = _drive(canary, tmp_path, title=title)

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "title" in row["message"]
    assert document_path(canary).read_bytes() == before


def test_set_title_refuses_a_request_without_a_title(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)
    params = {
        "repo_root": str(canary.root),
        "urn": BATCH_URN,
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
    }

    answer = asyncio.run(
        methods.dispatch(BATCH_SET_TITLE, method_context(tmp_path / "runtime"), params)
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value


def test_set_title_refuses_a_urn_addressing_another_entity(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)

    answer = _drive(canary, tmp_path, urn=TASK_URN)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_KIND_MISMATCH.value
    assert _firehose(canary) == []


def test_set_title_refuses_a_batch_the_tree_never_held(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="TTL")

    answer = _drive(canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_NOT_FOUND.value


def test_set_title_retry_replays_and_other_parameters_conflict(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path)

    first = _drive(canary, tmp_path)
    again = _drive(canary, tmp_path)
    other = _drive(canary, tmp_path, title="Another title")

    assert again["status"] == "ok" and again["result"] == first["result"]
    assert other["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(_firehose(canary)) == 1


# ---- set-title on a Batch compacted into its ledger ----------------------------------


def test_set_title_supersedes_a_compacted_completed_batch(tmp_path: Path) -> None:
    canary, path = _compacted_canary(tmp_path)
    before = path.read_bytes()

    answer = _drive(canary, tmp_path)

    assert answer["status"] == "ok", answer["errors"]
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert answer["result"]["event_name"] == TITLE_SET_EVENT
    assert path.read_bytes().startswith(before), "no earlier line is edited"
    *_, original, amendment = read_ledger_records(path)
    assert amendment.supersedes is not None and amendment.record_key == "BAT-0007"
    assert amendment.status == original.status == "COMPLETED"
    stored = latest_rows(path)["BAT-0007"]
    assert (stored["title"], stored["status"], stored["revision"]) == (TITLE, "COMPLETED", 2)
    unchanged = {name for name in original.payload if name not in {"revision", "updated_at"}}
    assert {name: stored[name] for name in unchanged} == {
        name: original.payload[name] for name in unchanged
    }
    assert "BAT-0007" not in read_document(document_path(canary)).get("batch", {})


def test_set_title_on_a_compacted_batch_replays_and_then_conflicts(tmp_path: Path) -> None:
    canary, path = _compacted_canary(tmp_path)

    first = _drive(canary, tmp_path)
    again = _drive(canary, tmp_path)
    stale = _drive(canary, tmp_path, idempotency_key="req-title-0002", title="Another title")
    other = _drive(canary, tmp_path, title="Another title")

    assert again["status"] == "ok" and again["result"] == first["result"]
    assert stale["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert other["errors"][0]["code"] == DomainErrorCode.IDEMPOTENCY_CONFLICT.value
    assert len(read_ledger_records(path)) == 3


def test_set_title_twice_on_a_compacted_batch_supersedes_the_amendment(tmp_path: Path) -> None:
    canary, path = _compacted_canary(tmp_path)
    _drive(canary, tmp_path)

    answer = _drive(
        canary, tmp_path, expected_revision=2, idempotency_key="req-title-0002", title=LONGEST
    )

    assert answer["status"] == "ok", answer["errors"]
    assert (latest_rows(path)["BAT-0007"]["title"], latest_rows(path)["BAT-0007"]["revision"]) == (
        LONGEST,
        3,
    )


def test_set_title_refuses_an_imported_batch_line_that_states_no_key(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="TTL")
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.BATCH),
        LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key="BAT-0007",
            status="COMPLETED",
            recorded_at=AT,
            payload={"lifecycle": "iter", "legacy_refs": {"title": "Old"}},
        ),
    )

    answer = _drive(canary, tmp_path)

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_NOT_FOUND.value


# ---- what a console draws ------------------------------------------------------------


def test_a_titled_document_batch_is_drawn_key_then_title(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path, "ACTIVE")
    _drive(canary, tmp_path)

    drawn = _spine_title(read_document(document_path(canary)))

    assert drawn == "BAT-0007 · ACTIVE · Ship the wheel build"


def test_a_titled_compacted_batch_is_drawn_key_then_title(tmp_path: Path) -> None:
    canary, path = _compacted_canary(tmp_path)
    _drive(canary, tmp_path)
    document = read_document(document_path(canary))

    drawn = _spine_title(document, (dict(latest_rows(path)["BAT-0007"]),))

    assert drawn == "BAT-0007 · COMPLETED · Ship the wheel build"


def test_an_untitled_batch_is_drawn_by_its_key_alone(tmp_path: Path) -> None:
    canary = _open_canary(tmp_path, "ACTIVE")

    assert _spine_title(read_document(document_path(canary))) == "BAT-0007 · ACTIVE"


def test_an_imported_batch_falls_back_to_its_legacy_title(tmp_path: Path) -> None:
    """An epoch-1 iter held in the document keeps its title in ``legacy_refs``."""
    canary = provision(tmp_path / "tree", code="TTL")
    imported = map_iter_row(
        source_id="BAT-0007",
        row={"id": "BAT-0007", "phase_id": "MLS-0030", "status": "closed", "title": "Old work"},
        index=LifecycleSourceIndex.build(
            {"schema_version": "1.20", "tracks": None}, report_rows=()
        ),
    )
    document = read_document(document_path(canary))
    document["batch"] = {
        "BAT-0007": {
            "status": imported.target_status,
            "payload": imported.model_dump(mode="json"),
            "continuation": [],
        }
    }
    write_document(document_path(canary), document)

    drawn = _spine_title(read_document(document_path(canary)))

    assert drawn == "BAT-0007 · COMPLETED · Old work"
