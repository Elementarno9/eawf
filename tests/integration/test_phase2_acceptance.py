"""Phase 2 acceptance gate: end-to-end lifecycle drive via the headless CLI.

This is the canonical scenario the planner enumerated in §"Acceptance gate":
``project init`` → ``phase open --auto`` → ``iter open`` → ``wave plan/claim/close``
→ ``status --json``. The test asserts the documented payload shape after the
full mutation chain runs.

**Cross-wave dependency.** The scenario depends on commands that land in
W01 (``project``, ``phase``, ``iter``, ``wave``) and W04 (``session``). Those
waves run in parallel under separate worktree branches; inside this W05
worktree alone, importing the W01/W04 modules raises :class:`ImportError`
and the entire module is skipped.

After all six waves cherry-pick onto ``feature/eawf-v0.1`` the main thread
runs the full suite — there the imports succeed and this test executes for
real, gating the phase merge into ``main``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Skip the entire module when W01 (lifecycle) or W04 (session) handlers are
# absent — see module docstring for the cherry-pick rationale.
pytest.importorskip("eawf.cli.commands.lifecycle")
pytest.importorskip("eawf.cli.commands.session")

from eawf.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.acceptance


def test_phase2_full_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the full Phase 2 happy path against a fresh state.json."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.chdir(tmp_path)

    # 1. project init
    r = runner.invoke(
        app,
        ["project", "init", "QR", "--title", "Quant Research", "--domains", "quant,research"],
    )
    assert r.exit_code == 0, r.output

    # 2. phase open auto-allocates P01
    r = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert r.exit_code == 0 and "P01" in r.stdout, r.output

    # 3. iter open
    r = runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "Iter 1"])
    assert r.exit_code == 0 and "P01-I01" in r.stdout, r.output

    # 4. wave plan + claim + close
    r = runner.invoke(
        app,
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            "P01-I01-W01",
            "--title",
            "W1",
            "--files",
            "src/foo.py",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "--json",
            "session",
            "start",
            "--role",
            "executor",
            "--scope",
            "P01-I01-W01",
            "--runtime",
            "claude",
        ],
    )
    assert r.exit_code == 0, r.output
    sid = json.loads(r.stdout)["id"]
    r = runner.invoke(app, ["wave", "claim", "P01-I01-W01", "--session", sid])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "wave",
            "close",
            "P01-I01-W01",
            "--outcome",
            "ok",
        ],
    )
    assert r.exit_code == 0, r.output

    # 5. close iter + phase so all three closure timestamps are observable
    r = runner.invoke(app, ["iter", "close", "P01-I01", "--audit", "AUD-IT-1"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app,
        [
            "decision",
            "add",
            "D001",
            "--scope-id",
            "P01",
            "--summary",
            "P01 scope collapse: finish as single-wave phase",
            "--rationale",
            "scope collapse accepted for minimal lifecycle scenario",
            "--alternative",
            "plan a second wave",
        ],
    )
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["phase", "close", "P01", "--audit", "AUD-PH-1"])
    assert r.exit_code == 0, r.output

    # 6. status returns current pointers + last-closed wave
    r = runner.invoke(app, ["--json", "status"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    # current phase pointer is cleared once the phase closes
    assert payload["current"]["phase_id"] is None
    assert "P01-I01-W01" in payload.get("last_closed_waves", [])

    # 7. closure timestamps land on disk for the wave, iter, and phase
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["waves"]["P01-I01-W01"]["closed_at"] is not None
    assert state["iters"]["P01-I01"]["closed_at"] is not None
    assert state["phases"]["P01"]["closed_at"] is not None
