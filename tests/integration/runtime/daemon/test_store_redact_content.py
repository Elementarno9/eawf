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
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.store_redaction import (
    REDACTION_NO_COMMIT,
    STORE_REDACT_CONTENT_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
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


def test_a_leaked_line_after_the_commit_is_withheld_and_the_commit_is_kept(
    tmp_path: Path,
) -> None:
    canary, published = make_tree(tmp_path)

    answer = redact(tmp_path, canary, "redact-1")

    assert answer["rows"] == 1
    assert answer["withheld_lines"] == 1
    assert answer["unpublished_rows"] == 2
    assert answer["record_keys"] == ["artifact://content/run-00000010/bb"]
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
