"""``runtime.store.redact_content`` withholds leaked lines only in rows no commit holds.

A canary's artifact ledger holds one content row that a commit already holds
and, after it, rows filed since. The verb compares the ledger with its bytes in
HEAD, withholds each flagged line in the rows after them, keeps every row's
reference and leaves the committed row as it was.
Every leak fixture is assembled from split pieces.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, WITHHELD_LINE, StoredContent
from eawf.kernel.runtime.content_redaction import (
    REDACTION_ROW_CITED,
    REDACTION_ROW_CONTENT,
    REDACTION_ROW_PUBLISHED,
    REDACTION_ROW_REVISION,
    REDACTION_ROW_SUPERSEDES,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_recovery import LEDGER_LINE_KEY
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.store_redaction import (
    REDACTION_NO_COMMIT,
    REDACTION_WAL_UNFINISHED,
    STORE_DROP_UNPUBLISHED_ROW_METHOD,
    STORE_REDACT_CONTENT_METHOD,
    LeakRemediation,
)
from eawf.runtime.daemon.wal import WalRecord, mark_applied, mark_fsynced, write_pending
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    root_context,
    seed,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
AT: Final = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
RUN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
SERVER_PATH: Final = "/" + "srv" + "/" + "checkout" + "/eawf/.ea/locks/epoch2/document.lock"


def git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _content(digest: str, *lines: str) -> LedgerRecord:
    stored = StoredContent(
        artifact_ref=f"artifact://content/run-00000010/{digest}",
        run_ref=RUN,
        lines=lines,
        total_lines=len(lines),
        withheld_lines=0,
        recorded_at=AT,
    )
    return LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=stored.artifact_ref,
        status=CONTENT_PAYLOAD_KIND,
        recorded_at=AT,
        payload=stored.model_dump(mode="json"),
    )


def _provision(repo: Path) -> CanaryProvision:
    canary = provision(repo)
    # The verb locks the tree's one project.
    seed(canary, {"project": {"EAWF": {"payload": {"code": "EAWF"}}}})
    return canary


def _ledger(canary: CanaryProvision) -> Path:
    return ledger_path(document_path(canary), Epoch2Collection.ARTIFACT)


def make_tree(tmp_path: Path) -> tuple[CanaryProvision, bytes]:
    """A canary whose committed ledger leaks once, with leaking and clean rows after it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "ci")
    git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    canary = _provision(repo)
    ledger = _ledger(canary)
    append_ledger_record(ledger, _content("aa", f"old {SERVER_PATH}"))
    git(repo, "add", "-f", str(ledger.relative_to(repo)))
    git(repo, "commit", "-q", "-m", "state: commit the first content row")
    published = ledger.read_bytes()
    append_ledger_record(ledger, _content("bb", "start", f"locked {SERVER_PATH}", "done"))
    append_ledger_record(ledger, _content("cc", "built 3 targets"))
    return canary, published


def redact(tmp_path: Path, canary: CanaryProvision, key: str) -> Any:
    """Send ``runtime.store.redact_content`` and return its answer."""
    call = {"repo_root": str(canary.root), "actor": ACTOR, "idempotency_key": key}
    return asyncio.run(
        methods.dispatch(STORE_REDACT_CONTENT_METHOD, method_context(tmp_path / "rt"), call)
    )


def drop(tmp_path: Path, canary: CanaryProvision, record_key: str, key: str) -> Any:
    """Send ``runtime.store.drop_unpublished_row`` for *record_key* and return its answer."""
    call = {
        "repo_root": str(canary.root),
        "actor": ACTOR,
        "idempotency_key": key,
        "record_key": record_key,
    }
    return asyncio.run(
        methods.dispatch(STORE_DROP_UNPUBLISHED_ROW_METHOD, method_context(tmp_path / "rt"), call)
    )


def _evidence(key: str, payload: dict[str, Any]) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key=key,
        status="recorded",
        recorded_at=AT,
        payload=payload,
    )


def test_a_leaked_line_after_the_commit_is_withheld_and_the_commit_is_kept(
    tmp_path: Path,
) -> None:
    canary, published = make_tree(tmp_path)

    answer = redact(tmp_path, canary, "redact-1")

    assert answer["rows"] == 1
    assert answer["withheld_lines"] == 1
    assert answer["unpublished_rows"] == 2
    assert answer["record_keys"] == ["artifact://content/run-00000010/bb"]
    assert answer["non_content_leak_keys"] == []
    content = _ledger(canary).read_bytes()
    assert content.startswith(published)
    records = read_ledger_records(_ledger(canary))
    stored = StoredContent.model_validate(records[1].payload)
    assert stored.lines == ("start", WITHHELD_LINE, "done")
    assert stored.withheld_lines == 1
    assert stored.artifact_ref == "artifact://content/run-00000010/bb"
    assert "checkout" not in content[len(published) :].decode("utf-8")


def test_a_second_redaction_writes_nothing_and_a_retry_replays(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    first = redact(tmp_path, canary, "redact-1")
    after_first = _ledger(canary).read_bytes()

    again = redact(tmp_path, canary, "redact-2")
    replay = redact(tmp_path, canary, "redact-1")

    assert again["rows"] == 0
    assert again["record_keys"] == []
    assert _ledger(canary).read_bytes() == after_first
    assert replay == {**first, "replayed": True}


def test_a_leaking_non_content_row_stays_and_its_key_is_answered(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    other = _evidence("EVD-0001", {"summary": f"wrote {SERVER_PATH}"})
    append_ledger_record(_ledger(canary), other)

    answer = redact(tmp_path, canary, "redact-1")

    assert answer["record_keys"] == ["artifact://content/run-00000010/bb"]
    assert answer["non_content_leak_keys"] == ["EVD-0001"]
    records = read_ledger_records(_ledger(canary))
    assert records[-1] == other
    stored = StoredContent.model_validate(records[1].payload)
    assert stored.lines == ("start", WITHHELD_LINE, "done")


def test_a_leaking_non_content_row_has_a_way_out(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    append_ledger_record(
        _ledger(canary),
        _evidence("EVD-0001", {"payload_kind": "spike_report", "summary": f"at {SERVER_PATH}"}),
    )
    append_ledger_record(_ledger(canary), _evidence("EVD-0002", {"summary": "clean"}))

    answer = redact(tmp_path, canary, "redact-1")
    [remediation] = answer["remediations"]
    assert remediation["record_key"] == "EVD-0001"
    assert remediation["payload_kind"] == "spike_report"
    assert remediation["command"].startswith("eawf store drop-unpublished-row EVD-0001 ")

    dropped = drop(tmp_path, canary, "EVD-0001", "drop-1")
    again = redact(tmp_path, canary, "redact-2")

    assert dropped["record_key"] == "EVD-0001"
    assert dropped["payload_kind"] == "spike_report"
    assert again["non_content_leak_keys"] == []
    assert again["remediations"] == []
    keys = [record.record_key for record in read_ledger_records(_ledger(canary))]
    assert "EVD-0001" not in keys
    assert "EVD-0002" in keys
    assert "checkout" not in _ledger(canary).read_text(encoding="utf-8").split("\n", 1)[1]
    assert drop(tmp_path, canary, "EVD-0001", "drop-1") == {**dropped, "replayed": True}


def test_a_remediation_names_the_drop_only_for_a_droppable_row(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    ledger = _ledger(canary)
    base = _evidence("EVD-0002", {"summary": "clean"})
    append_ledger_record(ledger, base)
    revision = {"payload_kind": "artifact_revision", "text": f"at {SERVER_PATH}"}
    append_ledger_record(ledger, _evidence("ART-0009#r1", revision))
    superseding = LedgerRecord(
        collection=Epoch2Collection.ARTIFACT,
        record_key="EVD-0003",
        status="recorded",
        recorded_at=AT,
        payload={"summary": f"moved {SERVER_PATH}"},
        supersedes=line_digest(render_ledger_line(base)),
    )
    append_ledger_record(ledger, superseding)
    append_ledger_record(ledger, _evidence("EVD-0004", {"summary": f"wrote {SERVER_PATH}"}))

    answer = redact(tmp_path, canary, "redact-1")
    by_key = {item["record_key"]: item for item in answer["remediations"]}

    assert answer["non_content_leak_keys"] == ["ART-0009#r1", "EVD-0003", "EVD-0004"]
    assert by_key["ART-0009#r1"]["command"] is None
    assert by_key["ART-0009#r1"]["refusal"] == REDACTION_ROW_REVISION
    assert by_key["EVD-0003"]["command"] is None
    assert by_key["EVD-0003"]["refusal"] == REDACTION_ROW_SUPERSEDES
    assert by_key["EVD-0004"]["command"].startswith("eawf store drop-unpublished-row EVD-0004 ")
    assert by_key["EVD-0004"]["refusal"] is None


def test_a_superseded_leaking_row_gets_the_drop_refusal_not_the_command(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    ledger = _ledger(canary)
    leaking = _evidence("EVD-0005", {"summary": f"wrote {SERVER_PATH}"})
    append_ledger_record(ledger, leaking)
    append_ledger_record(
        ledger,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key="EVD-0006",
            status="recorded",
            recorded_at=AT,
            payload={"summary": "corrected"},
            supersedes=line_digest(render_ledger_line(leaking)),
        ),
    )

    answer = redact(tmp_path, canary, "redact-1")
    [remediation] = answer["remediations"]

    assert remediation["record_key"] == "EVD-0005"
    assert remediation["command"] is None
    assert remediation["refusal"] == REDACTION_ROW_CITED
    with pytest.raises(ValueError, match="exactly one"):
        LeakRemediation(record_key="EVD-0005", payload_kind=None, command=None, refusal=None)
    with pytest.raises(ValueError, match="exactly one"):
        LeakRemediation(record_key="EVD-0005", payload_kind=None, command="x", refusal="y")


def test_dropping_a_committed_row_is_refused(tmp_path: Path) -> None:
    canary, published = make_tree(tmp_path)

    with pytest.raises(DaemonValidationError, match=REDACTION_ROW_PUBLISHED):
        drop(tmp_path, canary, "artifact://content/run-00000010/aa", "drop-1")
    assert _ledger(canary).read_bytes().startswith(published)


def test_dropping_a_content_row_is_refused_and_points_at_redaction(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    before = _ledger(canary).read_bytes()

    with pytest.raises(DaemonValidationError, match=REDACTION_ROW_CONTENT) as refused:
        drop(tmp_path, canary, "artifact://content/run-00000010/bb", "drop-1")
    assert "redact-content" in str(refused.value)
    assert _ledger(canary).read_bytes() == before


def test_dropping_a_row_another_row_cites_is_refused(tmp_path: Path) -> None:
    canary, _ = make_tree(tmp_path)
    append_ledger_record(_ledger(canary), _evidence("EVD-0001", {"summary": f"at {SERVER_PATH}"}))
    append_ledger_record(_ledger(canary), _evidence("EVD-0002", {"cites": "EVD-0001"}))
    before = _ledger(canary).read_bytes()

    with pytest.raises(DaemonValidationError, match=REDACTION_ROW_CITED):
        drop(tmp_path, canary, "EVD-0001", "drop-1")
    assert _ledger(canary).read_bytes() == before


def test_dropping_while_the_wal_holds_an_unfinished_artifact_line_is_refused(
    tmp_path: Path,
) -> None:
    canary, _ = make_tree(tmp_path)
    leaked = _evidence("EVD-0001", {"summary": f"at {SERVER_PATH}"})
    append_ledger_record(_ledger(canary), leaked)
    _journal_applied_line(tmp_path, canary, leaked)
    before = _ledger(canary).read_bytes()

    with pytest.raises(DaemonValidationError, match=REDACTION_WAL_UNFINISHED):
        drop(tmp_path, canary, "EVD-0001", "drop-1")
    assert _ledger(canary).read_bytes() == before


def test_a_repository_without_a_commit_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    canary = _provision(repo)
    ledger = _ledger(canary)
    append_ledger_record(ledger, _content("dd", f"at {SERVER_PATH}"))
    before = ledger.read_bytes()

    with pytest.raises(DaemonValidationError, match=REDACTION_NO_COMMIT):
        redact(tmp_path, canary, "redact-1")
    assert ledger.read_bytes() == before


def _journal_applied_line(tmp_path: Path, canary: CanaryProvision, record: LedgerRecord) -> str:
    """Leave *record*'s append applied in the WAL, as a failed firehose write does."""
    envelope = Envelope(
        id="evt-0001",
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=AT,
        summary="ledger.artifact.appended",
        payload={LEDGER_LINE_KEY: render_ledger_line(record)},
    )
    wal_record = WalRecord(
        record_id="wal0001",
        envelope=envelope,
        written_at=AT,
        before_state_version="before",
        after_state_version="after",
        state_path=str(document_path(canary)),
    )
    wal_dir = root_context(canary, tmp_path / "rt").wal_dir
    write_pending(wal_dir, wal_record)
    mark_applied(wal_dir, wal_record.record_id)
    return wal_record.record_id


def test_redaction_refuses_while_the_wal_holds_an_unfinished_artifact_line(
    tmp_path: Path,
) -> None:
    canary, _ = make_tree(tmp_path)
    leaked = _content("bb", "start", f"locked {SERVER_PATH}", "done")
    record_id = _journal_applied_line(tmp_path, canary, leaked)
    before = _ledger(canary).read_bytes()

    with pytest.raises(DaemonValidationError, match=REDACTION_WAL_UNFINISHED) as refused:
        redact(tmp_path, canary, "redact-1")
    assert record_id in str(refused.value)
    assert _ledger(canary).read_bytes() == before

    mark_fsynced(root_context(canary, tmp_path / "rt").wal_dir, record_id)
    answer = redact(tmp_path, canary, "redact-2")
    assert answer["rows"] == 1
    assert answer["record_keys"] == ["artifact://content/run-00000010/bb"]
