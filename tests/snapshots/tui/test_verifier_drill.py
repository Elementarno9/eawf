"""Golden snapshot + drill-key resolution for the verifier drill (P29-I13-W10).

The verifier-role
:class:`~eawf.surfaces.tui.modals.verifier_drill.VerifierDrillModal` renders,
one row per scored criterion, the oracle tier that settled it and who
produced the evidence -- the verifier view over the same evidence rows the
oracle-determinism ratio reads.

This module pins both criteria of the wave:

* **CR-01 (snapshot)** -- the modal shows each scored criterion's oracle
  tier + producer (golden snapshot).
* **CR-02 (affordance parity)** -- the Trust mode advertises a ``v verifier``
  footer key that resolves to a live :class:`~textual.binding.Binding`
  opening the verifier drill modal, even in the honest-empty (no-data) mount
  the affordance gate probes. Driven here through the real key->Binding probe
  + a Pilot keypress so a green test proves the advertised key is not dead.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_verifier_drill.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modals.verifier_drill import (
    NO_VERIFIER_ROWS_NOTICE,
    VerifierDrillModal,
    render_verifier_rows,
    verifier_rows,
)
from eawf.surfaces.tui.modes.trust import TrustModeScreen
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.snapshot.behaviour_probe import ProbeStatus, record_keypress_transcript
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
_REPO_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _REPO_STATE.is_file(), f"missing snapshot fixture: {_REPO_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"
_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)
_COMMIT = "verifier-drill-test"


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
    scope_id: str,
    evidence_kind: str,
    status: str,
    produced_by: str,
    tier: int | None,
) -> EvidenceRecord:
    """Build one scored evidence row, optionally stamping the oracle tier."""
    metrics: dict[str, int | float | str] | None = None
    if tier is not None:
        metrics = {"oracle_tier": tier}
    return EvidenceRecord(
        id=f"EV-{suffix}",
        scope_id=scope_id,
        produced_by=produced_by,  # type: ignore[arg-type]
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=f"{evidence_kind} {status}",
        metrics=metrics,
        created_at=_NOW,
    )


def _records() -> tuple[EvidenceRecord, ...]:
    """Two scored rows: a tier-1 tool pass and a jury agent pass (no tier)."""
    return (
        _evidence(
            suffix="aaaaaaaaaaaa",
            scope_id="P29-I13-W01",
            evidence_kind="deterministic",
            status="pass",
            produced_by="tool",
            tier=1,
        ),
        _evidence(
            suffix="bbbbbbbbbbbb",
            scope_id="P29-I13-W08",
            evidence_kind="jury",
            status="pass",
            produced_by="agent",
            tier=None,
        ),
    )


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_verifier_rows_project_tier_and_producer() -> None:
    """Each scored row projects its scope, oracle tier label, and producer."""
    rows = verifier_rows(_records())
    assert len(rows) == 2
    assert rows[0].scope_id == "P29-I13-W01"
    assert rows[0].tier == "T1"
    assert rows[0].produced_by == "tool"
    # A row with no recorded tier renders the dash placeholder.
    assert rows[1].tier == "-"
    assert rows[1].produced_by == "agent"


def test_verifier_rows_skips_unscored() -> None:
    """An unscored row is excluded from the verifier rows."""
    rows = verifier_rows(
        (
            _evidence(
                suffix="cccccccccccc",
                scope_id="P29-I13-W02",
                evidence_kind="deterministic",
                status="pass",
                produced_by="tool",
                tier=2,
            ),
        )
    )
    assert len(rows) == 1


def test_verifier_rows_empty_input_is_empty() -> None:
    """No evidence rows -> no verifier rows (the honest-empty path)."""
    assert verifier_rows(()) == ()


def test_render_verifier_rows_lists_tier_and_producer() -> None:
    """The block carries each row's scope, tier, producer, and status."""
    block = render_verifier_rows(verifier_rows(_records()))
    assert "P29-I13-W01 T1 tool pass" in block
    assert "P29-I13-W08 - agent pass" in block


def test_render_verifier_rows_empty_notice() -> None:
    """No rows renders the honest-empty notice."""
    assert render_verifier_rows(()) == NO_VERIFIER_ROWS_NOTICE


# --------------------------------------------------------------------------
# CR-01: snapshot -- the modal shows the oracle tier + producer per row
# --------------------------------------------------------------------------


def test_verifier_drill_snapshot() -> None:
    """The verifier drill renders each scored row's oracle tier + producer."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            app.push_modal(VerifierDrillModal(_records()))
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "P29-I13-W01 T1 tool pass" in frame
            assert "P29-I13-W08 - agent pass" in frame
            assert_screen_snapshot(app, _GOLDEN / "verifier_drill.txt")

    asyncio.run(body())


# --------------------------------------------------------------------------
# CR-02: the advertised ``v verifier`` key resolves + opens the drill modal
# --------------------------------------------------------------------------


def test_verifier_key_advertised_in_trust_footer() -> None:
    """The Trust mode footer advertises the ``v verifier`` drill key."""
    from eawf.surfaces.tui.modes.trust import _TRUST_HINTS

    assert any(hint.startswith("v ") for hint in _TRUST_HINTS)


def test_verifier_key_resolves_in_honest_empty_trust_mode() -> None:
    """The advertised ``v`` key resolves to a live binding with NO data bound.

    Drives ``v`` through the real key->Binding probe in the same data-starved
    mount the affordance matrix uses (no evidence pushed), so the key must
    resolve (not classify UNRESOLVED) for the advertised affordance to be live.
    """

    async def body() -> ProbeStatus:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await app.switch_mode("trust")
            await settle_screen(pilot)
            transcript = await record_keypress_transcript(pilot, ["v"], source_commit=_COMMIT)
            return transcript.outcomes[0].status

    status = asyncio.run(body())
    assert status is not ProbeStatus.UNRESOLVED


def test_verifier_key_opens_modal() -> None:
    """Pressing ``v`` in the Trust mode opens the verifier drill modal."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("4")  # -> trust
            await settle_screen(pilot)
            screen = cast(TrustModeScreen, app.screen)
            screen.set_evidence(_records())
            await settle_screen(pilot)
            depth_before = app.modal_depth()
            await pilot.press("v")  # open the verifier drill
            await settle_screen(pilot)
            assert app.modal_depth() == depth_before + 1
            assert isinstance(app.screen, VerifierDrillModal)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "P29-I13-W01 T1 tool pass" in frame

    asyncio.run(body())
