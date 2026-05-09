"""Integration tests for ``eawf store compact``.

The canonical store path is ``<state_dir>/store/<kind>.jsonl``. Each test
seeds a fresh ``.ea/`` directory, writes a JSONL store with duplicate-id
records, and asserts the dedup count and exit code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_ea_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EA_STATE", raising=False)
    yield


def _seed_state(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    return state_path


def _memory_envelope(env_id: str, summary: str = "x") -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.MEMORY,
        scope_id=None,
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
        summary=summary,
        payload={"body": summary, "confidence": "high", "review_due": None},
    )


def _append(path: Path, env: Envelope) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(env.model_dump_json())
        fh.write("\n")


def test_store_compact_dedupes_duplicate_ids_in_memory_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    store_dir = state_path.parent / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    store_path = store_dir / "memory.jsonl"
    _append(store_path, _memory_envelope("M-1", summary="first"))
    _append(store_path, _memory_envelope("M-2", summary="only m2"))
    _append(store_path, _memory_envelope("M-1", summary="second"))

    result = runner.invoke(app, ["--json", "store", "compact", "--kind", "memory"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["kind"] == "memory"
    assert payload["records_in"] == 3
    assert payload["records_out"] == 2
    assert payload["dedup_count"] == 1
    assert payload["scope"] is None
    assert payload["budget"] is None
    assert payload["path"].endswith("store/memory.jsonl")


def test_store_compact_handles_missing_file_zero_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "store", "compact", "--kind", "memory"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["records_in"] == 0
    assert payload["records_out"] == 0
    assert payload["dedup_count"] == 0


def test_store_compact_records_scope_arg_in_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(
        app,
        ["--json", "store", "compact", "--kind", "memory", "--scope", "P01-I01"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["scope"] == "P01-I01"


def test_store_compact_records_budget_arg_in_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(
        app,
        ["--json", "store", "compact", "--kind", "memory", "--budget", "1024"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["budget"] == 1024


def test_store_compact_text_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["store", "compact", "--kind", "memory"])
    assert result.exit_code == 0, result.output
    assert "compact: kind=memory" in result.stdout
    assert "in=0" in result.stdout


def test_store_compact_returns_not_found_when_state_dir_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonexistent = tmp_path / "no" / "such" / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(nonexistent))
    result = runner.invoke(app, ["--json", "store", "compact", "--kind", "memory"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["error"] == "NotFound"


def test_store_compact_rejects_unknown_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["store", "compact", "--kind", "not-a-real-kind"])
    # Typer's enum coercion fails before our handler runs — that path returns
    # the standard Click "invalid choice" exit code (2).
    assert result.exit_code == 2
    assert "Invalid value" in result.output or "is not one of" in result.output


def test_store_compact_kind_default_is_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "store", "compact"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["kind"] == "memory"
