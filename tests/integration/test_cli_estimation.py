"""End-to-end CLI tests for ``eawf estimate`` / ``eawf actual``.

Drives the Typer app via :class:`typer.testing.CliRunner` against a temp
``.ea/state.json`` (seeded from the Phase 1 valid fixtures) and asserts:

- estimate creates an ``estimates.jsonl`` envelope and updates state.estimates.
- actual start opens a segment, writes ``actuals.jsonl``, updates state.actuals.
- actual stop closes the segment with a non-zero elapsed_eu.
- Double-open for the same (scope, session) pair is rejected with exit 4.
- actual recover marks stale segments abandoned with the cap applied.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.lock.stale import STALE_HEARTBEAT_SECONDS

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_state(tmp_path: Path) -> Path:
    """Copy ``01-empty-repo.json`` to a temp ``.ea/state.json``.

    Returns the workspace root so the caller can pass ``-w`` to the CLI.
    """
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    src = FIXTURES / "valid" / "01-empty-repo.json"
    state_path = state_dir / "state.json"
    state_path.write_bytes(src.read_bytes())
    return workspace


def _read_state(workspace: Path) -> dict[str, Any]:
    return json.loads((workspace / ".ea" / "state.json").read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_estimate_create_writes_state_and_jsonl(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "estimate",
            "set",
            "P01-I01-W01",
            "--source",
            "prep",
            "--confidence",
            "m",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["scope"] == "P01-I01-W01"
    assert payload["estimate_id"] == "EST-P01-I01-W01"
    assert payload["expected_eu"] > 0

    state = _read_state(workspace)
    assert state["estimates"]["P01-I01-W01"]["id"] == "EST-P01-I01-W01"
    assert state["estimates"]["P01-I01-W01"]["confidence"] == "medium"

    estimates = _read_jsonl(workspace / ".ea" / "store" / "estimate.jsonl")
    assert len(estimates) == 1
    assert estimates[0]["kind"] == "estimate"
    assert estimates[0]["scope_id"] == "P01-I01-W01"


def test_estimate_update_replaces_summary(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    runner.invoke(
        app,
        ["-w", str(workspace), "estimate", "set", "P01-I01-W01", "--source", "prep"],
    )
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "estimate",
            "update",
            "P01-I01-W01",
            "--source",
            "replan",
            "--confidence",
            "l",
        ],
    )
    assert result.exit_code == 0, result.output
    state = _read_state(workspace)
    assert state["estimates"]["P01-I01-W01"]["confidence"] == "low"

    estimates = _read_jsonl(workspace / ".ea" / "store" / "estimate.jsonl")
    # Two envelopes: prep + replan (different store record ids -> append-only).
    assert len(estimates) == 2
    sources = {env["payload"]["source"] for env in estimates}
    assert sources == {"prep", "replan"}


def test_estimate_update_without_existing_returns_not_found(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "estimate",
            "update",
            "P01-I01-W01",
            "--source",
            "replan",
        ],
    )
    assert result.exit_code == 1  # NOT_FOUND
    assert "no estimate exists" in result.output


def test_estimate_invalid_confidence_returns_invalid_input(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "estimate",
            "set",
            "P01-I01-W01",
            "--confidence",
            "z",
        ],
    )
    assert result.exit_code == 1  # INVALID_INPUT


def test_actual_start_then_stop_round_trip(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    start_result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    assert start_result.exit_code == 0, start_result.output
    started_payload = json.loads(start_result.stdout)
    assert started_payload["session"] == "SES-001"

    state = _read_state(workspace)
    assert state["actuals"]["P01-I01-W01"]["status"] == "active"
    start_record_id = state["actuals"]["P01-I01-W01"]["current_store_record_id"]
    assert start_record_id.startswith("ACT-P01-I01-W01-")

    stop_result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "actual",
            "stop",
            "P01-I01-W01",
        ],
    )
    assert stop_result.exit_code == 0, stop_result.output
    stop_payload = json.loads(stop_result.stdout)
    assert stop_payload["status"] == "done"
    assert stop_payload["elapsed_eu"] >= 0  # could be ~0 if instant test

    state = _read_state(workspace)
    assert state["actuals"]["P01-I01-W01"]["status"] == "done"
    stop_record_id = state["actuals"]["P01-I01-W01"]["current_store_record_id"]
    assert stop_record_id.startswith("ACT-P01-I01-W01-")
    assert stop_record_id != start_record_id, "start and stop must produce distinct envelope IDs"

    actuals = _read_jsonl(workspace / ".ea" / "store" / "actual.jsonl")
    # Two envelopes — start + stop, each with its own timestamped ID.
    assert len(actuals) == 2
    envelope_ids = [env["id"] for env in actuals]
    assert envelope_ids[0] == start_record_id
    assert envelope_ids[1] == stop_record_id
    assert envelope_ids[0] != envelope_ids[1]


def test_actual_start_double_open_rejected(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    first = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    assert second.exit_code == 2  # VALIDATION_FAILED
    assert "already open" in second.output


def test_actual_stop_without_open_segment_returns_not_found(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "actual", "stop", "P01-I01-W01"],
    )
    assert result.exit_code == 1  # NOT_FOUND


def test_actual_stop_with_status_abandoned_marks_abandoned(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    runner.invoke(
        app,
        ["-w", str(workspace), "actual", "start", "P01-I01-W01", "--session", "SES-001"],
    )
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "stop",
            "P01-I01-W01",
            "--status",
            "abandoned",
        ],
    )
    assert result.exit_code == 0, result.output
    state = _read_state(workspace)
    assert state["actuals"]["P01-I01-W01"]["status"] == "abandoned"


def test_actual_stop_invalid_status_returns_invalid_input(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    runner.invoke(
        app,
        ["-w", str(workspace), "actual", "start", "P01-I01-W01", "--session", "SES-001"],
    )
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "stop",
            "P01-I01-W01",
            "--status",
            "garbage-status",
        ],
    )
    assert result.exit_code == 1  # INVALID_INPUT


def test_actual_recover_promotes_stale_segment(tmp_path: Path) -> None:
    """Recovery must mark a segment ABANDONED when its lock holder is dead."""
    workspace = _seed_state(tmp_path)

    # Open a segment normally.
    runner.invoke(
        app,
        ["-w", str(workspace), "actual", "start", "P01-I01-W01", "--session", "SES-001"],
    )

    # Plant a stale lockfile in the recover-locks dir.
    locks_dir = workspace / ".ea" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    long_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    (locks_dir / "actual-P01-I01-W01.lock").write_text(
        json.dumps(
            {
                "pid": 999_999_999,
                "hostname": "ghost",
                "started_at": long_ago,
                "heartbeat_at": long_ago,
            }
        )
    )

    # Backdate the segment so the cap actually applies (would otherwise be ~0s).
    actuals_path = workspace / ".ea" / "store" / "actual.jsonl"
    lines = actuals_path.read_text(encoding="utf-8").splitlines()
    # Only one envelope so far (the start).
    last = json.loads(lines[-1])
    new_started = (datetime.now(UTC) - timedelta(hours=8)).isoformat()
    last["payload"]["segments"][0]["started_at"] = new_started
    last["payload"]["segments"][0]["ended_at"] = new_started
    lines[-1] = json.dumps(last)
    actuals_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "actual", "recover"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recovered_count"] == 1
    assert payload["recovered"][0]["scope"] == "P01-I01-W01"

    state = _read_state(workspace)
    assert state["actuals"]["P01-I01-W01"]["status"] == "abandoned"

    # Cap honoured: elapsed_eu corresponds to STALE_HEARTBEAT_SECONDS, not 8h.
    expected_capped_minutes = STALE_HEARTBEAT_SECONDS / 60.0
    expected_capped_eu = expected_capped_minutes / 30.0
    assert state["actuals"]["P01-I01-W01"]["elapsed_eu"] == pytest.approx(
        expected_capped_eu, rel=1e-9
    )


def test_actual_recover_noop_when_no_stale_segments(tmp_path: Path) -> None:
    """recover finds nothing when the only active segment has a live lock."""
    workspace = _seed_state(tmp_path)
    runner.invoke(
        app,
        ["-w", str(workspace), "actual", "start", "P01-I01-W01", "--session", "SES-001"],
    )
    # Plant a fresh lock so is_stale returns False.
    locks_dir = workspace / ".ea" / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    (locks_dir / "actual-P01-I01-W01.lock").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": "live",
                "started_at": now,
                "heartbeat_at": now,
            }
        )
    )

    result = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "actual", "recover"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["recovered_count"] == 0


def test_estimate_emits_event_to_events_jsonl(tmp_path: Path) -> None:
    workspace = _seed_state(tmp_path)
    runner.invoke(
        app,
        ["-w", str(workspace), "estimate", "set", "P01-I01-W01", "--source", "prep"],
    )
    events = _read_jsonl(workspace / ".ea" / "store" / "event.jsonl")
    assert any(env["payload"]["event_type"] == "estimate.created" for env in events)


# ---- RX-C: state-then-jsonl atomicity regression ----------------------------


def test_actual_start_appends_jsonl_before_committing_state(
    tmp_path: Path,
) -> None:
    """jsonl-first ordering: the audit envelope lands before state.json mutates.

    Direct observation:
    - ``actuals.jsonl`` contains exactly one envelope after ``actual start``.
    - That envelope's ``id`` matches ``state.actuals[scope].current_store_record_id``.
    - Both records share the same ``now`` timestamp, so we cross-check that
      the envelope was constructed in the same mutation cycle as the state
      summary.
    """
    workspace = _seed_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    assert result.exit_code == 0, result.output

    actuals = _read_jsonl(workspace / ".ea" / "store" / "actual.jsonl")
    assert len(actuals) == 1
    envelope = actuals[0]
    assert envelope["kind"] == "actual"
    assert envelope["scope_id"] == "P01-I01-W01"

    state = _read_state(workspace)
    summary = state["actuals"]["P01-I01-W01"]
    # The state summary references exactly the envelope we just appended.
    assert envelope["id"] == summary["current_store_record_id"]


def test_actual_start_jsonl_lands_when_commit_state_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash-mid-flow proof: a failing ``_commit_state`` still leaves a jsonl record.

    Forces the post-jsonl ``_commit_state`` to raise ``ValidationFailed``.
    With the RX-C ordering (``_append_jsonl`` -> ``_emit_event`` ->
    ``_commit_state``), the actuals.jsonl record is already on disk by the
    time the state writer fails, so a recovery tool can replay the segment
    open. The state.json must remain unmutated (no actuals entry) because
    ``_commit_state`` aborted before writing.
    """
    workspace = _seed_state(tmp_path)

    from eawf.cli import errors as cli_errors
    from eawf.cli.commands import estimation as est_cmd

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise cli_errors.ValidationFailed("forced for atomicity-ordering test")

    monkeypatch.setattr(est_cmd, "_commit_state", _boom)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    # _commit_state raised ValidationFailed -> exit code 4.
    assert result.exit_code == 2, result.output

    # The actuals.jsonl record landed BEFORE _commit_state ran.
    actuals = _read_jsonl(workspace / ".ea" / "store" / "actual.jsonl")
    assert len(actuals) == 1
    assert actuals[0]["kind"] == "actual"
    assert actuals[0]["scope_id"] == "P01-I01-W01"

    # The events.jsonl record also landed (emitted before commit).
    events = _read_jsonl(workspace / ".ea" / "store" / "event.jsonl")
    assert any(env["payload"]["event_type"] == "actual.start" for env in events)

    # state.json was NOT mutated — actuals key is absent or empty.
    state = _read_state(workspace)
    assert not state.get("actuals"), (
        f"state.actuals must be empty since _commit_state aborted; got {state.get('actuals')!r}"
    )
