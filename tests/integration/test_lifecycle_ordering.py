"""Regression tests for the in-process mutation ordering invariant.

Per rule 4 + D-SUP-01 the daemon is the canonical writer; the in-process
path in :func:`eawf.cli.commands.lifecycle._commit_mutation` is the V1
CI / one-shot / recovery fallback. P27-I02-W18 flipped that fallback from
the legacy **event-first** ordering to a **state-first, WAL-backed**
ordering that mirrors the daemon's outcome-WAL algorithm. P27-I02-W32
then reordered ``mark_applied`` to fire BEFORE the event append:

    replay_wal → write_pending → write state.json → mark_applied
    → append event.jsonl → mark_fsynced

The invariant this buys is the inverse of the old one, and the one the
authority map actually wants: the event row is appended **only after**
``state.json`` is durably written, so a crash never leaves a *phantom
event* (an event whose state change did not commit). And because
``mark_applied`` fires before the append, a crash between the state
write and the event append leaves an ``.applied`` WAL record — which
``replay_wal`` re-issues the captured envelope for, instead of the
``.pending`` record it would POISON (silently dropping the event row).

The scenarios below assert the new invariant directly:

* Happy path: at the moment :func:`append_envelope` runs, ``state.json``
  is already the post-mutation payload on disk (state-first); after the
  call the WAL record has retired to ``.fsynced.json``.
* No-phantom-event crash: monkey-patching ``append_envelope`` to raise
  ``OSError`` after the state write leaves the new event row absent from
  ``event.jsonl`` (no phantom event) while an ``.applied`` WAL record
  survives for roll-forward; the CLI exits non-zero.
* Roll-forward: the next successful mutation runs ``replay_wal`` first,
  which re-issues the ``.applied`` record's envelope — the event row that
  was lost to the crash lands exactly once.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

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


def _wal_dir(workspace: Path) -> Path:
    return workspace / ".ea" / "locks" / "wal"


def _event_commands(workspace: Path) -> list[str]:
    """Return the ``payload.command`` of every event row, in order."""
    path = _event_path(workspace)
    if not path.exists():
        return []
    commands: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        commands.append(orjson.loads(line)["payload"]["command"])
    return commands


def _init_project(workspace: Path) -> None:
    """Drive ``eawf project init`` against the workspace fixture."""
    res = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
    )
    assert res.exit_code == 0, res.stdout


# ---- happy path: state.json is written before event.jsonl ------------------


def test_phase_open_writes_state_before_event(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At the moment ``append_envelope`` fires, ``state.json`` already holds
    the post-mutation payload (state-first ordering).

    Structural assertion (not mtime-based): we snapshot ``state.json`` at
    the instant the event append runs and confirm it already carries the
    new phase, proving the state write landed first.
    """
    _init_project(workspace)
    state_path = _state_path(workspace)
    state_before_open = orjson.loads(state_path.read_bytes())
    assert state_before_open["phases"] == {}, "no phases before phase open"

    from eawf.store import append as append_module

    captured: dict[str, Any] = {}
    real_append: Any = append_module.append_envelope

    def _spy_append(*args: Any, **kwargs: Any) -> None:
        captured["state_at_append"] = orjson.loads(state_path.read_bytes())
        real_append(*args, **kwargs)

    # ``append_envelope`` is the seam ``_commit_mutation`` calls after the
    # state write; spy on it so we observe the on-disk state at the instant
    # the event row lands.
    monkeypatch.setattr("eawf.store.append.append_envelope", _spy_append)

    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code == 0, res.stdout

    # At the moment the event append fired, state.json already carried the
    # new phase — the state write committed first.
    state_at_append = captured["state_at_append"]
    assert state_at_append["phases"], (
        "state.json must already hold the new phase before the event row is "
        "appended — state-first invariant"
    )
    # And the event row for `phase open` is now on disk.
    assert _event_commands(workspace) == ["project init", "phase open"]


def test_phase_open_retires_wal_record_to_fsynced(workspace: Path) -> None:
    """A clean ``phase open`` leaves the WAL record retired to ``.fsynced``.

    The state-first commit writes a ``.pending`` record, then state, then
    the event, then renames the record ``pending → applied → fsynced``. A
    clean run leaves only ``.fsynced.json`` (and no ``.pending`` /
    ``.applied`` leftovers).
    """
    _init_project(workspace)
    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code == 0, res.stdout

    wal_dir = _wal_dir(workspace)
    assert wal_dir.exists(), "the in-process fallback writes a repo-local WAL"
    pending = list(wal_dir.glob("*.pending.json"))
    applied = list(wal_dir.glob("*.applied.json"))
    fsynced = list(wal_dir.glob("*.fsynced.json"))
    assert pending == [], "no .pending records survive a clean commit"
    assert applied == [], "no .applied records survive a clean commit"
    # ``project init`` uses a bespoke create-the-file path (not
    # ``_commit_mutation``); only ``phase open`` retires a WAL record here.
    assert len(fsynced) == 1, f"expected one fsynced WAL record, saw {len(fsynced)}"


# ---- crash safety: append failure leaves NO phantom event ------------------


def test_phase_open_no_phantom_event_when_append_fails(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``append_envelope`` raises during ``phase open``, the event row is
    never written (no phantom event) even though ``state.json`` may have
    committed.

    The state-first ordering means the event row is appended only after
    the state write. When the append fails, the log carries no event for
    the mutation — the success criterion's "no event without its state
    change" holds. Because ``mark_applied`` fires before the append, an
    ``.applied`` WAL record survives for roll-forward (NOT a ``.pending``
    one, which replay would poison and silently drop the event row).
    """
    _init_project(workspace)
    events_before = _event_commands(workspace)
    assert events_before == ["project init"]

    def _fail_append(*args: object, **kwargs: object) -> None:
        raise OSError("simulated event store failure")

    # ``append_envelope`` is lazy-imported inside ``_commit_mutation`` from
    # its source module, so the failure seam is patched at the source.
    monkeypatch.setattr("eawf.store.append.append_envelope", _fail_append)

    res = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res.exit_code != 0, res.stdout

    # No phantom event: the event log carries nothing new for the failed
    # phase open — there is no event whose state change is unaccounted for.
    assert _event_commands(workspace) == ["project init"], (
        "event.jsonl must NOT carry a phase open event when the append fails — no phantom event"
    )
    # An .applied WAL record survives so the next mutation's replay re-issues
    # the dropped event row (the crash window is now mapped to APPLIED, not
    # PENDING — the latter would be poisoned and the row lost forever).
    assert list(_wal_dir(workspace).glob("*.pending.json")) == [], (
        "no .pending record should survive — mark_applied ran before the append"
    )
    applied = list(_wal_dir(workspace).glob("*.applied.json"))
    assert applied, "an .applied WAL record must survive the crashed append"


def test_phase_open_crash_then_next_mutation_rolls_forward(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next successful mutation replays the WAL and recovers the lost row.

    Scenario: ``phase open`` writes state but the event append crashes,
    leaving an ``.applied`` record (``mark_applied`` ran before the
    append). The subsequent ``phase open`` runs ``replay_wal`` first;
    replay re-issues the ``.applied`` record's captured envelope, so the
    event row the crash dropped lands exactly once — no longer lost to a
    poisoned ``.pending`` record as it was before W32.
    """
    from eawf.store import append as append_module

    _init_project(workspace)

    call_count = {"n": 0}
    real_append: Any = append_module.append_envelope

    def _flaky_append(*args: Any, **kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated event store failure (first append only)")
        real_append(*args, **kwargs)

    monkeypatch.setattr("eawf.store.append.append_envelope", _flaky_append)

    # First phase open: state write lands, mark_applied runs, event append
    # crashes — leaving an .applied record (not a .pending one).
    res1 = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert res1.exit_code != 0, res1.stdout
    applied_after_crash = list(_wal_dir(workspace).glob("*.applied.json"))
    assert applied_after_crash, "crash leaves an .applied WAL record"
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []

    # Second phase open succeeds; its commit runs replay_wal first, which
    # re-issues the prior .applied record's envelope (idempotent on
    # envelope id), recovering the event row the crash dropped. The
    # outcome-WAL captures the full post-apply envelope, so an APPLIED
    # record is re-issued verbatim — never poisoned.
    res2 = runner.invoke(app, ["phase", "open", "--auto", "--title", "P2"])
    assert res2.exit_code == 0, res2.stdout

    # No .applied/.pending leftovers, and nothing poisoned (the crash window
    # is APPLIED, which replay recovers — not the PENDING it used to poison).
    assert list(_wal_dir(workspace).glob("*.applied.json")) == []
    assert list(_wal_dir(workspace).glob("*.pending.json")) == []
    poisoned = list((_wal_dir(workspace) / "poisoned").glob("*.poisoned.json"))
    assert poisoned == [], "an APPLIED record is recovered, never poisoned"

    # Both event rows are present: the first (crashed) phase open's row was
    # re-issued by replay, and the second landed on its own append. No row
    # is lost — the S7 fix's whole point.
    commands = _event_commands(workspace)
    assert commands.count("phase open") == 2, (
        f"both phase open event rows should exist after roll-forward, saw {commands!r}"
    )
