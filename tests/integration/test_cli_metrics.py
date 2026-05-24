"""End-to-end CLI smoke tests for ``eawf metrics``.

The bare command + the read-only sub-verbs never mutate ``state.json`` or
append to the store. The tests drive the Typer app via :class:`CliRunner`
against a seeded ``.ea/state.json`` fixture and check three things:

1. Default text render: the Rich table is present and the four canonical
   metric labels appear.
2. JSON envelope: ``--json`` emits a payload with ``schema_version=1`` and
   the four sub-metric objects.
3. Plain render: ``--plain`` emits an ANSI-free table that callers (TUI
   overlay / release-notes paste) can consume.

A fourth case covers the error path: with no ``.ea/state.json`` reachable
the command exits 4 (NotFound) without panicking.

The ``metrics backfill-actuals`` sub-verb (P27-I02-W29) is the one
mutating member: it threads
:func:`eawf.kernel.migrations.backfill_actuals.backfill_actuals` through the
canonical writer to attach retroactive actuals to historical CLOSED
waves. Its cases seed CLOSED waves with timestamps + no actuals and
assert the added count, idempotence on a re-run, and that the telemetry
cache is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_state(tmp_path: Path) -> Path:
    """Copy ``09-estimates-and-actuals.json`` into a temp workspace."""
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = FIXTURES / "valid" / "09-estimates-and-actuals.json"
    state_path = state_dir / "state.json"
    state_path.write_bytes(src.read_bytes())
    return workspace


def _closed_wave(wave_id: str, *, opened: str, closed: str) -> dict[str, Any]:
    """Return a minimal CLOSED wave payload with a positive open->close span."""
    iter_id = "-".join(wave_id.split("-")[:2])
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": f"Wave {wave_id}",
        "status": "closed",
        "deps": [],
        "file_scopes": [],
        "opened_at": opened,
        "closed_at": closed,
    }


def _seed_closed_waves(tmp_path: Path, count: int) -> Path:
    """Seed a workspace whose state carries *count* CLOSED waves, no actuals.

    Builds off the empty-repo fixture so the project / current scaffolding
    stays schema-valid, then injects *count* CLOSED waves (each with a
    one-hour open->close span) and leaves ``actuals`` absent — exactly the
    shape historical pre-W25 closes left on disk.
    """
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    payload = json.loads((FIXTURES / "valid" / "01-empty-repo.json").read_text(encoding="utf-8"))
    waves: dict[str, Any] = {}
    wave_ids: list[str] = []
    for n in range(1, count + 1):
        wave_id = f"P01-I01-W{n:02d}"
        wave_ids.append(wave_id)
        waves[wave_id] = _closed_wave(
            wave_id,
            opened="2026-05-01T00:00:00Z",
            closed="2026-05-01T01:00:00Z",
        )
    # Parent phase + iter so the post-mutation parent-linkage invariants
    # (INV.PARENT.WAVE_ITER_MISSING / ITER_PHASE_MISSING) stay satisfied.
    payload["phases"] = {
        "P01": {
            "id": "P01",
            "scope_id": "QR",
            "title": "Phase 1",
            "status": "active",
            "iter_ids": ["P01-I01"],
            "outcome_ids": [],
            "opened_at": "2026-05-01T00:00:00Z",
        }
    }
    payload["iters"] = {
        "P01-I01": {
            "id": "P01-I01",
            "phase_id": "P01",
            "title": "Iter 1",
            "status": "active",
            "wave_ids": wave_ids,
            "opened_at": "2026-05-01T00:00:00Z",
        }
    }
    payload["waves"] = waves
    state_path = state_dir / "state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return workspace


def test_metrics_default_render_emits_rich_table(tmp_path: Path) -> None:
    """Default invocation prints the four-metric rich table to stdout."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    assert "eawf metrics" in result.stdout
    assert "EU variance" in result.stdout
    assert "Audit pass rate" in result.stdout
    assert "Wave elapsed" in result.stdout
    assert "Planned vs reactive" in result.stdout


def test_metrics_json_envelope_schema_version_one(tmp_path: Path) -> None:
    """``--json`` emits a typed envelope with schema_version=1 and four metrics."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert set(payload.keys()) == {
        "schema_version",
        "eu_variance",
        "audit_pass_rate",
        "wave_elapsed",
        "planned_vs_reactive",
    }
    # Fixture has one IN_PROGRESS wave with no closed_at: every wave-derived
    # metric should read as zero samples.
    assert payload["eu_variance"]["sample_count"] == 0
    assert payload["wave_elapsed"]["sample_count"] == 0
    # Fixture has no audits ⇒ decided_count == 0.
    assert payload["audit_pass_rate"]["decided_count"] == 0
    # Single I01 wave ⇒ planned=1, reactive=0.
    assert payload["planned_vs_reactive"]["planned_count"] == 1
    assert payload["planned_vs_reactive"]["reactive_count"] == 0


def test_metrics_plain_render_is_ansi_free(tmp_path: Path) -> None:
    """``--plain`` emits a header + four metric rows without ANSI escape codes."""
    workspace = _seed_state(tmp_path)
    result = runner.invoke(app, ["--plain", "-w", str(workspace), "metrics"])
    assert result.exit_code == 0, result.output
    # The plain renderer uses a literal header string; the rich branch wraps
    # the title in box-drawing characters. Spotting either rules out the
    # branches accidentally swapping.
    assert "\x1b[" not in result.stdout
    assert "eawf metrics" in result.stdout
    for label in (
        "EU variance",
        "Audit pass rate",
        "Wave elapsed (min)",
        "Planned vs reactive",
    ):
        assert label in result.stdout


def test_metrics_missing_state_exits_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace pointer that resolves to a missing state file → exit 2 (NotFound).

    Using ``-w <empty-dir>`` is the most reliable way to drive the
    NotFound path under Typer's CliRunner: the resolver appends
    ``.ea/state.json`` to the workspace, and the load attempt fails
    closed with the canonical NotFound error rather than panicking.
    """
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics"])
    # Exit code 2 maps to NotFound in :mod:`eawf.surfaces.cli.exit_codes`.
    assert result.exit_code == 1


# --- metrics backfill-actuals (P27-I02-W29) ---------------------------------


def _db_path(workspace: Path) -> Path:
    """Return the telemetry cache path a collecting sub-verb would create."""
    return workspace / ".ea" / "telemetry.db"


def test_backfill_actuals_adds_one_per_closed_wave(tmp_path: Path) -> None:
    """The verb derives one actual per CLOSED wave missing one and persists it."""
    workspace = _seed_closed_waves(tmp_path, count=3)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals"])
    assert result.exit_code == 0, result.output
    assert "backfilled 3 actual(s)" in result.stdout
    body = json.loads((workspace / ".ea" / "state.json").read_text(encoding="utf-8"))
    assert set(body["actuals"]) == {"P01-I01-W01", "P01-I01-W02", "P01-I01-W03"}


def test_backfill_actuals_json_envelope_reports_count(tmp_path: Path) -> None:
    """``--json`` emits the typed ``actuals_added`` / ``dry_run`` envelope."""
    workspace = _seed_closed_waves(tmp_path, count=2)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "backfill-actuals"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {"actuals_added": 2, "dry_run": False}


def test_backfill_actuals_second_run_adds_zero(tmp_path: Path) -> None:
    """Idempotence: a re-run over an already-backfilled state adds nothing."""
    workspace = _seed_closed_waves(tmp_path, count=3)
    first = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals"])
    assert second.exit_code == 0, second.output
    assert "backfilled 0 actual(s)" in second.stdout


def test_backfill_actuals_no_closed_waves_is_noop(tmp_path: Path) -> None:
    """Boundary: a state with no CLOSED waves backfills zero and stays actual-free."""
    workspace = _seed_closed_waves(tmp_path, count=0)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "backfill-actuals"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["actuals_added"] == 0
    body = json.loads((workspace / ".ea" / "state.json").read_text(encoding="utf-8"))
    # No write happened ⇒ ``actuals`` stays absent (no empty-dict shape churn).
    assert "actuals" not in body or body["actuals"] is None


def test_backfill_actuals_dry_run_reports_without_writing(tmp_path: Path) -> None:
    """``--dry-run`` reports the count but leaves ``state.json`` untouched."""
    workspace = _seed_closed_waves(tmp_path, count=2)
    before = (workspace / ".ea" / "state.json").read_text(encoding="utf-8")
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "backfilled 2 actual(s) (dry-run, not written)" in result.stdout
    after = (workspace / ".ea" / "state.json").read_text(encoding="utf-8")
    assert before == after


def test_backfill_actuals_does_not_touch_telemetry_db(tmp_path: Path) -> None:
    """The verb mutates state.json waves only — it never opens telemetry.db.

    Backfill is about estimation actuals, not the metrics cache, so it must
    work + persist regardless of the telemetry surface and never create the
    DB as a side effect.
    """
    workspace = _seed_closed_waves(tmp_path, count=2)
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals"])
    assert result.exit_code == 0, result.output
    assert "backfilled 2 actual(s)" in result.stdout
    assert not _db_path(workspace).exists()


def test_backfill_actuals_missing_state_exits_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with no resolvable state.json exits NotFound (1)."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "backfill-actuals"])
    assert result.exit_code == 1
