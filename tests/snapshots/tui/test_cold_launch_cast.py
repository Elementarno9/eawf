"""Cold-launch cast golden + no-churn gate for the Home attention band (P29-I08-W20).

Records a *cold-launch* asciinema cast -- the app starting against a
POPULATED on-disk fixture -- and runs the deterministic no-churn gate the
T2 contract in ``.ea/local/research/2026-06-03-i08-uiux-validation-specs.md``
names: the opening frames carry populated rows (no flash-of-stale-empty,
per the W18 synchronous-bind fix) and the feed never goes
populated->empty->populated (no populate-then-clear churn). This is the
cast-level companion to the W19 first-DOM-pass gate: W19 instruments the
band's first rebuild to prove the flash window itself is populated, while
this gate proves the *recorded evidence* -- the cast a juror or reviewer
would watch -- shows no flash and no churn across its opening frames.

Provenance: the cast is stamped with the source commit + fixture id
(:func:`~eawf.surfaces.tui.snapshot.asciinema.write_cast`) so the evidence
records which build produced it; the stamp round-trips through
:func:`~eawf.surfaces.tui.snapshot.asciinema.read_cast_provenance`. The
commit is derived from the live ``HEAD`` rather than pinned to a literal,
so a re-recorded cast carries the build it was rendered from and no
committed golden churns on every commit.

Frame cadence: the cast is recorded at a fine inter-frame interval with
NO ``settle_screen`` first, so a real flash WOULD land in a captured
frame -- a coarse settle would coalesce the opening frames and hide a
transient empty flash before the gate could see it. The gate is meaningful
because the same recording against an honest-empty fixture yields the
empty placeholder on every frame (the discrimination check below), so a
populated-then-cleared churn could not pass unnoticed.

Jury residual (ARMED-but-IDLE): the *perceived* flash-of-empty across the
opening frames of this provenance-pinned cold-launch cast is the one thing
a deterministic frame assertion structurally cannot judge -- it is the T2
jury residual (ISO interaction-capability + reliability). The cross-vendor
band jury that would score it is built (W04/W05) and proven to discriminate
(W08/W11) but DORMANT: the ``quality`` profile that enables the band is
opt-in and not in the default enabled set, and the live ballot fn is idle.
So this module ships the deterministic no-churn gate -- the load-bearing
value -- and leaves the perceived-flash judgement to the armed-but-idle
jury rather than invoking a live ballot.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.attention import EMPTY_FEED_TEXT
from eawf.surfaces.tui.snapshot.asciinema import (
    read_cast_provenance,
    record_cast,
    write_cast,
)

#: A populated repo fixture: one OPEN high-severity incident, which the
#: attention reducer surfaces as a single ranked feed item. The base
#: active-wave fixture is legitimately empty, so a populated fixture is
#: needed for a flash (an empty frame between two populated frames) to be
#: observable at all.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO_ROOT / "fixtures" / "states" / "valid"
_POPULATED = _FIXTURES / "08-incident-open.json"
#: A truly data-starved repo: no incident, no active wave -- the honest-empty
#: case the discrimination check uses to prove the gate can tell empty apart.
_EMPTY = _FIXTURES / "01-empty-repo.json"
_SIZE = (120, 40)

#: The cold-launch fixture id stamped into the cast provenance.
_FIXTURE_ID = "repo-incident-open"

#: A substring of the seeded incident row so a frame check can confirm the
#: populated row text (not just a non-empty frame) made the cast.
_INCIDENT_TITLE_FRAGMENT = "Validate command exits 0 on invariant violations"

#: A fine cadence with NO settle first: the opening frames are captured close
#: enough together that a transient empty flash between two populated frames
#: would land in one of them. The ``pause 0.0`` steps pump the message loop
#: one turn each so successive frames sample the band as it stabilises.
_COLD_LAUNCH_SCRIPT = [("pause", "0.0")] * 5
_FRAME_MS = 10


def _head_commit() -> str:
    """Return the short ``HEAD`` SHA the cast is provenance-stamped with.

    Derives the commit from the live working tree so a re-recorded cast
    records the build it was rendered from. Falls back to a sentinel when
    git is unavailable (the stamp still round-trips; only the value is a
    placeholder) so the test never depends on a git environment.

    Returns:
        The short ``HEAD`` SHA, or ``"unknown"`` when git cannot resolve it.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def _feed_states(frames: list[tuple[float, str]]) -> list[bool]:
    """Map each cast frame to whether the attention feed is populated.

    A frame is *populated* when the seeded incident row text is present;
    *empty* (``False``) when the honest-empty placeholder
    (:data:`~eawf.surfaces.tui.attention.EMPTY_FEED_TEXT`) shows instead.

    Args:
        frames: ``(timestamp, screen_text)`` pairs from
            :func:`~eawf.surfaces.tui.snapshot.asciinema.record_cast`.

    Returns:
        One ``bool`` per frame: ``True`` populated, ``False`` empty.
    """
    return [_INCIDENT_TITLE_FRAGMENT in screen for _, screen in frames]


def _has_populate_clear_churn(populated_flags: list[bool]) -> bool:
    """Return ``True`` when the feed went populated -> empty -> populated.

    The churn defect the gate forbids: the band shows rows, then blanks to
    the empty placeholder, then re-mounts the rows -- a visible flicker. It
    is detected as a populated frame followed (not necessarily immediately)
    by an empty frame that is in turn followed by another populated frame.

    Args:
        populated_flags: Per-frame populated booleans from
            :func:`_feed_states`.

    Returns:
        ``True`` when a populated -> empty -> populated transition exists.
    """
    seen_populated = False
    seen_empty_after_populated = False
    for is_populated in populated_flags:
        if is_populated:
            if seen_empty_after_populated:
                return True
            seen_populated = True
        elif seen_populated:
            seen_empty_after_populated = True
    return False


def _record_cold_launch(state_path: Path) -> list[tuple[float, str]]:
    """Record a cold-launch cast against *state_path* at a fine cadence.

    Deliberately captures WITHOUT a ``settle_screen`` first -- the initial
    frame is taken at pilot entry and one per ``pause`` step -- so the
    opening frames sample the band before and as it stabilises, the window a
    real flash would land in. The DOM-rebuild worker is drained inside
    ``record_cast``'s ``pause`` steps so the capture is deterministic.

    Args:
        state_path: The on-disk fixture the app cold-launches against.

    Returns:
        The recorded ``(timestamp, screen_text)`` frames.
    """

    async def body() -> list[tuple[float, str]]:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            return await record_cast(pilot, _COLD_LAUNCH_SCRIPT, frame_ms=_FRAME_MS)

    return asyncio.run(body())


# --------------------------------------------------------------------------
# (a) No flash-of-stale-empty: the first populated frame already has rows
# --------------------------------------------------------------------------


def test_cold_launch_cast_first_frame_is_populated() -> None:
    # The cast's FIRST captured frame -- the app at cold launch against a
    # populated fixture -- already carries the incident row, never the
    # honest-empty placeholder. Pre-W18 the band composed while app.state
    # was None, so the opening frame could show the empty Static (the
    # flash); the synchronous bind closes that window.
    frames = _record_cold_launch(_POPULATED)
    _, first_screen = frames[0]
    assert _INCIDENT_TITLE_FRAGMENT in first_screen
    assert EMPTY_FEED_TEXT not in first_screen


def test_cold_launch_cast_every_frame_is_populated() -> None:
    # No flash anywhere in the opening window: every captured frame carries
    # the populated row -- there is no single empty frame for a juror's eye
    # to catch as a flash-of-stale-empty.
    frames = _record_cold_launch(_POPULATED)
    populated_flags = _feed_states(frames)
    assert all(populated_flags), f"a frame showed the empty feed: {populated_flags}"


# --------------------------------------------------------------------------
# (b) No populate-then-clear churn across the cast frames
# --------------------------------------------------------------------------


def test_cold_launch_cast_has_no_populate_then_clear_churn() -> None:
    # The feed never goes populated -> empty -> populated across the cast: it
    # mounts the rows once and holds them, so there is no populate-then-clear
    # flicker in the recorded evidence.
    frames = _record_cold_launch(_POPULATED)
    populated_flags = _feed_states(frames)
    assert not _has_populate_clear_churn(populated_flags), (
        f"populate->empty->populate churn in cast frames: {populated_flags}"
    )


def test_churn_detector_flags_a_planted_populate_clear_sequence() -> None:
    # The no-churn assertion is meaningful: the detector DOES fire on a
    # planted populated -> empty -> populated sequence (the refute-first
    # direction), so a real churn could not slip past the gate above.
    assert _has_populate_clear_churn([True, True, False, True]) is True
    # A clean always-populated run does not trip it.
    assert _has_populate_clear_churn([True, True, True]) is False
    # An honest cold-start that populates and stays populated is not churn.
    assert _has_populate_clear_churn([False, True, True]) is False


# --------------------------------------------------------------------------
# Discrimination: the same recording against an empty fixture is honest-empty
# --------------------------------------------------------------------------


def test_cold_launch_cast_empty_fixture_is_honestly_empty() -> None:
    # The gate can tell populated from empty: cold-launching against a
    # data-starved repo yields the honest-empty placeholder on every frame
    # (never the incident row), so the populated-frame assertions above are
    # not vacuously true for any cast.
    frames = _record_cold_launch(_EMPTY)
    for _, screen in frames:
        assert EMPTY_FEED_TEXT in screen
        assert _INCIDENT_TITLE_FRAGMENT not in screen


# --------------------------------------------------------------------------
# Provenance: the cast is stamped with the source commit + fixture id
# --------------------------------------------------------------------------


def test_cold_launch_cast_is_provenance_stamped(tmp_path: Path) -> None:
    # The recorded cold-launch cast is written with its source commit +
    # fixture id embedded, and the stamp round-trips -- so the evidence
    # records which build produced it and a stale / forged frame is
    # deterministically detectable before any juror sees it.
    frames = _record_cold_launch(_POPULATED)
    commit = _head_commit()
    cast_path = tmp_path / "cold_launch.cast"
    write_cast(
        frames,
        cast_path,
        title="eawf TUI cold launch",
        source_commit=commit,
        fixture_id=_FIXTURE_ID,
    )

    source_commit, fixture_id = read_cast_provenance(cast_path)
    assert source_commit == commit
    assert fixture_id == _FIXTURE_ID
    # The stamped cast carries the populated row in its frames -- the
    # evidence on disk matches the in-process assertions above.
    assert _INCIDENT_TITLE_FRAGMENT in cast_path.read_text(encoding="utf-8")
