"""Regression tests for the lifecycle event-first ordering invariant.

The canonical mutation pattern in :mod:`eawf.cli.commands.lifecycle` writes
the JSONL audit envelope to ``<state>/store/event.jsonl`` *before* it
mutates ``state.json``. This mirrors the evidence-side ordering established
in commit ``18ee287`` and protects two failure modes:

1. If the event append fails, ``state.json`` is unchanged — no orphan
   state mutation with a missing audit trail.
2. If the state write fails after a successful append, the surplus event
   record is forward-replayable from the JSONL log.

The two scenarios below assert the invariant directly:

* Happy path: at the moment :func:`_write_state_unlocked` runs, the new
  ``event.jsonl`` line is already on disk (structural check that does not
  rely on mtime resolution).
* Sad path: monkey-patching ``append_envelope`` to raise ``OSError`` leaves
  ``state.json`` byte-for-byte unchanged; the CLI exits non-zero.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.commands import lifecycle as lifecycle_module

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp workspace with ``EA_STATE`` pointing inside it."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _event_path(workspace: Path) -> Path:
    return workspace / ".ea" / "store" / "event.jsonl"


def _init_project(workspace: Path) -> None:
    """Drive ``eawf project init`` against the workspace fixture."""
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
    )
    assert res.exit_code == 0, res.stdout


# ---- happy path: events.jsonl is on disk before state.json is written ------


def test_phase_open_writes_event_before_state(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the moment ``_write_state_unlocked`` fires, the new event line is
    already in ``event.jsonl`` on disk.

    This is a structural ordering assertion (not mtime-based) — it survives
    on fast filesystems where mtime resolution coarsens consecutive writes
    into the same tick.
    """
    _init_project(workspace)
    # ``project init`` already wrote one event line. We capture the count
    # of event lines visible at the instant ``phase open`` writes its
    # state.json; that count must reflect the new event line (>= 2).
    initial_lines = _event_path(workspace).read_text(encoding="utf-8").splitlines()
    assert len(initial_lines) == 1, "project init should have written one event"

    captured: dict[str, Any] = {}
    real_writer = lifecycle_module._write_state_unlocked

    def _spy_writer(path: Path, data: dict[str, Any]) -> None:
        captured["lines_at_write"] = _event_path(workspace).read_text(encoding="utf-8").splitlines()
        real_writer(path, data)

    monkeypatch.setattr(lifecycle_module, "_write_state_unlocked", _spy_writer)

    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code == 0, res.stdout

    # The spy fires for `phase open`'s state write. At that moment the new
    # event line for `phase open` must already be on disk: the visible
    # count must equal the post-init count + 1.
    lines_at_write = captured["lines_at_write"]
    assert len(lines_at_write) == 2, (
        f"event.jsonl must contain the new event before state.json is written; "
        f"saw {len(lines_at_write)} line(s) at write time"
    )
    # And the second (newest) line is the `phase open` event we just added.
    newest = orjson.loads(lines_at_write[1])
    assert newest["payload"]["command"] == "phase open"


def test_phase_open_event_mtime_le_state_mtime(workspace: Path) -> None:
    """Mtime-level sanity check: ``event.jsonl`` is mtime <= ``state.json``.

    Belt-and-braces complement to the structural check above. Filesystems
    with sub-millisecond mtime resolution (APFS, ext4 with ``relatime``)
    can collapse the two writes into the same tick, so we accept equality.
    """
    _init_project(workspace)
    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code == 0, res.stdout

    state_mtime = _state_path(workspace).stat().st_mtime_ns
    event_mtime = _event_path(workspace).stat().st_mtime_ns
    assert event_mtime <= state_mtime, (
        f"event.jsonl mtime ({event_mtime}) must be <= state.json mtime "
        f"({state_mtime}) — events-first invariant"
    )


# ---- sad path: append failure leaves state.json untouched ------------------


def test_phase_open_state_unchanged_when_event_append_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``append_envelope`` raises during ``phase open``, ``state.json`` is
    byte-for-byte unchanged.

    The events-first ordering means the state writer is never reached when
    the append fails. The CLI exits non-zero (the bubbling exception is
    routed through the canonical ``CliError`` envelope).
    """
    _init_project(workspace)
    state_path = _state_path(workspace)
    state_bytes_before = state_path.read_bytes()

    def _fail_append(*args: object, **kwargs: object) -> None:
        raise OSError("simulated event store failure")

    monkeypatch.setattr(lifecycle_module, "append_envelope", _fail_append)

    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    # Any non-zero exit suffices — the contract is "did not succeed".
    assert res.exit_code != 0, res.stdout

    # state.json is byte-for-byte the same as before the failed phase open.
    state_bytes_after = state_path.read_bytes()
    assert state_bytes_after == state_bytes_before, (
        "state.json must be byte-for-byte unchanged when event append fails"
    )


def test_phase_open_state_unchanged_when_event_append_lock_conflict(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LockConflict`` from the events store also leaves ``state.json`` intact.

    Maps to canonical exit 5 via the ``CliError`` envelope.
    """
    from eawf.cli.errors import LockConflict

    _init_project(workspace)
    state_path = _state_path(workspace)
    state_bytes_before = state_path.read_bytes()

    def _fail_lock(*args: object, **kwargs: object) -> None:
        raise LockConflict("simulated lock conflict on event.jsonl")

    monkeypatch.setattr(lifecycle_module, "append_envelope", _fail_lock)

    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code == 3, res.stdout

    state_bytes_after = state_path.read_bytes()
    assert state_bytes_after == state_bytes_before, (
        "state.json must be byte-for-byte unchanged when event lock conflict fires"
    )
