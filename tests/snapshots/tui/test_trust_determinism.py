"""Oracle-determinism ratio section in the Trust mode (P29-I13-W08).

The Trust mode (digit ``4``) gains an ORACLE DETERMINISM section: the
oracle-determinism ratio computed over the closed criteria's evidence
rows -- deterministic-tier passes divided by total scored
(:func:`~eawf.surfaces.tui.modes.trust.compute_oracle_determinism`). A high
ratio means most closed criteria were settled by the cheapest deterministic
oracle (a code gate) rather than the (idle, in v0.5) jury or an operator
attestation.

The measurable signal is the snapshot assertion: the trust mode shows the
oracle-determinism ratio computed over closed criteria.

This module pins:

* the pure ratio helpers (no Textual mount) -- counting, the honest-empty
  unscored path, and the ratio arithmetic; and
* the Pilot-driven section -- pushing evidence rows via ``set_evidence``
  surfaces the ratio; no scored rows render the honest-empty notice.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_trust_determinism.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.trust import (
    NO_SCORED_EVIDENCE,
    OracleDeterminism,
    TrustModeScreen,
    compute_oracle_determinism,
    render_oracle_determinism,
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


def _evidence(
    *,
    suffix: str,
    evidence_kind: str,
    status: str,
) -> EvidenceRecord:
    """Build one evidence row with the given kind + status."""
    return EvidenceRecord(
        id=f"EV-{suffix}",
        scope_id="P01-I01-W01",
        produced_by="tool" if evidence_kind == "deterministic" else "agent",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=f"{evidence_kind} {status}",
        created_at=_NOW,
    )


def _mixed_records() -> tuple[EvidenceRecord, ...]:
    """Four scored rows: 3 deterministic passes + 1 jury pass (ratio 3/4)."""
    return (
        _evidence(suffix="aaaaaaaaaaaa", evidence_kind="deterministic", status="pass"),
        _evidence(suffix="bbbbbbbbbbbb", evidence_kind="deterministic", status="pass"),
        _evidence(suffix="cccccccccccc", evidence_kind="deterministic", status="pass"),
        _evidence(suffix="dddddddddddd", evidence_kind="jury", status="pass"),
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_compute_oracle_determinism_counts_deterministic_passes() -> None:
    """3 deterministic passes out of 4 scored rows -> 3/4."""
    det = compute_oracle_determinism(_mixed_records())
    assert det.deterministic_passes == 3
    assert det.total_scored == 4
    assert det.ratio == pytest.approx(0.75)


def test_compute_oracle_determinism_empty_is_unscored() -> None:
    """No rows -> zero scored, ratio None (the honest-empty path)."""
    det = compute_oracle_determinism(())
    assert det.total_scored == 0
    assert det.ratio is None


def test_compute_oracle_determinism_excludes_unscored_status() -> None:
    """A row whose status is not terminal is excluded from both sides."""
    rows = (
        _evidence(suffix="aaaaaaaaaaaa", evidence_kind="deterministic", status="pass"),
        # A jury fail is scored (denominator) but not a deterministic pass.
        _evidence(suffix="bbbbbbbbbbbb", evidence_kind="jury", status="fail"),
    )
    det = compute_oracle_determinism(rows)
    assert det.deterministic_passes == 1
    assert det.total_scored == 2
    assert det.ratio == pytest.approx(0.5)


def test_compute_oracle_determinism_deterministic_fail_not_a_pass() -> None:
    """A deterministic FAIL is scored but is not a deterministic pass."""
    rows = (_evidence(suffix="aaaaaaaaaaaa", evidence_kind="deterministic", status="fail"),)
    det = compute_oracle_determinism(rows)
    assert det.deterministic_passes == 0
    assert det.total_scored == 1
    assert det.ratio == pytest.approx(0.0)


def test_render_oracle_determinism_shows_ratio_and_fraction() -> None:
    """The section body shows the ratio percent and the raw fraction."""
    body = render_oracle_determinism(OracleDeterminism(deterministic_passes=3, total_scored=4))
    assert "75%" in body
    assert "3 / 4 scored" in body


def test_render_oracle_determinism_unscored_renders_honest_empty() -> None:
    """An unscored tally renders the honest-empty notice, not a fake 0%."""
    body = render_oracle_determinism(OracleDeterminism(deterministic_passes=0, total_scored=0))
    assert NO_SCORED_EVIDENCE in body
    assert "%" not in body


# --------------------------------------------------------------------------
# Pilot-driven section -- pushed evidence surfaces the ratio
# --------------------------------------------------------------------------


def test_trust_pane_shows_oracle_determinism_ratio() -> None:
    """The Trust mode renders the oracle-determinism ratio over the evidence."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = app.screen
            assert isinstance(screen, TrustModeScreen)
            screen.set_evidence(_mixed_records())
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "ORACLE DETERMINISM" in frame
            assert "75%" in frame
            assert "3 / 4 scored" in frame
            assert_screen_snapshot(app, _GOLDEN / "trust_determinism.txt")

    asyncio.run(body())


def test_trust_pane_determinism_honest_empty_with_no_evidence() -> None:
    """With no scored evidence the section renders the honest-empty notice."""

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
            assert NO_SCORED_EVIDENCE in frame

    asyncio.run(body())
