"""The read verbs answer from the selected generation of a tree with no v1 document.

A tree ``eawf init`` bears holds no ``.ea/state.json``. Metrics, provenance,
doc verification, memory and doctor each used to open that document, and so
refused a born tree; they now read the generation, so each exits 0 there and
none of them touches the path the document would sit at.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.cutover import HISTORY_RECORD_STATUS
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.native_records import NativeRecordCollection, map_native_record
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, ratify
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

DECISION_KEY: Final = "D01"
DECISION_SCOPE: Final = "eawf://WSP-MAIN/PRJ-DEMO/REP-DEMO/milestone/MLS-0001"

#: Each ported read verb, as typed after ``eawf --json``.
PORTED_READERS: Final = (
    ("metrics",),
    ("why", DECISION_KEY),
    ("doc", "verify"),
    ("memory", "list"),
    ("doctor",),
)


def _born_tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bear a tree in ``repo`` with ``eawf init``, resolve from there, return its ``.ea``."""
    result = runner.invoke(
        app, ["--json", "--no-input", "init", "--target", str(repo), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(repo)
    return repo / ".ea"


def _file_decision(tree: Path) -> None:
    """Append one ratified native decision to the generation's decision ledger."""
    authority = resolve_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    proposed = Decision.model_validate(
        {
            "key": DECISION_KEY,
            "scope_ref": DECISION_SCOPE,
            "title": "Read the generation",
            "decision": "Read the generation.",
            "rationale": "A born tree has no other record.",
            "alternatives": [{"key": "read", "label": "Read it"}, {"key": "skip", "label": "Skip"}],
            "chosen_option_key": "read",
            "consequences": ["The read verbs answer on a born tree"],
            "evidence_refs": ["eawf://WSP-MAIN/PRJ-DEMO/REP-DEMO/evidence/EVD-0001"],
            "effective_policy_revision": 1,
            "created_at": "2026-10-01T00:00:00Z",
        }
    )
    decision = ratify(proposed, by="OP-0001", at=proposed.created_at)
    append_ledger_record(
        ledger_path(document, Epoch2Collection.DECISION),
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key=decision.key,
            status=decision.status.value,
            recorded_at=decision.created_at,
            supersedes=None,
            payload={"payload": decision.model_dump(mode="json")},
        ),
    )


def test_ported_readers_run_without_v1_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _born_tree(tmp_path, monkeypatch)
    _file_decision(tree)
    v1_document = (tree / "state.json").resolve()
    assert not v1_document.exists()
    touched: list[str] = []

    def _recording(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == v1_document:
                touched.append(name)
            return original(self, *args, **kwargs)

        return _wrapper

    for name in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, name, _recording(name, getattr(Path, name)))

    for argv in PORTED_READERS:
        result = runner.invoke(app, ["--json", *argv])
        assert result.exit_code == exit_codes.OK, (argv, result.output)
        json.loads(result.stdout)
        assert touched == [], argv

    why = json.loads(runner.invoke(app, ["--json", "why", DECISION_KEY]).stdout)
    assert (why["kind"], why["id"], why["title"]) == (
        "decision",
        DECISION_KEY,
        "Read the generation",
    )


def test_why_names_a_key_the_generation_does_not_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _born_tree(tmp_path, monkeypatch)
    result = runner.invoke(app, ["--json", "why", "D99"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "D99" in result.stdout


def _file_imported_decision(tree: Path, key: str) -> None:
    """Append one epoch-1 decision to the decision ledger the way the cutover imports it."""
    authority = resolve_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    record = map_native_record(
        collection=NativeRecordCollection.DECISIONS,
        record_key=key,
        row={
            "id": key,
            "scope_id": "P01-I01-W01",
            "title": "Keep the frozen decisions explainable",
            "rationale": "A migrated tree still cites them.",
            "status": "superseded",
            "superseded_by": "D08",
            "created_at": "2026-06-01T00:00:00Z",
        },
        project_code="DEMO",
        source_schema_version="1.20",
    )
    append_ledger_record(
        ledger_path(document, Epoch2Collection.DECISION),
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key=key,
            status=HISTORY_RECORD_STATUS,
            recorded_at=datetime(2026, 9, 26, tzinfo=UTC),
            payload=record.model_dump(mode="json"),
        ),
    )


def test_why_explains_a_decision_imported_from_epoch_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _born_tree(tmp_path, monkeypatch)
    _file_imported_decision(tree, "D07")
    result = runner.invoke(app, ["--json", "why", "D07"])
    assert result.exit_code == exit_codes.OK, result.output
    why = json.loads(result.stdout)
    assert (why["id"], why["title"], why["status"]) == (
        "D07",
        "Keep the frozen decisions explainable",
        "superseded",
    )
    assert why["summary"] == (
        "Keep the frozen decisions explainable Rationale: A migrated tree still cites them."
    )
    assert why["refs"] == [
        {"ref": "P01-I01-W01", "kind": "scope"},
        {"ref": "D08", "kind": "superseded_by"},
    ]
