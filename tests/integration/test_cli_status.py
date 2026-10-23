"""Integration tests for ``eawf status`` driving the CLI via :class:`CliRunner`.

A tree is born at epoch 2 under ``tmp_path/.ea/`` and ``EA_STATE`` is pointed
at its ``state.json`` slot. The git invocations are stubbed out via
monkeypatching of :func:`subprocess.run` so the test never depends on a real
worktree.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, ratify, supersede
from eawf.kernel.state.epoch2.task import Task, TaskPriority, TaskStatus
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_ledger_record,
    line_digest,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()

_PAYLOAD_KEYS = {"git", "authority", "native", "recent_decisions", "blockers"}


def _stub_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every git subprocess invocation to fail with FileNotFoundError."""

    def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git stubbed away")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _document_path(state_path: Path) -> Path:
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _invoke(state_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> Any:
    monkeypatch.setenv("EA_STATE", str(state_path))
    _stub_no_git(monkeypatch)
    return runner.invoke(app, list(argv))


def _status_json(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    result = _invoke(state_path, monkeypatch, "--json", "status")
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_status_json_envelope_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    payload = _status_json(state_path, monkeypatch)
    assert set(payload) == _PAYLOAD_KEYS
    assert payload["git"] == {"head": None, "branch": None, "dirty": None}
    assert payload["authority"]["epoch"] == 2
    assert (
        payload["authority"]["generation_id"] == resolve_authority(tmp_path / ".ea").generation_id
    )
    assert payload["native"] == {
        "track_key": None,
        "milestone_key": None,
        "open_task_count": 0,
        "open_tasks": [],
    }
    assert payload["recent_decisions"] == []
    assert payload["blockers"] == []


def test_status_text_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke(lay_epoch2_tree(tmp_path), monkeypatch, "status")
    assert result.exit_code == 0, result.output
    assert "track: <none> milestone: <none>" in result.stdout
    assert "authority: epoch 2 (generation gen-" in result.stdout
    assert "blockers: none" in result.stdout


def test_status_command_local_json_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status --json`` (subcommand-level) also activates JSON emission."""
    result = _invoke(lay_epoch2_tree(tmp_path), monkeypatch, "status", "--json")
    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)) == _PAYLOAD_KEYS


def test_status_returns_not_found_when_no_tree_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke(tmp_path / ".ea" / "state.json", monkeypatch, "--json", "status")
    assert result.exit_code == 1  # NOT_FOUND
    body = json.loads(result.stdout)
    assert body["error"] == "UserError"
    assert "no eawf tree at" in body["message"]


def test_status_reports_an_epoch1_tree_without_reading_its_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tree with only a v1 document is not read; its gap is the one blocker."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text("not json at all")

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an epoch-1 tree must not read native decisions")

    monkeypatch.setattr("eawf.observability.reflect.decisions.read_native_decisions", _raise)
    payload = _status_json(state_path, monkeypatch)
    assert payload["authority"] == {"epoch": 1, "gap": "undeclared", "generation_id": None}
    assert payload["native"] is None
    assert payload["recent_decisions"] == []
    assert payload["blockers"] == [
        "tree is at authority epoch 1 (undeclared); status reads only epoch 2"
    ]
    text = _invoke(state_path, monkeypatch, "status")
    assert text.exit_code == 0, text.output
    assert "track:" not in text.stdout
    assert "authority: epoch 1 (undeclared)" in text.stdout


def test_status_workspace_flag_overrides_pwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lay_epoch2_tree(tmp_path)
    monkeypatch.delenv("EA_STATE", raising=False)
    _stub_no_git(monkeypatch)
    result = runner.invoke(app, ["--json", "status", "-w", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["authority"]["epoch"] == 2


def test_status_is_byte_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``eawf status`` mutates nothing: every file of the tree is byte-equal after it."""
    state_path = lay_epoch2_tree(tmp_path)
    tree = state_path.parent

    def _digests() -> dict[Path, str]:
        return {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tree.rglob("*")
            if path.is_file()
        }

    before = _digests()
    result = _invoke(state_path, monkeypatch, "--json", "status")
    assert result.exit_code == 0, result.output
    assert _digests() == before


def _native_task(key: str, status: TaskStatus) -> dict[str, Any]:
    """Return one native backlog Task row, as ``eawf task create`` stores it."""
    at = datetime(2026, 10, 1, tzinfo=UTC).isoformat()
    row: dict[str, Any] = Task.model_validate(
        {
            "uid": str(uuid4()),
            "key": key,
            "urn": f"eawf://QR/QR/QR/task/{key}",
            "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
            "revision": 1,
            "created_at": at,
            "updated_at": at,
            "priority": TaskPriority.P1.value,
            "intent": f"deliver {key}",
            "contract_revision": 1,
            "status": status.value,
        }
    ).model_dump(mode="json")
    return row


def _epoch2_tree_with_tasks(tmp_path: Path, tasks: dict[str, TaskStatus]) -> Path:
    """Bear a tree at epoch 2 and file ``tasks`` in its generation."""
    state_path = lay_epoch2_tree(tmp_path)
    document_path = _document_path(state_path)
    document = read_document(document_path)
    document["task"] = {key: _native_task(key, status) for key, status in tasks.items()}
    write_document(document_path, document)
    return state_path


def test_status_lists_the_open_native_tasks_of_an_epoch2_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _epoch2_tree_with_tasks(
        tmp_path, {"QR-0001": TaskStatus.DRAFT, "QR-0002": TaskStatus.DROPPED}
    )
    native = _status_json(state_path, monkeypatch)["native"]
    assert native["open_task_count"] == 1
    assert native["open_tasks"] == [
        {"key": "QR-0001", "status": "DRAFT", "title": "deliver QR-0001"}
    ]
    text = _invoke(state_path, monkeypatch, "status").stdout
    assert "open tasks: 1 (QR-0001 DRAFT)" in text


def test_status_lists_at_most_ten_open_tasks_but_counts_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [f"QR-{n:04d}" for n in range(1, 12)]
    state_path = _epoch2_tree_with_tasks(tmp_path, dict.fromkeys(keys, TaskStatus.DRAFT))
    native = _status_json(state_path, monkeypatch)["native"]
    assert native["open_task_count"] == 11
    assert [t["key"] for t in native["open_tasks"]] == keys[:10]


def test_status_says_an_epoch2_tree_with_no_open_task_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke(_epoch2_tree_with_tasks(tmp_path, {}), monkeypatch, "status")
    assert result.exit_code == 0, result.output
    assert "open tasks: none" in result.stdout


def test_status_resolves_no_wave_commit_from_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status reads git once for its head and never per wave."""

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("status must not resolve a wave commit from git")

    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.derive_wave_sha", _raise)
    monkeypatch.setattr("eawf.runtime.vcs.wave_sha.build_wave_sha_index", _raise)
    state_path = lay_epoch2_tree(tmp_path)
    assert "drift" not in _status_json(state_path, monkeypatch)
    text = _invoke(state_path, monkeypatch, "status")
    assert text.exit_code == 0, text.output
    assert "drift:" not in text.stdout


def test_status_refuses_a_generation_document_that_is_not_an_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    _document_path(state_path).write_text("[]")
    result = _invoke(state_path, monkeypatch, "--json", "status")
    assert result.exit_code == 1, result.output
    assert "generation document unreadable" in json.loads(result.stdout)["message"]


def test_status_refuses_a_milestone_row_the_projection_cannot_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    document_path = _document_path(state_path)
    document = read_document(document_path)
    document["milestone"] = {"MLS-0001": {"status": "ACTIVE"}}
    write_document(document_path, document)
    result = _invoke(state_path, monkeypatch, "--json", "status")
    assert result.exit_code == 1, result.output
    assert "generation document unreadable" in json.loads(result.stdout)["message"]


def test_status_reports_an_epoch2_tree_whose_document_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    _document_path(state_path).unlink()
    payload = _status_json(state_path, monkeypatch)
    assert payload["authority"]["epoch"] == 2
    assert payload["native"] is None


_DECISION_SCOPE = "eawf://WSP-MAIN/PRJ-QR/REP-QR/milestone/MLS-0001"


def _native_decision(key: str, created_at: str, **overrides: Any) -> Decision:
    """Return an active native decision filed at ``created_at``."""
    fields: dict[str, Any] = {
        "key": key,
        "scope_ref": _DECISION_SCOPE,
        "title": f"Native verdict {key}",
        "decision": "Do the thing.",
        "rationale": "It is the thing to do.",
        "alternatives": [{"key": "do", "label": "Do it"}, {"key": "skip", "label": "Skip it"}],
        "chosen_option_key": "do",
        "consequences": ["The thing is done"],
        "evidence_refs": ["eawf://WSP-MAIN/PRJ-QR/REP-QR/evidence/EVD-0001"],
        "effective_policy_revision": 1,
        "created_at": created_at,
    }
    fields.update(overrides)
    proposed = Decision.model_validate(fields)
    return ratify(proposed, by="OP-0001", at=proposed.created_at)


def _decision_ledger(state_path: Path) -> Path:
    return ledger_path(_document_path(state_path), Epoch2Collection.DECISION)


def _file_decision(
    state_path: Path, decision: Decision, *, replaces: LedgerRecord | None = None
) -> LedgerRecord:
    """Append ``decision``'s ledger line, as a successor of ``replaces`` when given."""
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key=decision.key,
        status=decision.status.value,
        recorded_at=decision.created_at,
        supersedes=None if replaces is None else line_digest(render_ledger_line(replaces)),
        payload={"payload": decision.model_dump(mode="json")},
    )
    append_ledger_record(_decision_ledger(state_path), record)
    return record


def _status_decisions(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    decisions: list[dict[str, str]] = _status_json(state_path, monkeypatch)["recent_decisions"]
    return decisions


def test_status_lists_native_decisions_newest_first_and_passes_over_imported_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    imported = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D02",
        status="imported",
        recorded_at=datetime(2026, 5, 9, tzinfo=UTC),
        payload={"origin": {"kind": "legacy"}, "payload": {"id": "D02", "title": "old"}},
    )
    append_ledger_record(_decision_ledger(state_path), imported)
    _file_decision(state_path, _native_decision("D03", "2026-05-10T00:00:00Z"))
    _file_decision(state_path, _native_decision("D04", "2026-05-11T00:00:00Z"))
    assert _status_decisions(state_path, monkeypatch) == [
        {"id": "D04", "title": "Native verdict D04", "status": "active"},
        {"id": "D03", "title": "Native verdict D03", "status": "active"},
    ]
    text = _invoke(state_path, monkeypatch, "status").stdout
    assert "recent decisions: D04, D03" in text


def test_status_caps_the_decisions_at_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    for day in range(10, 16):
        _file_decision(state_path, _native_decision(f"D{day}", f"2026-05-{day}T00:00:00Z"))
    decisions = _status_decisions(state_path, monkeypatch)
    assert [d["id"] for d in decisions] == ["D15", "D14", "D13", "D12", "D11"]


def test_status_lists_no_decision_for_an_epoch2_tree_with_an_empty_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    assert _status_decisions(state_path, monkeypatch) == []
    assert "recent decisions: none" in _invoke(state_path, monkeypatch, "status").stdout


def test_status_shows_a_superseded_native_decision_as_superseded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    old = _native_decision("D10", "2026-05-10T00:00:00Z")
    old_line = _file_decision(state_path, old)
    new = _native_decision("D11", "2026-05-11T00:00:00Z", supersedes="D10")
    _file_decision(state_path, supersede(old, new), replaces=old_line)
    _file_decision(state_path, new)
    assert _status_decisions(state_path, monkeypatch) == [
        {"id": "D11", "title": "Native verdict D11", "status": "active"},
        {"id": "D10", "title": "Native verdict D10", "status": "superseded"},
    ]


def test_status_reports_around_a_torn_decision_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    ledger = _decision_ledger(state_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"schema_version":"2.0"')
    payload = _status_json(state_path, monkeypatch)
    assert payload["recent_decisions"] == []
    assert [b for b in payload["blockers"] if b.startswith("decision ledger unreadable")]


def test_status_reports_around_a_native_decision_line_its_model_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D10",
        status="ACTIVE",
        recorded_at=datetime(2026, 5, 10, tzinfo=UTC),
        payload={"payload": {"key": "D10"}},
    )
    append_ledger_record(_decision_ledger(state_path), record)
    payload = _status_json(state_path, monkeypatch)
    assert payload["recent_decisions"] == []
    assert [b for b in payload["blockers"] if b.startswith("decision ledger unreadable")]
