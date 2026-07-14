"""W31: every needs_user surface drops a CLOSED wave's stale advisory.

W26 retracted stale-wave advisories on close and filtered them out of the
attention hero, but the footer badge count, the inbox overlay, and the
auto-open modal all read ``list_open_pauses`` raw (via ``_all_open_pauses``
/ ``_open_pauses_for_state``) and bypassed the filter. These tests pin that
the app-level pause helpers now apply the same ``_pause_wave_closed``
predicate, so a closed wave's over-budget advisory never lingers in the
badge / inbox / auto-open after the wave closes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.stale_wave import StaleWave, build_stale_wave_envelope
from eawf.surfaces.tui.app import EaApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _seed_state_with_advisory(tmp_path: Path, *, wave_status: WaveStatus) -> Path:
    """Write a state whose single wave has *wave_status* + an advisory pause for it."""
    base = State.model_validate_json(_PHASE_ITER_WAVE.read_text(encoding="utf-8"))
    wave_id = next(iter(base.waves))
    wave = base.waves[wave_id].model_copy(update={"status": wave_status})
    state = base.model_copy(update={"waves": {wave_id: wave}})

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")

    plan = StaleWave(
        wave_id=wave_id,
        scope_id=state.urn,
        session="urn:eawf:v1:session:cli/SES-tui",
        anchor=_NOW,
        elapsed_seconds=9000.0,
        advisory_band="err",
        budget_minutes=60.0,
    )
    append_envelope(
        store_path(state_path, StoreKind.EVENT), build_stale_wave_envelope(plan, now=_NOW)
    )
    return state_path


def test_closed_wave_advisory_is_dropped_from_badge_and_inbox(tmp_path: Path) -> None:
    # The advisory's subject wave is CLOSED -> the badge count + inbox source
    # (_all_open_pauses) must drop it.
    state_path = _seed_state_with_advisory(tmp_path, wave_status=WaveStatus.CLOSED)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app._all_open_pauses() == []
            assert app.pending_pauses == 0

    asyncio.run(body())


def test_active_wave_advisory_is_kept(tmp_path: Path) -> None:
    # The same advisory while its wave is still IN_PROGRESS is a live signal.
    state_path = _seed_state_with_advisory(tmp_path, wave_status=WaveStatus.IN_PROGRESS)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(app._all_open_pauses()) == 1
            assert app.pending_pauses == 1

    asyncio.run(body())
