"""Every committed native mutation files which fields it changed, before and after.

A transition, a create, a Batch listing its new Task and a row write each file one change
record per row they changed, journalled in the commit's WAL envelope and appended once the
document write is durable. A refused mutation files none. Changes to Runs and to a Task in
flight land in the generation's local tier; every other change lands in its committed
tier. The daemon's read serves the feed newest first, a page at a time.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.changes import (
    ChangeRecord,
    ChangeTier,
    change_log_path,
    change_tier,
    read_change_page,
)
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import epoch2_changes, epoch2_transaction, methods
from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
from eawf.runtime.daemon.epoch2_recovery import CHANGE_LINES_KEY, replay_native_wal
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    commit_row_write,
    run_transaction,
)
from eawf.runtime.daemon.methods.console_records import HISTORY_CHANGES_READ_METHOD
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
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"


class _KilledError(Exception):
    """The injected crash."""


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding a planned Milestone, a planned Batch and a draft and a claimed Task."""
    provisioned = provision(tmp_path / "repo", code="FEED")
    claimed = seed_row("task", "CLAIMED")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "PLANNED")},
            "batch": {"BAT-0007": seed_row("batch", "PLANNED")},
            "task": {"EAWF-0042": seed_row("task", "DRAFT")},
        },
    )
    assert claimed["status"] == "CLAIMED"
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary."""
    return root_context(canary, tmp_path / "runtime")


def _transition(urn: str, to_status: str, revision: int = 1, **extra: Any) -> TransitionRequest:
    return TransitionRequest.model_validate(
        {
            "urn": urn,
            "to_status": to_status,
            "expected_revision": revision,
            "idempotency_key": f"req-{urn.rsplit('/', 1)[1]}-{to_status}",
            "actor": ACTOR,
            **extra,
        }
    )


def _feed(canary: CanaryProvision, tier: ChangeTier) -> list[ChangeRecord]:
    path = change_log_path(document_path(canary), tier)
    if not path.is_file():
        return []
    return [ChangeRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _fields(record: ChangeRecord) -> dict[str, tuple[Any, Any]]:
    return {
        change.field: (
            None if change.before is None else change.before.value,
            None if change.after is None else change.after.value,
        )
        for change in record.changes
    }


def test_a_transition_files_its_changed_fields_before_and_after(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(
        context=context, request=_transition(MILESTONE_URN, "ACTIVE"), now=AT
    )
    [record] = _feed(canary, ChangeTier.COMMITTED)
    assert record.record_key == "MLS-0030"
    assert record.collection is Epoch2Collection.MILESTONE
    assert (record.revision_before, record.revision_after) == (1, 2)
    assert record.canonical_sequence == committed.receipt.canonical_sequence
    assert record.actor_ref == ACTOR
    assert record.event_name == "domain.milestone.activated"
    assert _fields(record)["status"] == ("PLANNED", "ACTIVE")
    assert "revision" not in _fields(record) and "updated_at" not in _fields(record)
    assert _feed(canary, ChangeTier.LOCAL) == []


def test_the_firehose_row_journals_the_change_lines(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_transition(MILESTONE_URN, "ACTIVE"), now=AT)
    [row] = [json.loads(line) for line in firehose_path(canary).read_text().splitlines()]
    [line] = row["payload"][CHANGE_LINES_KEY]
    assert ChangeRecord.model_validate_json(line) == _feed(canary, ChangeTier.COMMITTED)[0]


def test_a_promotion_files_the_task_and_the_batch_listing_it(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    criteria = [item.model_dump(mode="json") for item in world.criteria("CR-01")]
    request = _transition(
        TASK_URN,
        "PLANNED",
        updates={"batch_ref": BATCH_URN, "criteria": criteria, "due_scope": MILESTONE_URN},
    )
    run_transaction(context=context, request=request, now=AT)
    records = {record.record_key: record for record in _feed(canary, ChangeTier.COMMITTED)}
    assert set(records) == {"BAT-0007", "EAWF-0042"}
    assert records["BAT-0007"].canonical_sequence == records["EAWF-0042"].canonical_sequence
    assert "task_refs" in _fields(records["BAT-0007"])
    assert _fields(records["EAWF-0042"])["status"] == ("DRAFT", "PLANNED")


def test_a_create_files_every_field_as_added(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    spec = {
        key: value
        for key, value in seed_row("track", "ACTIVE").items()
        if key
        not in {"uid", "urn", "origin", "revision", "created_at", "updated_at", "status"}
        | {"milestone_refs", "campaign_refs", "contract_revision"}
    }
    run_create(
        context=context,
        request=CreateRequest.model_validate(
            {
                "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME",
                "expected_revision": 0,
                "idempotency_key": "req-create-track",
                "actor": ACTOR,
                "spec": spec,
            }
        ),
        now=AT,
    )
    [record] = _feed(canary, ChangeTier.COMMITTED)
    assert record.record_key == "TRK-RUNTIME"
    assert record.revision_before is None and record.revision_after == 1
    assert all(change.before is None for change in record.changes)
    assert _fields(record)["status"] == (None, "ACTIVE")


def test_a_run_and_an_in_flight_task_file_their_changes_locally(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    claimed = seed_row("task", "CLAIMED")
    seed(canary, {"task": {"EAWF-0042": claimed}, "run": {"RUN-00000010": _queued_run()}})
    context = root_context(canary, tmp_path / "runtime")
    run_transaction(
        context=context,
        request=_transition(RUN_URN, "RUNNING", updates={"started_at": AT.isoformat()}),
        now=AT,
    )
    run_transaction(
        context=context,
        request=_transition(
            TASK_URN,
            "RUNNING",
            revision=claimed["revision"],
            updates={"active_run_ref": RUN_URN},
        ),
        now=AT,
    )
    local = _feed(canary, ChangeTier.LOCAL)
    assert [record.record_key for record in local] == ["RUN-00000010", "EAWF-0042"]
    assert all(record.tier is ChangeTier.LOCAL for record in local)
    assert _fields(local[1])["status"] == ("CLAIMED", "RUNNING")
    assert _feed(canary, ChangeTier.COMMITTED) == []


def _queued_run() -> dict[str, Any]:
    row = seed_row("run", "QUEUED")
    row["key"] = "RUN-00000010"
    row["urn"] = RUN_URN
    return row


def test_an_in_flight_task_change_is_local_and_a_planned_one_committed() -> None:
    assert change_tier(Epoch2Collection.TASK, {"status": "CLAIMED"}) is ChangeTier.LOCAL
    assert change_tier(Epoch2Collection.TASK, {"status": "RUNNING"}) is ChangeTier.LOCAL
    assert change_tier(Epoch2Collection.TASK, {"status": "PLANNED"}) is ChangeTier.COMMITTED
    assert change_tier(Epoch2Collection.TASK, {"status": "COMPLETED"}) is ChangeTier.COMMITTED
    assert change_tier(Epoch2Collection.RUN, {"status": "COMPLETED"}) is ChangeTier.LOCAL
    assert change_tier(Epoch2Collection.MILESTONE, {}) is ChangeTier.COMMITTED


@pytest.mark.parametrize(
    "request_overrides",
    [
        {"to_status": "COMPLETED"},
        {"expected_revision": 7},
        {"updates": {"title": "ghp_" + "a" * 36}},
    ],
    ids=["denied-edge", "stale-revision", "leak-refused"],
)
def test_a_refused_mutation_files_no_change(
    context: Epoch2RootContext, canary: CanaryProvision, request_overrides: dict[str, Any]
) -> None:
    payload: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "to_status": "ACTIVE",
        "expected_revision": 1,
        "idempotency_key": "req-refused",
        "actor": ACTOR,
        **request_overrides,
    }
    with pytest.raises(TransactionRefusedError):
        run_transaction(context=context, request=TransitionRequest.model_validate(payload), now=AT)
    for tier in ChangeTier:
        assert not change_log_path(document_path(canary), tier).exists()


def test_a_crash_before_the_document_write_files_no_change(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    def killed(*_args: Any, **_kwargs: Any) -> None:
        raise _KilledError

    monkeypatch.setattr(epoch2_transaction.RootSession, "write_document", killed)
    with pytest.raises(_KilledError):
        run_transaction(context=context, request=_transition(MILESTONE_URN, "ACTIVE"), now=AT)
    monkeypatch.undo()
    replay_native_wal(context.wal_dir.parent.parent)
    assert _feed(canary, ChangeTier.COMMITTED) == []


def test_a_crash_after_the_document_write_is_finished_by_the_replay_once(
    context: Epoch2RootContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    def killed(*_args: Any, **_kwargs: Any) -> None:
        raise _KilledError

    monkeypatch.setattr(epoch2_changes, "append_change_record", killed)
    with pytest.raises(_KilledError):
        run_transaction(context=context, request=_transition(MILESTONE_URN, "ACTIVE"), now=AT)
    monkeypatch.undo()
    assert _feed(canary, ChangeTier.COMMITTED) == []

    daemon_wal = context.wal_dir.parent.parent
    replay_native_wal(daemon_wal)
    replay_native_wal(daemon_wal)

    [record] = _feed(canary, ChangeTier.COMMITTED)
    assert _fields(record)["status"] == ("PLANNED", "ACTIVE")


def test_each_tier_of_the_feed_is_declared_where_its_status_split_says(
    canary: CanaryProvision,
) -> None:
    tree = canary.root
    document = document_path(canary)
    committed = change_log_path(document, ChangeTier.COMMITTED).relative_to(tree).as_posix()
    local = change_log_path(document, ChangeTier.LOCAL).relative_to(tree).as_posix()
    assert classify_path(committed).policy is CommitPolicy.COMMITTED
    assert classify_path(local).policy is CommitPolicy.NOT_COMMITTED


def test_the_daemon_read_pages_the_feed_newest_first(
    context: Epoch2RootContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    run_transaction(context=context, request=_transition(MILESTONE_URN, "ACTIVE"), now=AT)
    run_transaction(context=context, request=_transition(TASK_URN, "DEFERRED"), now=AT)

    def read(**params: Any) -> dict[str, Any]:
        return asyncio.run(
            methods.dispatch(
                HISTORY_CHANGES_READ_METHOD,
                method_context(tmp_path / "runtime"),
                {"repo_root": str(canary.root), **params},
            )
        )

    first = read(limit=1)
    assert [item["record_key"] for item in first["changes"]] == ["EAWF-0042"]
    assert first["since"] == "2026-10-02"
    second = read(limit=1, cursor=first["next_cursor"])
    assert [item["record_key"] for item in second["changes"]] == ["MLS-0030"]
    assert second["next_cursor"] is None
    one = read(key="MLS-0030")
    assert [item["record_key"] for item in one["changes"]] == ["MLS-0030"]
    assert read(key="MLS-0999")["changes"] == []
    page = read_change_page(document_path(canary), record_key=None, cursor=None, limit=50)
    assert [record.record_key for record in page.changes] == ["EAWF-0042", "MLS-0030"]


def test_a_row_write_files_its_change_under_the_actor_its_event_names(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    row = {"key": "CAM-0001", "status": "OPEN", "revision": 1, "title": "Find the leak"}
    with context.session([MILESTONE_URN]) as session:
        commit_row_write(
            session,
            collection=Epoch2Collection.CAMPAIGN,
            record_key="CAM-0001",
            row=row,
            event_name="domain.campaign.opened",
            event_fields={"actor": ACTOR},
            compaction=None,
            now=AT,
        )
    [record] = _feed(canary, ChangeTier.COMMITTED)
    assert (record.collection, record.record_key) == (Epoch2Collection.CAMPAIGN, "CAM-0001")
    assert record.actor_ref == ACTOR and record.event_name == "domain.campaign.opened"
    assert _fields(record) == {
        "key": (None, "CAM-0001"),
        "status": (None, "OPEN"),
        "title": (None, "Find the leak"),
    }
