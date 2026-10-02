"""The console record reads: what each answers for an empty tree, a hit and a miss."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import ConformanceStageRecord
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.conformance import StoreStageJournal
from eawf.runtime.daemon.methods.console_records import (
    MISSING_ENDING,
    REPOSITORY_READ_METHOD,
    BatchRecordsRead,
    RepositoryRead,
    TargetResolve,
    read_conflict_frames,
    read_health_verdicts,
    read_proof_receipts,
    resolve_target,
)
from eawf.runtime.daemon.methods.delivery import CLEARED_STATUS, CONFLICT_STATUS
from eawf.runtime.vcs import repository_read
from eawf.runtime.vcs.repository_read import NOT_A_REPOSITORY, RepositoryAnswer
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
BATCH: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"


@pytest.fixture
def document(tmp_path: Path) -> Path:
    """Return the document of a canary holding one Batch and nothing on any ledger."""
    canary = provision(tmp_path / "repo")
    seed(canary, {Epoch2Collection.BATCH.value: {"BAT-0007": seed_row("batch", "ACTIVE")}})
    return document_path(canary)


def test_a_key_the_document_holds_resolves(document: Path) -> None:
    assert resolve_target(document, "BAT-0007").ending is None


def test_a_key_nothing_holds_is_missing(document: Path) -> None:
    assert resolve_target(document, "BAT-0999").ending == MISSING_ENDING


def test_a_key_only_a_ledger_line_names_is_not_missing(document: Path) -> None:
    append_ledger_record(
        ledger_path(document, Epoch2Collection.RECEIPT),
        LedgerRecord(
            collection=Epoch2Collection.RECEIPT,
            record_key="PRF-0000000000000000-EAWF-0001",
            status="pass",
            recorded_at=AT,
            payload={"receipt": {"id": "RCP-0042"}},
        ),
    )
    assert resolve_target(document, "RCP-0042").ending is None
    assert resolve_target(document, "RCP-0043").ending == MISSING_ENDING


def test_a_tree_with_no_ledger_yet_reads_empty_runs(document: Path) -> None:
    batch = BatchRecordsRead.model_validate({"urn": BATCH}).urn
    assert read_conflict_frames(ledger_path(document, Epoch2Collection.BATCH), batch) == ()
    assert read_proof_receipts(ledger_path(document, Epoch2Collection.RECEIPT), "RCP-1") == ()


def test_a_tree_that_never_certified_holds_no_verdict(document: Path) -> None:
    tree = next(parent for parent in document.parents if parent.name == ".ea")
    assert read_health_verdicts(tree).verdicts == ()


def test_a_verdict_states_when_its_newest_stage_completed(document: Path) -> None:
    """The health route's LAST RESULT is the newest stage's completion, as the runner wrote it."""
    tree = next(parent for parent in document.parents if parent.name == ".ea")
    journal = StoreStageJournal(tree / "state.json")
    for stage, done in (("probe", AT), ("canary", AT + timedelta(minutes=7))):
        journal.append(
            tuple_digest=f"sha256:{'a' * 64}",
            record=ConformanceStageRecord(
                stage=stage,
                outcome="passed",
                evidence_ref="artifact://conformance/probe",
                started_at=AT,
                completed_at=done,
            ),
        )
    (verdict,) = read_health_verdicts(tree).verdicts
    assert verdict.checked_at == AT + timedelta(minutes=7)


def _conflict_line(*, status: str, cleared: bool) -> LedgerRecord:
    payload: dict[str, object] = {
        "id": "INC-000001",
        "attempt_id": "INA-000001",
        "batch_ref": BATCH,
        "repository_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF",
        "branch": "eawf/delivery/BAT-0007",
        "ahead": 1,
        "behind": 1,
        "files": [
            {
                "path": "src/a.py",
                "hunks": [
                    {
                        "index": 1,
                        "ours": {
                            "authority": {"kind": "agent", "batch_ref": BATCH},
                            "at": AT.isoformat(),
                            "sha": "a" * 40,
                            "lines": ["x = 1"],
                        },
                        "theirs": {
                            "authority": {"kind": "principal", "principal_key": "OP-0001"},
                            "at": AT.isoformat(),
                            "sha": "b" * 40,
                            "lines": ["x = 2"],
                        },
                    }
                ],
            }
        ],
        "exit": {"kind": "repair_task", "ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0050"},
    }
    if cleared:
        payload["cleared_at"] = AT.isoformat()
        payload["cleared_by"] = {
            "generation_id": "ING-000003",
            "head_sha": "c" * 40,
            "actor": "OP-0001",
        }
    return LedgerRecord(
        collection=Epoch2Collection.BATCH,
        record_key="INC-000001-BAT-0007",
        status=status,
        recorded_at=AT,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("lines", "cleared"),
    [
        ([(CONFLICT_STATUS, False)], False),
        ([(CONFLICT_STATUS, False), (CLEARED_STATUS, True)], True),
        ([(CONFLICT_STATUS, False), (CLEARED_STATUS, True), (CONFLICT_STATUS, False)], False),
    ],
    ids=["standing", "cleared", "blocked-again"],
)
def test_a_conflict_reads_as_its_newest_line(
    document: Path, lines: list[tuple[str, bool]], cleared: bool
) -> None:
    """One frame per conflict, standing or cleared as its newest line says."""
    path = ledger_path(document, Epoch2Collection.BATCH)
    for status, is_cleared in lines:
        append_ledger_record(path, _conflict_line(status=status, cleared=is_cleared))
    batch = BatchRecordsRead.model_validate({"urn": BATCH}).urn
    (frame,) = read_conflict_frames(path, batch)
    assert (frame.cleared_at is not None) is cleared


@pytest.mark.parametrize("key", ["", "bat-0007", "BAT", "BAT-0007 ", 7])
def test_a_key_outside_the_entity_grammar_is_refused(key: object) -> None:
    with pytest.raises(ValidationError):
        TargetResolve.model_validate({"key": key})


def test_an_unknown_parameter_is_refused() -> None:
    with pytest.raises(ValidationError):
        TargetResolve.model_validate({"key": "BAT-0007", "route": "git.pr"})


def test_the_repository_verb_reads_the_working_tree_the_ea_tree_sits_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon reads the tree's own checkout; a canary outside git says so, not fails."""
    canary = provision(tmp_path / "repo")
    asked: list[Path] = []

    def read(root: Path, branch: str | None = None) -> RepositoryAnswer:
        asked.append(root)
        return repository_read.read_repository(root, branch)

    monkeypatch.setattr("eawf.runtime.daemon.methods.console_records.read_repository", read)
    answer = RepositoryAnswer.model_validate(
        asyncio.run(
            methods.dispatch(
                REPOSITORY_READ_METHOD,
                method_context(tmp_path / "runtime"),
                {"repo_root": str(canary.root)},
            )
        )
    )
    assert asked == [canary.root]
    assert answer.branch_unread == NOT_A_REPOSITORY


def test_the_repository_verb_takes_a_branch_and_refuses_any_other_parameter() -> None:
    assert RepositoryRead.model_validate({"branch": "main"}).branch == "main"
    assert RepositoryRead.model_validate({}).branch is None
    with pytest.raises(ValidationError):
        RepositoryRead.model_validate({"remote": "origin"})
    with pytest.raises(ValidationError):
        RepositoryRead.model_validate({"branch": ""})


def test_the_repository_verb_looks_the_pull_request_up_for_the_branch_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = provision(tmp_path / "repo")
    asked: list[str | None] = []

    def read(root: Path, branch: str | None = None) -> RepositoryAnswer:
        asked.append(branch)
        return RepositoryAnswer(pull_request_branch=branch)

    monkeypatch.setattr("eawf.runtime.daemon.methods.console_records.read_repository", read)
    asyncio.run(
        methods.dispatch(
            REPOSITORY_READ_METHOD,
            method_context(tmp_path / "runtime"),
            {"repo_root": str(canary.root), "branch": "feature/x"},
        )
    )
    assert asked == ["feature/x"]
