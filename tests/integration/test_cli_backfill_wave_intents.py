from __future__ import annotations

from pathlib import Path

import orjson
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from tests.unit.state.test_wave_intent_backfill import _state

runner = CliRunner()


def _write_state(root: Path) -> Path:
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_state().model_dump_json(), encoding="utf-8")
    return state_path


def _read_state(state_path: Path) -> dict:
    return orjson.loads(state_path.read_bytes())


def test_wave_intents_dry_run_reports_target_without_mutating(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = _write_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))

    result = runner.invoke(
        app,
        ["--json", "backfill", "wave-intents", "--wave", "P01-I01-W01"],
    )

    assert result.exit_code == 0, result.output
    body = orjson.loads(result.stdout)
    assert body["pending"] == ["P01-I01-W01"]
    assert body["changed"] == []
    assert _read_state(state_path)["waves"]["P01-I01-W01"]["intent"] is None


def test_wave_intents_apply_backfills_target_and_records_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = _write_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))

    result = runner.invoke(
        app,
        ["--json", "backfill", "wave-intents", "--apply", "--wave", "P01-I01-W01"],
    )

    assert result.exit_code == 0, result.output
    body = orjson.loads(result.stdout)
    assert body["changed"] == ["P01-I01-W01"]
    intent = _read_state(state_path)["waves"]["P01-I01-W01"]["intent"]
    assert intent["problem"] == "wave P01-I01-W01 was synced without typed intent"
    events = store_path(state_path, StoreKind.EVENT).read_text(encoding="utf-8")
    assert "backfill.wave_intents" in events


def test_wave_intents_check_exits_nonzero_when_repair_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = _write_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))

    result = runner.invoke(
        app,
        ["--json", "backfill", "wave-intents", "--check", "--wave", "P01-I01-W01"],
    )

    assert result.exit_code == 1
    body = orjson.loads(result.stdout)
    assert body["pending"] == ["P01-I01-W01"]


def test_wave_intents_check_exits_zero_after_apply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = _write_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    apply_result = runner.invoke(
        app,
        ["backfill", "wave-intents", "--apply", "--wave", "P01-I01-W01"],
    )
    assert apply_result.exit_code == 0, apply_result.output

    result = runner.invoke(
        app,
        ["--json", "backfill", "wave-intents", "--check", "--wave", "P01-I01-W01"],
    )

    assert result.exit_code == 0, result.output
    body = orjson.loads(result.stdout)
    assert body["pending"] == []
    assert body["rows"][0]["reason"] == "already_has_intent"


def test_wave_intents_requires_explicit_wave(tmp_path: Path, monkeypatch) -> None:
    state_path = _write_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))

    result = runner.invoke(app, ["backfill", "wave-intents"])

    assert result.exit_code != 0
    assert "--wave is required" in result.output
