"""Kernel review fixes: re-key ordering, URN spelling and fail-closed reads."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from eawf.kernel.identity import EntityKind, QualifiedUrn, parse_qualified_urn
from eawf.kernel.identity.errors import IdentityError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.urn_rekey import plan_urn_rekey, spells_single_key
from eawf.kernel.state.epoch2.pending_action import (
    OptionEffect,
    PendingAction,
    PendingActionStatus,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerSnapshotError,
    latest_rows,
    ledger_snapshot,
    line_digest,
    render_ledger_line,
    seal_ledger_prefix,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.campaign_scheduler import _slot
from eawf.runtime.daemon.methods.campaign import _sibling
from eawf.runtime.daemon.methods.doctor import project_lock_urn
from eawf.runtime.daemon.methods.evidence_ladder import _address
from eawf.runtime.daemon.methods.host_question import _question_urn
from eawf.runtime.daemon.methods.memory import project_subject
from eawf.runtime.daemon.methods.permission import _permission_urn
from eawf.runtime.daemon.methods.task_dispatch import next_run_urn
from eawf.runtime.daemon.pause_migration import _workspace as _pause_workspace
from eawf.runtime.daemon.record_append import _project_urn as _record_append_project
from eawf.workflow.planning.apply import _approval_spent_by, _build_citation_resolvers
from eawf.workflow.planning.revision import approval_action_refusal

OLD = "eawf://EAWF/EAWF/EAWF"
NEW = "eawf://EAWF"
WRITTEN_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
REKEYED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TASK = Epoch2Collection.TASK


def _record(key: str, payload: dict[str, Any]) -> LedgerRecord:
    return LedgerRecord(
        collection=TASK, record_key=key, status="open", recorded_at=WRITTEN_AT, payload=payload
    )


def _generation(tmp_path: Path, records: list[LedgerRecord]) -> Path:
    state_path = tmp_path / "gen-x" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}")
    path = ledger_path(state_path, TASK)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(f"{render_ledger_line(r)}\n" for r in records).encode())
    return state_path


def _apply(state_path: Path) -> None:
    plan = plan_urn_rekey(state_path, recorded_at=REKEYED_AT)
    for path, appended in plan.ledger_appends.items():
        path.write_bytes((path.read_bytes() if path.exists() else b"") + appended)


# ---- the re-key keeps each key's standing line last ---------------------------------


def test_rekey_does_not_resurrect_an_earlier_line(tmp_path: Path) -> None:
    """The reproduced defect: v1 holds an old URN, v2 none; v2 must stay latest."""
    v1 = {"key": "EAWF-0001", "batch_ref": f"{OLD}/batch/BAT-0001", "title": "v1"}
    v2 = {"key": "EAWF-0001", "batch_ref": f"{NEW}/batch/BAT-0001", "title": "v2"}
    state_path = _generation(tmp_path, [_record("EAWF-0001", v1), _record("EAWF-0001", v2)])

    assert plan_urn_rekey(state_path, recorded_at=REKEYED_AT).rows == ()
    _apply(state_path)
    assert latest_rows(ledger_path(state_path, TASK))["EAWF-0001"]["title"] == "v2"


def test_rekey_supersedes_only_the_last_line_of_a_key(tmp_path: Path) -> None:
    v1 = {"key": "EAWF-0001", "batch_ref": f"{NEW}/batch/BAT-0001", "title": "v1"}
    v2 = {"key": "EAWF-0001", "batch_ref": f"{OLD}/batch/BAT-0002", "title": "v2"}
    state_path = _generation(tmp_path, [_record("EAWF-0001", v1), _record("EAWF-0001", v2)])
    lines = ledger_path(state_path, TASK).read_text().splitlines()

    plan = plan_urn_rekey(state_path, recorded_at=REKEYED_AT)

    assert [row.record.supersedes for row in plan.rows] == [line_digest(lines[1])]
    _apply(state_path)
    latest = latest_rows(ledger_path(state_path, TASK))["EAWF-0001"]
    assert latest == {**v2, "batch_ref": f"{NEW}/batch/BAT-0002"}
    assert plan_urn_rekey(state_path, recorded_at=REKEYED_AT).rows == ()


def test_rekey_of_an_empty_ledger_plans_nothing(tmp_path: Path) -> None:
    assert plan_urn_rekey(_generation(tmp_path, []), recorded_at=REKEYED_AT).rows == ()


def test_rekey_reads_the_sealed_snapshot(tmp_path: Path) -> None:
    sealed = {"key": "EAWF-0001", "batch_ref": f"{OLD}/batch/BAT-0001"}
    live = {"key": "EAWF-0002", "title": "no urn"}
    state_path = _generation(tmp_path, [_record("EAWF-0001", sealed), _record("EAWF-0002", live)])
    path = ledger_path(state_path, TASK)
    first = path.read_bytes().split(b"\n", 1)[0] + b"\n"
    seal_ledger_prefix(path, len(first))

    plan = plan_urn_rekey(state_path, recorded_at=REKEYED_AT)

    assert [row.record.record_key for row in plan.rows] == ["EAWF-0001"]
    assert plan.rows[0].record.supersedes == line_digest(first.decode().rstrip("\n"))
    _apply(state_path)
    assert latest_rows(path)["EAWF-0001"]["batch_ref"] == f"{NEW}/batch/BAT-0001"


def test_rekey_refuses_a_snapshot_that_fails_its_digest(tmp_path: Path) -> None:
    row = {"key": "EAWF-0001", "batch_ref": f"{OLD}/batch/BAT-0001"}
    state_path = _generation(tmp_path, [_record("EAWF-0001", row), _record("EAWF-0002", row)])
    path = ledger_path(state_path, TASK)
    seal_ledger_prefix(path, len(path.read_bytes().split(b"\n", 1)[0]) + 1)
    snapshot = ledger_snapshot(path)
    assert snapshot is not None
    snapshot.write_bytes(snapshot.read_bytes().replace(b"BAT-0001", b"BAT-0009"))

    with pytest.raises(LedgerSnapshotError):
        plan_urn_rekey(state_path, recorded_at=REKEYED_AT)


# ---- an address compares by record, not by spelling ---------------------------------


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        (f"{OLD}/pending-action/ACT-0001", f"{NEW}/pending-action/ACT-0001", True),
        (f"{NEW}/pending-action/ACT-0001", f"{NEW}/pending-action/ACT-0001", True),
        (f"{OLD}/pending-action/ACT-0001", f"{NEW}/pending-action/ACT-0002", False),
        (f"{NEW}/claim/CLM-0001#rung-2", "eawf://EAWF/EAWF/_/claim/CLM-0001", True),
        (f"{NEW}/milestone/MLS-0001", f"{NEW}/batch/BAT-0001", False),
        ("eawf://ABC/ABC/ABC/milestone/MLS-0001", f"{OLD}/milestone/MLS-0001", False),
    ],
)
def test_same_entity_compares_the_address(left: str, right: str, same: bool) -> None:
    assert parse_qualified_urn(left).same_entity(parse_qualified_urn(right)) is same
    assert parse_qualified_urn(right).same_entity(parse_qualified_urn(left)) is same


def test_same_entity_refuses_a_string() -> None:
    with pytest.raises(AttributeError):
        parse_qualified_urn(f"{NEW}/task/EAWF-0001").same_entity(f"{NEW}/task/EAWF-0001")  # type: ignore[arg-type]


def _spending(action_ref: str) -> dict[str, Any]:
    return {"plan_revision": {"PLR-0001": {"approval": {"action_ref": action_ref}}}}


@pytest.mark.parametrize(
    "stored", [f"{OLD}/pending-action/ACT-0001", f"{NEW}/pending-action/ACT-0001"]
)
def test_an_answer_spent_under_either_spelling_is_spent(stored: str) -> None:
    for cited in (f"{OLD}/pending-action/ACT-0001", f"{NEW}/pending-action/ACT-0001"):
        assert _approval_spent_by(_spending(stored), parse_qualified_urn(cited)) == "PLR-0001"


def test_an_answer_about_another_action_is_not_spent() -> None:
    cited = parse_qualified_urn(f"{NEW}/pending-action/ACT-0002")
    assert _approval_spent_by(_spending(f"{OLD}/pending-action/ACT-0001"), cited) is None
    assert _approval_spent_by({}, cited) is None


def test_a_stored_approval_with_a_broken_ref_fails_loudly() -> None:
    with pytest.raises(IdentityError):
        _approval_spent_by(
            _spending("not-a-urn"), parse_qualified_urn(f"{NEW}/pending-action/ACT-0001")
        )


def _consent(subject: str, milestone: str) -> Any:
    revision = SimpleNamespace(
        key="PLR-0002",
        revision=2,
        body=SimpleNamespace(milestone_urn=parse_qualified_urn(milestone)),
    )
    action = SimpleNamespace(
        status=PendingActionStatus.SEALED,
        selected_option=SimpleNamespace(effect=OptionEffect.APPROVE, option_id="approve"),
        subject_ref=parse_qualified_urn(subject),
    )
    return approval_action_refusal(
        cast(Any, revision), action_key="ACT-0001", action=cast(Any, action), spent_by=None
    )


def test_an_answer_about_the_milestone_in_the_other_spelling_consents() -> None:
    assert _consent(f"{NEW}/milestone/MLS-0001", f"{OLD}/milestone/MLS-0001") is None
    assert _consent(f"{OLD}/milestone/MLS-0001", f"{NEW}/milestone/MLS-0001") is None


def test_an_answer_about_another_milestone_still_refuses() -> None:
    refusal = _consent(f"{NEW}/milestone/MLS-0002", f"{OLD}/milestone/MLS-0001")
    assert refusal is not None
    assert refusal.guard == "approval_action_subject"


# ---- a minted URN takes the spelling of the record it is minted beside -------------


ANCHORS = (f"{OLD}/task/EAWF-0001", f"{NEW}/task/EAWF-0001")


def _form(urn: QualifiedUrn | str) -> bool:
    return (parse_qualified_urn(urn) if isinstance(urn, str) else urn).single_key


@pytest.mark.parametrize("anchor", ANCHORS)
def test_every_beside_minter_keeps_the_anchor_spelling(anchor: str) -> None:
    task = parse_qualified_urn(anchor)
    run = parse_qualified_urn(anchor.replace("task/EAWF-0001", "run/RUN-00000001"))
    minted: list[QualifiedUrn | str] = [
        _sibling(task, EntityKind.CAMPAIGN, "CAM-0001"),
        _slot(task, EntityKind.CAMPAIGN, "CAM-0001", repository=True),
        _slot(task, EntityKind.QUESTION, "QST-0001", repository=False),
        _permission_urn(run, "PERM-0001"),
        _question_urn(run, "QST-0001"),
        _address(task, EntityKind.CLAIM, "CLM-0001"),
        _address(task, EntityKind.EVIDENCE, "EVD-0001"),
    ]
    assert [_form(urn) for urn in minted] == [task.single_key] * len(minted)


@pytest.mark.parametrize("anchor", ANCHORS)
def test_a_pending_action_urn_backfills_in_its_subject_spelling(anchor: str) -> None:
    data = cast(Any, PendingAction)._urn_defaults_from_subject(
        {"id": "ACT-0001", "subject_ref": anchor}
    )
    assert _form(data["urn"]) is parse_qualified_urn(anchor).single_key


@pytest.mark.parametrize("anchor", ANCHORS)
def test_the_next_run_urn_keeps_the_task_spelling(tmp_path: Path, anchor: str) -> None:
    run_ledger = tmp_path / "run.jsonl"
    held = LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key="RUN-00000007",
        status="open",
        recorded_at=WRITTEN_AT,
        payload={},
    )
    run_ledger.write_text(f"{render_ledger_line(held)}\n")
    session = SimpleNamespace(read_document=dict, ledger_path=lambda _collection: run_ledger)

    @contextmanager
    def _session(_urns: object) -> Iterator[object]:
        yield session

    task = SimpleNamespace(urn=parse_qualified_urn(anchor))
    urn = next_run_urn(cast(Any, SimpleNamespace(session=_session)), cast(Any, task))

    assert urn.entity_key == "RUN-00000008"
    assert urn.single_key is task.urn.single_key


# ---- an unreadable snapshot fails the citation check closed -------------------------


def test_a_decision_snapshot_failing_its_digest_refuses_every_citation(tmp_path: Path) -> None:
    decision = tmp_path / "decision.jsonl"
    line = render_ledger_line(
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key="D-0001",
            status="open",
            recorded_at=WRITTEN_AT,
            payload={"payload": {"status": "accepted", "superseded_by": None}},
        )
    )
    decision.write_text(f"{line}\n{line}\n")
    seal_ledger_prefix(decision, len(line) + 1)
    snapshot = ledger_snapshot(decision)
    assert snapshot is not None
    snapshot.write_text(f"{line} \n")
    session = SimpleNamespace(ledger_path=lambda collection: tmp_path / f"{collection.value}.jsonl")

    resolvers = _build_citation_resolvers(cast(Any, session), {}, provider_tuple=())

    assert resolvers.decision_is_resolvable("D-0001") is False
    assert resolvers.contract_is_resolvable("anything") is False


# ---- a URN minted with no record beside it takes the tree's form --------------------


def _tree_document(repository_urn: str | None) -> dict[str, Any]:
    document: dict[str, Any] = {"project": {"EAWF": {"status": "active"}}}
    if repository_urn is not None:
        document["repository"] = {"EAWF": {"key": "EAWF", "urn": repository_urn}}
    return document


@pytest.mark.parametrize(
    ("repository_urn", "single"),
    [(f"{OLD}/repository/EAWF", False), (f"{NEW}/repository/EAWF", True), (None, False)],
)
def test_the_repository_row_witnesses_the_tree_form(
    repository_urn: str | None, single: bool
) -> None:
    assert spells_single_key(_tree_document(repository_urn)) is single


def test_the_tree_form_of_an_empty_document_is_three_slot() -> None:
    assert spells_single_key({}) is False


def test_the_tree_form_refuses_a_repository_row_without_a_urn() -> None:
    with pytest.raises(KeyError):
        spells_single_key({"repository": {"EAWF": {"key": "EAWF"}}})


def test_the_tree_form_refuses_a_malformed_repository_urn() -> None:
    with pytest.raises(IdentityError):
        spells_single_key(_tree_document("eawf://not/a/urn"))


def _context(tmp_path: Path, document: dict[str, Any]) -> Any:
    (tmp_path / GENERATION_DOCUMENT).write_text(json.dumps(document))
    authority = SimpleNamespace(
        target=SimpleNamespace(generation_path=lambda _generation: tmp_path),
        generation_id="gen-x",
    )
    return SimpleNamespace(
        require_selected_generation=lambda: authority,
        identity=SimpleNamespace(tree_root=tmp_path),
    )


@pytest.mark.parametrize(
    ("repository_urn", "single"),
    [(f"{OLD}/repository/EAWF", False), (f"{NEW}/repository/EAWF", True)],
)
def test_every_tree_minter_takes_the_tree_form(
    tmp_path: Path, repository_urn: str, single: bool
) -> None:
    document = _tree_document(repository_urn)
    context = _context(tmp_path, document)
    minted = [
        _record_append_project(context),
        project_subject(context),
        project_lock_urn(context),
    ]
    workspace = _pause_workspace(document)
    assert workspace is not None
    assert [_form(urn) for urn in minted] == [single] * 3
    assert workspace.single_key is single
    assert {parse_qualified_urn(urn).entity_key for urn in minted} == {"EAWF"}
