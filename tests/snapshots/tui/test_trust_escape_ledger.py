"""Escape-ledger readout in the Trust mode (P29-I13-W09).

The Trust mode (digit ``4``) gains an ESCAPE LEDGER section: one row per
escaped (operator-waived) criterion with its waiver reason
(:func:`~eawf.surfaces.tui.modes.trust.build_escape_ledger` over the evidence
rows whose ``status`` is ``waived``). An escape is a criterion cleared by a
waiver rather than a passing gate -- the verdict the trust surface must
always surface so a waived criterion never hides behind a green close.

Honest-empty (no waivers) is the COMMON, desired path: a clean ledger
renders the muted :data:`~eawf.surfaces.tui.modes.trust.NO_ESCAPES_NOTICE`.

The measurable signal is the snapshot assertion: the escape-ledger readout
lists each escaped criterion with its waiver reason.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_trust_escape_ledger.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.trust import (
    NO_ESCAPES_NOTICE,
    EscapedCriterion,
    TrustModeScreen,
    build_escape_ledger,
    render_escape_ledger,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"
_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def _evidence(*, suffix: str, scope_id: str, status: str, summary: str) -> EvidenceRecord:
    """Build one evidence row with the given status + summary."""
    return EvidenceRecord(
        id=f"EV-{suffix}",
        scope_id=scope_id,
        produced_by="human" if status == "waived" else "tool",
        evidence_kind="attested" if status == "waived" else "deterministic",
        status=status,  # type: ignore[arg-type]
        summary=summary,
        created_at=_NOW,
    )


def _records_with_waivers() -> tuple[EvidenceRecord, ...]:
    """Two waived rows plus one passing row (only the waivers escape)."""
    return (
        _evidence(
            suffix="aaaaaaaaaaaa",
            scope_id="P29-I13-W03",
            status="waived",
            summary="flaky network gate waived by operator",
        ),
        _evidence(
            suffix="bbbbbbbbbbbb",
            scope_id="P29-I13-W07",
            status="pass",
            summary="pytest gate passed",
        ),
        _evidence(
            suffix="cccccccccccc",
            scope_id="P29-I13-W09",
            status="waived",
            summary="coverage threshold waived for spike wave",
        ),
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_build_escape_ledger_selects_only_waived_rows() -> None:
    """Only waived rows enter the ledger; a pass row is skipped."""
    escapes = build_escape_ledger(_records_with_waivers())
    assert len(escapes) == 2
    assert [e.scope_id for e in escapes] == ["P29-I13-W03", "P29-I13-W09"]
    assert escapes[0].reason == "flaky network gate waived by operator"


def test_build_escape_ledger_empty_when_no_waivers() -> None:
    """No waived row -> an empty ledger (the common, desired path)."""
    rows = (
        _evidence(
            suffix="aaaaaaaaaaaa",
            scope_id="P29-I13-W01",
            status="pass",
            summary="pytest gate passed",
        ),
    )
    assert build_escape_ledger(rows) == ()


def test_build_escape_ledger_empty_input_is_empty() -> None:
    """An empty evidence set yields an empty ledger."""
    assert build_escape_ledger(()) == ()


def test_render_escape_ledger_lists_scope_and_reason() -> None:
    """Each ledger row renders its scope id and waiver reason."""
    escapes = (EscapedCriterion(scope_id="P29-I13-W03", reason="flaky network gate waived"),)
    body = render_escape_ledger(escapes)
    assert "P29-I13-W03" in body
    assert "flaky network gate waived" in body


def test_render_escape_ledger_empty_renders_honest_notice() -> None:
    """An empty ledger renders the honest-empty no-escapes notice."""
    assert NO_ESCAPES_NOTICE in render_escape_ledger(())


def test_render_escape_ledger_caps_rows_with_overflow() -> None:
    """A ledger past the cap renders a ``+N more`` overflow line."""
    escapes = tuple(
        EscapedCriterion(scope_id=f"P29-I13-W{index:02d}", reason="waived")
        for index in range(1, 20)
    )
    body = render_escape_ledger(escapes)
    assert "+7 more" in body  # 19 escapes, cap 12 -> 7 overflow


# --------------------------------------------------------------------------
# Pilot-driven section -- pushed waivers surface the ledger
# --------------------------------------------------------------------------


def test_trust_pane_escape_ledger_lists_waived_criteria() -> None:
    """The Trust mode lists each escaped criterion with its waiver reason."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, TrustModeScreen)
            screen.set_evidence(_records_with_waivers())
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "ESCAPE LEDGER" in frame
            assert "P29-I13-W03" in frame
            assert "flaky network gate waived by operator" in frame
            assert "P29-I13-W09" in frame
            assert "coverage threshold waived for spike wave" in frame
            assert_screen_snapshot(app, _GOLDEN / "trust_escape_ledger.txt")

    asyncio.run(body())


def test_trust_pane_escape_ledger_honest_empty_with_no_waivers() -> None:
    """With no waived row the section renders the honest-empty notice."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, TrustModeScreen)
            screen.set_evidence(())
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert NO_ESCAPES_NOTICE in frame

    asyncio.run(body())
