"""Milestone and Track maintenance verbs commit through the daemon or write nothing.

Each verb is driven through the daemon dispatcher against a provisioned canary,
so the request model, the fence and the commit are the ones a CLI call reaches.
A refusal is checked to leave the document and the ledgers byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, latest_rows, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.kernel.validate.epoch2 import validate_generation
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import domain
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.methods.milestone_maintenance import MILESTONE_EDIT, MILESTONE_PURGE
from eawf.runtime.daemon.methods.track_maintenance import TRACK_RENAME, TRACK_SET_POLICY
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    rekeyed,
    seed,
    seed_row,
)

ACTOR: Final = "OP-0001"
_CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK_URN: Final = f"{_CONTAINER}/track/TRK-RUNTIME"
OTHER_MILESTONE_URN: Final = f"{_CONTAINER}/milestone/MLS-0031"
CANCEL: Final = "domain.milestone.cancel"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def _git(root: Path, *args: str) -> None:
    """Run one git command in *root*, failing the test when it fails."""
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo(canary: CanaryProvision) -> None:
    """Make the canary root a git checkout on ``main``, as a real tree is."""
    _git(canary.root, "init", "--quiet", "--initial-branch=main")
    _git(canary.root, "config", "user.email", "agent@example.invalid")
    _git(canary.root, "config", "user.name", "agent")
    _git(canary.root, "config", "commit.gpgsign", "false")


def _push_everything(canary: CanaryProvision, remote: Path) -> None:
    """Commit the whole tree and push it to a bare *remote*, creating it on first use."""
    if not remote.exists():
        _git(remote.parent, "init", "--quiet", "--bare", remote.name)
        _git(canary.root, "remote", "add", "origin", str(remote))
    _git(canary.root, "add", "--force", "--all", ".")
    _git(canary.root, "commit", "--quiet", "--allow-empty", "-m", "state")
    _git(canary.root, "push", "--quiet", "origin", "main")


def _drive(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Send *method* against *canary* and return its envelope."""
    ctx = method_context(tmp_path / "runtime")
    request: dict[str, Any] = {
        "repo_root": str(canary.root),
        "expected_revision": 1,
        "actor": ACTOR,
        **params,
    }
    return asyncio.run(methods.dispatch(method, ctx, request))


def _document(canary: CanaryProvision) -> dict[str, Any]:
    return read_document(document_path(canary))


def _ledger(canary: CanaryProvision, collection: Epoch2Collection) -> Path:
    return ledger_path(document_path(canary), collection)


def _ledger_bytes(canary: CanaryProvision, collection: Epoch2Collection) -> bytes:
    path = _ledger(canary, collection)
    return path.read_bytes() if path.exists() else b""


def _firehose(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _snapshot(canary: CanaryProvision) -> tuple[bytes, ...]:
    """Return the document and every lifecycle ledger, to prove a refusal wrote nothing."""
    return (
        document_path(canary).read_bytes(),
        *(
            _ledger_bytes(canary, collection)
            for collection in (
                Epoch2Collection.MILESTONE,
                Epoch2Collection.BATCH,
                Epoch2Collection.TASK,
            )
        ),
    )


def _milestone(status: str, key: str = "MLS-0030") -> dict[str, Any]:
    return rekeyed(seed_row("milestone", status), key=key)


def _task(status: str, key: str, **overrides: Any) -> dict[str, Any]:
    row = rekeyed(seed_row("task", status), key=key)
    row.update(overrides)
    return row


def test_milestone_edit_changes_only_a_planned_milestone(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="EDT")
    seed(
        canary,
        {
            "milestone": {
                "MLS-0030": _milestone("PLANNED"),
                "MLS-0031": _milestone("ACTIVE", key="MLS-0031"),
            }
        },
    )

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_EDIT,
        urn=MILESTONE_URN,
        idempotency_key="req-edit-0001",
        title="Publish a signed installable wheel",
        exclusions=["platform packaging for Windows", "a conda recipe"],
    )

    assert answer["status"] == "ok", answer["errors"]
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    row = _document(canary)["milestone"]["MLS-0030"]
    assert row["title"] == "Publish a signed installable wheel"
    assert row["exclusions"] == ["platform packaging for Windows", "a conda recipe"]
    assert (row["status"], row["revision"]) == ("PLANNED", 2)
    assert row["outcome"] == seed_row("milestone", "PLANNED")["outcome"]

    before = _snapshot(canary)
    refused = _drive(
        canary,
        tmp_path,
        MILESTONE_EDIT,
        urn=OTHER_MILESTONE_URN,
        idempotency_key="req-edit-0002",
        title="Rename an active Milestone",
    )

    assert refused["status"] == "error"
    assert refused["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert refused["revision_before"] == refused["revision_after"] == 1
    assert _snapshot(canary) == before


def test_milestone_edit_replays_a_retry_and_refuses_a_stale_revision(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="EDR")
    seed(canary, {"milestone": {"MLS-0030": _milestone("PLANNED")}})
    params: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "idempotency_key": "req-edit-0003",
        "outcome": "An operator installs the signed wheel and the CLI answers.",
    }

    first = _drive(canary, tmp_path, MILESTONE_EDIT, **params)
    retry = _drive(canary, tmp_path, MILESTONE_EDIT, **params)
    stale = _drive(
        canary, tmp_path, MILESTONE_EDIT, **{**params, "idempotency_key": "req-edit-0004"}
    )

    assert first["status"] == retry["status"] == "ok"
    assert retry["result"] == first["result"]
    assert len(_firehose(canary)) == 1
    assert stale["errors"][0]["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert stale["revision_before"] == 2


@pytest.mark.parametrize(
    ("params", "field"),
    [
        # a request naming no field fails the model as a whole, so no field is named
        ({}, "check"),
        ({"title": ""}, "title"),
        ({"title": "x" * 200}, "title"),
        ({"exclusions": []}, "exclusions"),
        ({"appetite": "XXL"}, "appetite"),
        ({"status": "ACTIVE", "title": "Move it"}, "status"),
    ],
)
def test_milestone_edit_refuses_a_request_its_model_does_not_accept(
    params: dict[str, Any], field: str, tmp_path: Path
) -> None:
    canary = provision(tmp_path / "tree", code="EDS")
    seed(canary, {"milestone": {"MLS-0030": _milestone("PLANNED")}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_EDIT,
        urn=MILESTONE_URN,
        idempotency_key="req-edit-0005",
        **params,
    )

    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert field in row["message"]
    assert _snapshot(canary) == before


def test_milestone_edit_refuses_a_urn_of_another_kind(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="EDK")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_EDIT,
        urn=TRACK_URN,
        idempotency_key="req-edit-0006",
        title="Rename through the wrong verb",
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.IDENTITY_KIND_MISMATCH.value
    assert _snapshot(canary) == before


def _cascade_tree(tmp_path: Path) -> CanaryProvision:
    """Seed an ACTIVE Milestone owning a PLANNED and an ACTIVE Batch, their Tasks and drafts."""
    canary = provision(tmp_path / "tree", code="CNC")
    _git_repo(canary)
    planned_batch = f"{_CONTAINER}/batch/BAT-0007"
    active_batch = f"{_CONTAINER}/batch/BAT-0008"
    other_batch = f"{_CONTAINER}/batch/BAT-0009"
    elsewhere = rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0009")
    elsewhere["milestone_ref"] = OTHER_MILESTONE_URN
    seed(
        canary,
        {
            "milestone": {
                "MLS-0030": _milestone("ACTIVE"),
                "MLS-0031": _milestone("ACTIVE", key="MLS-0031"),
            },
            "batch": {
                "BAT-0007": seed_row("batch", "PLANNED"),
                "BAT-0008": rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0008"),
                "BAT-0009": elsewhere,
            },
            "task": {
                "EAWF-0042": _task("PLANNED", "EAWF-0042", batch_ref=planned_batch),
                "EAWF-0043": _task("RUNNING", "EAWF-0043", batch_ref=active_batch),
                "EAWF-0044": _task("CLAIMED", "EAWF-0044", batch_ref=active_batch),
                "EAWF-0045": _task("DRAFT", "EAWF-0045", due_scope=MILESTONE_URN),
                "EAWF-0046": _task("DEFERRED", "EAWF-0046", due_scope=MILESTONE_URN),
                "EAWF-0047": _task("DRAFT", "EAWF-0047", due_scope=OTHER_MILESTONE_URN),
                "EAWF-0048": _task(
                    "PLANNED", "EAWF-0048", batch_ref=other_batch, due_scope=OTHER_MILESTONE_URN
                ),
            },
        },
    )
    return canary


def test_milestone_cancel_cascades_to_batches_and_tasks(tmp_path: Path) -> None:
    canary = _cascade_tree(tmp_path)

    answer = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-cancel-0001",
        reason_code="superseded-by-replan",
    )

    assert answer["status"] == "ok", answer["errors"]
    milestones = latest_rows(_ledger(canary, Epoch2Collection.MILESTONE))
    batches = latest_rows(_ledger(canary, Epoch2Collection.BATCH))
    tasks = latest_rows(_ledger(canary, Epoch2Collection.TASK))
    document = _document(canary)
    assert milestones["MLS-0030"]["status"] == "CANCELLED"
    assert {key: batches[key]["status"] for key in ("BAT-0007", "BAT-0008")} == {
        "BAT-0007": "CANCELLED",
        "BAT-0008": "CANCELLED",
    }
    # a never-activated Batch records the branch the repository stands on
    assert batches["BAT-0007"]["target_branch"] == "main"
    assert {key: tasks[key]["status"] for key in ("EAWF-0042", "EAWF-0043", "EAWF-0044")} == {
        "EAWF-0042": "CANCELLED",
        "EAWF-0043": "CANCELLED",
        "EAWF-0044": "CANCELLED",
    }
    # a dropped Task has no outgoing edge, so it compacts like one the drop verb moved
    assert (tasks["EAWF-0045"]["status"], tasks["EAWF-0046"]["status"]) == ("DROPPED", "DROPPED")
    rows = document["task"]
    # nothing outside the cancelled Milestone moves
    assert rows["EAWF-0047"]["status"] == "DRAFT"
    assert rows["EAWF-0048"]["status"] == "PLANNED"
    assert document["batch"]["BAT-0009"]["status"] == "ACTIVE"
    assert document["milestone"]["MLS-0031"]["status"] == "ACTIVE"
    # one commit: one firehose row carries every moved record
    [event] = _firehose(canary)
    assert event["payload"]["name"] == "domain.milestone.cancelled"


def test_milestone_cancel_refuses_an_open_task_it_cannot_cancel(tmp_path: Path) -> None:
    """A Task ready to integrate has no cancel edge, so the whole cancel writes nothing."""
    canary = _cascade_tree(tmp_path)
    ready = _task("READY_TO_INTEGRATE", "EAWF-0049", batch_ref=f"{_CONTAINER}/batch/BAT-0008")
    seed(canary, {"task": {"EAWF-0049": ready}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-cancel-0002",
        reason_code="superseded-by-replan",
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert "EAWF-0049" in answer["errors"][0]["message"]
    assert _snapshot(canary) == before
    assert _firehose(canary) == []


def test_milestone_purge_removes_only_an_unpushed_cancelled_record(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="PRG")
    _git_repo(canary)
    remote = tmp_path / "remote.git"
    seed(
        canary,
        {
            "milestone": {
                "MLS-0031": _milestone("PLANNED", key="MLS-0031"),
                "MLS-0032": _milestone("ACTIVE", key="MLS-0032"),
            }
        },
    )

    def cancel(urn: str) -> None:
        key = urn.rsplit("/", 1)[-1]
        answer = _drive(
            canary, tmp_path, CANCEL, urn=urn, idempotency_key=f"req-c-{key}", reason_code="drop"
        )
        assert answer["status"] == "ok", answer["errors"]

    # MLS-0031 is cancelled and pushed; MLS-0030 is created and cancelled afterwards,
    # so no commit the remote holds has ever carried it
    cancel(OTHER_MILESTONE_URN)
    _push_everything(canary, remote)
    seed(canary, {"milestone": {"MLS-0030": _milestone("PLANNED")}})
    cancel(MILESTONE_URN)

    def refused(urn: str, revision: int, code: DomainErrorCode) -> None:
        before = _snapshot(canary)
        answer = _drive(
            canary,
            tmp_path,
            MILESTONE_PURGE,
            urn=urn,
            expected_revision=revision,
            idempotency_key=f"req-p-{urn.rsplit('/', 1)[-1]}",
        )
        assert answer["status"] == "error"
        assert answer["errors"][0]["code"] == code.value, answer["errors"]
        assert _snapshot(canary) == before

    refused(f"{_CONTAINER}/milestone/MLS-0032", 1, DomainErrorCode.ILLEGAL_TRANSITION)
    refused(OTHER_MILESTONE_URN, 2, DomainErrorCode.TRANSITION_GUARD_FAILED)

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_PURGE,
        urn=MILESTONE_URN,
        expected_revision=2,
        idempotency_key="req-p-0003",
    )

    assert answer["status"] == "ok", answer["errors"]
    milestones = latest_rows(_ledger(canary, Epoch2Collection.MILESTONE))
    assert "MLS-0030" not in milestones
    assert milestones["MLS-0031"]["status"] == "CANCELLED"
    assert "MLS-0030" not in _document(canary)["milestone"]
    assert _firehose(canary)[-1]["payload"]["name"] == "milestone.purged"


def _references(canary: CanaryProvision, key: str) -> list[str]:
    """Return every Batch or Task row, in the document or a ledger, that names *key*."""
    document = _document(canary)
    named: list[str] = []
    for collection in (Epoch2Collection.BATCH, Epoch2Collection.TASK):
        rows = {**latest_rows(_ledger(canary, collection)), **document.get(collection.value, {})}
        named += [
            f"{collection.value}/{row_key}" for row_key, row in rows.items() if key in str(row)
        ]
    return sorted(named)


def test_milestone_purge_takes_its_cancelled_work_and_leaves_the_tree_valid(
    tmp_path: Path,
) -> None:
    canary = _cascade_tree(tmp_path)
    cancelled = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-c-0003",
        reason_code="drop",
    )
    assert cancelled["status"] == "ok", cancelled["errors"]
    assert _references(canary, "MLS-0030")

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_PURGE,
        urn=MILESTONE_URN,
        expected_revision=2,
        idempotency_key="req-p-0004",
    )

    assert answer["status"] == "ok", answer["errors"]
    assert _references(canary, "MLS-0030") == []
    payload = _firehose(canary)[-1]["payload"]
    assert payload["purged_batch_keys"] == ["BAT-0007", "BAT-0008"]
    assert payload["purged_task_keys"] == [
        "EAWF-0042",
        "EAWF-0043",
        "EAWF-0044",
        "EAWF-0045",
        "EAWF-0046",
    ]
    # the other Milestone's work is untouched
    document = _document(canary)
    assert document["batch"]["BAT-0009"]["status"] == "ACTIVE"
    assert {"EAWF-0047", "EAWF-0048"} <= set(document["task"])
    report = validate_generation(document_path(canary))
    assert (report.schema_errors, report.violations) == ([], [])


def test_milestone_purge_refuses_while_it_owns_open_work(tmp_path: Path) -> None:
    """A Task still in the backlog for the Milestone carries history a purge keeps."""
    canary = _cascade_tree(tmp_path)
    cancelled = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-c-0004",
        reason_code="drop",
    )
    assert cancelled["status"] == "ok", cancelled["errors"]
    seed(canary, {"task": {"EAWF-0050": _task("DRAFT", "EAWF-0050", due_scope=MILESTONE_URN)}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_PURGE,
        urn=MILESTONE_URN,
        expected_revision=2,
        idempotency_key="req-p-0005",
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert "EAWF-0050 DRAFT" in answer["errors"][0]["message"]
    assert _snapshot(canary) == before


def test_track_rename_and_set_policy(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="TRK")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})

    renamed = _drive(
        canary,
        tmp_path,
        TRACK_RENAME,
        urn=TRACK_URN,
        idempotency_key="req-rename-0001",
        title="Keep the runtime recoverable through provider outages",
    )
    policy = _drive(
        canary,
        tmp_path,
        TRACK_SET_POLICY,
        urn=TRACK_URN,
        expected_revision=2,
        idempotency_key="req-policy-0001",
        active_milestones=3,
    )

    assert renamed["status"] == "ok", renamed["errors"]
    assert policy["status"] == "ok", policy["errors"]
    row = _document(canary)["track"]["TRK-RUNTIME"]
    assert row["title"] == "Keep the runtime recoverable through provider outages"
    assert row["policy"]["wip"] == {"active_milestones": 3, "active_batches_per_repo": 1}
    # a policy edit moves the policy's own revision, which proofs bind
    assert (row["revision"], row["policy"]["revision"]) == (3, 2)
    assert [event["payload"]["name"] for event in _firehose(canary)] == [
        "track.renamed",
        "track.policy_set",
    ]


@pytest.mark.parametrize("value", [-1, "3", 1.5, None])
def test_track_set_policy_refuses_a_value_that_is_not_a_count(value: Any, tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="TRP")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        TRACK_SET_POLICY,
        urn=TRACK_URN,
        idempotency_key="req-policy-0002",
        active_milestones=value,
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert _snapshot(canary) == before


def test_track_maintenance_refuses_a_retired_track(tmp_path: Path) -> None:
    canary = provision(tmp_path / "tree", code="TRR")
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "RETIRED")}})
    before = _snapshot(canary)

    answer = _drive(
        canary,
        tmp_path,
        TRACK_RENAME,
        urn=TRACK_URN,
        idempotency_key="req-rename-0002",
        title="Rename a retired Track",
    )

    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert _snapshot(canary) == before


RUN_URN: Final = f"{_CONTAINER}/run/RUN-00000010"


def _live_run(task_key: str) -> dict[str, Any]:
    """Return the seeded RUNNING Run, working for Task *task_key*."""
    run = seed_row("run", "RUNNING")
    run["scope"]["task_ref"] = f"{_CONTAINER}/task/{task_key}"
    return run


def _control_facts(canary: CanaryProvision) -> list[tuple[str, str, str]]:
    """Return every control fact the run ledger holds, as (run, control, phase)."""
    return [
        (line.payload["run_ref"], line.payload["control"], line.payload["phase"])
        for line in read_ledger_records(_ledger(canary, Epoch2Collection.RUN))
        if line.payload.get("payload_kind") == "control"
    ]


def test_milestone_cancel_asks_the_live_run_of_a_running_task_to_cancel(tmp_path: Path) -> None:
    canary = _cascade_tree(tmp_path)
    seed(canary, {"run": {"RUN-00000010": _live_run("EAWF-0043")}})
    params: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "idempotency_key": "req-cancel-0005",
        "reason_code": "superseded-by-replan",
    }

    answer = _drive(canary, tmp_path, CANCEL, **params)

    assert answer["status"] == "ok", answer["errors"]
    assert latest_rows(_ledger(canary, Epoch2Collection.TASK))["EAWF-0043"]["status"] == "CANCELLED"
    assert _control_facts(canary) == [(RUN_URN, "cancel", "requested")]
    # a retry replays the cancel's receipt and asks nothing a second time
    retry = _drive(canary, tmp_path, CANCEL, **params)
    assert retry["result"] == answer["result"]
    assert _control_facts(canary) == [(RUN_URN, "cancel", "requested")]


def test_milestone_cancel_retry_asks_the_live_run_a_lost_ask_never_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon that died between the cancel's commit and its ask leaves the retry to ask."""
    canary = _cascade_tree(tmp_path)
    seed(canary, {"run": {"RUN-00000010": _live_run("EAWF-0043")}})
    params: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "idempotency_key": "req-cancel-0007",
        "reason_code": "superseded-by-replan",
    }
    asked = domain._ask_run_cancel

    async def _lost(*_args: Any, **_kwargs: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(domain, "_ask_run_cancel", _lost)
    answer = _drive(canary, tmp_path, CANCEL, **params)
    assert answer["status"] == "ok", answer["errors"]
    assert _control_facts(canary) == []

    monkeypatch.setattr(domain, "_ask_run_cancel", asked)
    retry = _drive(canary, tmp_path, CANCEL, **params)

    assert retry["result"] == answer["result"]
    assert _control_facts(canary) == [(RUN_URN, "cancel", "requested")]


def test_milestone_cancel_asks_no_run_of_a_task_whose_run_has_ended(tmp_path: Path) -> None:
    """Boundary: a Run already compacted out of the document has nothing left to stop."""
    canary = _cascade_tree(tmp_path)

    answer = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-cancel-0006",
        reason_code="superseded-by-replan",
    )

    assert answer["status"] == "ok", answer["errors"]
    assert answer["warnings"] == []
    assert _control_facts(canary) == []


def _ended_run_line(canary: CanaryProvision) -> None:
    """File a CANCELLED Run of EAWF-0043 in the run ledger, as a compaction leaves it."""
    run = rekeyed(seed_row("run", "CANCELLED"), key="RUN-00000011")
    run["scope"]["task_ref"] = f"{_CONTAINER}/task/EAWF-0043"
    line = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key="RUN-00000011",
        status="CANCELLED",
        recorded_at=AT,
        payload=run,
    )
    path = _ledger(canary, Epoch2Collection.RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{line.model_dump_json()}\n")


@pytest.mark.parametrize(
    ("named_by", "plant"),
    [
        (
            "run/RUN-00000010",
            lambda canary: seed(canary, {"run": {"RUN-00000010": _live_run("EAWF-0043")}}),
        ),
        ("run/RUN-00000011", _ended_run_line),
        (
            "task/EAWF-0047",
            lambda canary: seed(
                canary,
                {
                    "task": {
                        "EAWF-0047": _task(
                            "DRAFT",
                            "EAWF-0047",
                            due_scope=OTHER_MILESTONE_URN,
                            depends_on=[f"{_CONTAINER}/task/EAWF-0045"],
                        )
                    }
                },
            ),
        ),
    ],
)
def test_milestone_purge_refuses_while_a_surviving_record_names_its_work(
    named_by: str, plant: Any, tmp_path: Path
) -> None:
    """A Run that worked a purged Task, or a Task depending on one, would be left dangling."""
    canary = _cascade_tree(tmp_path)
    plant(canary)
    cancelled = _drive(
        canary,
        tmp_path,
        CANCEL,
        urn=MILESTONE_URN,
        idempotency_key="req-c-0007",
        reason_code="drop",
    )
    assert cancelled["status"] == "ok", cancelled["errors"]
    before = _snapshot(canary)
    run_ledger = _ledger_bytes(canary, Epoch2Collection.RUN)

    answer = _drive(
        canary,
        tmp_path,
        MILESTONE_PURGE,
        urn=MILESTONE_URN,
        expected_revision=2,
        idempotency_key="req-p-0007",
    )

    assert answer["status"] == "error"
    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert named_by in answer["errors"][0]["message"]
    assert _snapshot(canary) == before
    assert _ledger_bytes(canary, Epoch2Collection.RUN) == run_ledger
