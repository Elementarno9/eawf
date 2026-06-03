"""Unit tests for the C06 asciinema cast generator.

Covers :mod:`eawf.surfaces.tui.snapshot.asciinema`: scripted frame capture
(initial frame + one per step), synthesised deterministic timestamps,
the unknown-action error path, and the asciinema v2 cast file shape
(header line + one output event per frame, byte-stable).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot.asciinema import (
    CAST_HEIGHT,
    CAST_WIDTH,
    DEFAULT_FRAME_MS,
    PROVENANCE_KEY,
    read_cast_provenance,
    record_cast,
    write_cast,
)

_REPO_STATE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)
_SIZE = (120, 40)


# --------------------------------------------------------------------------
# record_cast — frame count, timestamps, normalisation
# --------------------------------------------------------------------------


def test_record_cast_frame_count_is_initial_plus_steps() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            script = [("press", "down"), ("pause", "0.0"), ("press", "down")]
            frames = await record_cast(pilot, script, frame_ms=50)
            # One initial frame + one per script step.
            assert len(frames) == 1 + len(script)

    asyncio.run(body())


def test_record_cast_timestamps_are_deterministic_and_monotonic() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            frames = await record_cast(
                pilot,
                [("press", "down"), ("press", "down"), ("press", "down")],
                frame_ms=50,
            )
            timestamps = [ts for ts, _ in frames]
            # Synthesised from the integer step count — no float drift.
            assert timestamps == [0.0, 0.05, 0.1, 0.15]

    asyncio.run(body())


def test_record_cast_frames_have_clock_normalised() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            frames = await record_cast(pilot, [("press", "down")])
            _, first_frame = frames[0]
            assert "HH:MM UTC" in first_frame

    asyncio.run(body())


def test_record_cast_rejects_unknown_action() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            with pytest.raises(ValueError, match="unknown cast action"):
                await record_cast(pilot, [("wiggle", "x")])

    asyncio.run(body())


def test_record_cast_empty_script_yields_single_frame() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            frames = await record_cast(pilot, [])
            assert len(frames) == 1
            assert frames[0][0] == 0.0

    asyncio.run(body())


def test_default_frame_ms_matches_q6_override() -> None:
    # Q6 OVERRIDE picks 50 ms (higher fidelity than the brief's 100 ms).
    assert DEFAULT_FRAME_MS == 50


# --------------------------------------------------------------------------
# write_cast — asciinema v2 file shape, byte-stability
# --------------------------------------------------------------------------


def test_write_cast_emits_v2_header_and_events(tmp_path: Path) -> None:
    frames = [(0.0, "frame-zero"), (0.05, "frame-one")]
    out = tmp_path / "demo.cast"
    write_cast(frames, out, title="test cast")

    lines = out.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == CAST_WIDTH
    assert header["height"] == CAST_HEIGHT
    assert header["timestamp"] == 0  # deterministic
    assert header["title"] == "test cast"
    # One output event per frame, in order.
    assert json.loads(lines[1]) == [0.0, "o", "frame-zero"]
    assert json.loads(lines[2]) == [0.05, "o", "frame-one"]


def test_write_cast_is_byte_stable(tmp_path: Path) -> None:
    frames = [(0.0, "a"), (0.05, "b")]
    first = tmp_path / "a.cast"
    second = tmp_path / "b.cast"
    write_cast(frames, first)
    write_cast(frames, second)
    assert first.read_bytes() == second.read_bytes()


def test_write_cast_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "demo.cast"
    write_cast([(0.0, "x")], out)
    assert out.is_file()


# --------------------------------------------------------------------------
# provenance stamp — round-trip, back-compat, byte-stability
# --------------------------------------------------------------------------


def test_write_cast_stamps_provenance_under_namespaced_key(tmp_path: Path) -> None:
    out = tmp_path / "demo.cast"
    write_cast([(0.0, "x")], out, source_commit="abc1234", fixture_id="repo-active")

    header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert header[PROVENANCE_KEY] == {
        "source_commit": "abc1234",
        "fixture_id": "repo-active",
    }


def test_read_cast_provenance_round_trips_source_commit_and_fixture(tmp_path: Path) -> None:
    out = tmp_path / "demo.cast"
    write_cast([(0.0, "x")], out, source_commit="deadbee", fixture_id="repo-active")

    source_commit, fixture_id = read_cast_provenance(out)
    assert source_commit == "deadbee"
    assert fixture_id == "repo-active"


def test_read_cast_provenance_returns_none_without_stamp(tmp_path: Path) -> None:
    # Back-compat: a cast written the pre-provenance way reads back as (None, None).
    out = tmp_path / "demo.cast"
    write_cast([(0.0, "x")], out)

    assert read_cast_provenance(out) == (None, None)


def test_write_cast_without_provenance_is_byte_identical_to_legacy(tmp_path: Path) -> None:
    # Omitting both provenance fields must not add the namespaced header key,
    # so already-committed goldens do not churn.
    frames = [(0.0, "a"), (0.05, "b")]
    out = tmp_path / "demo.cast"
    write_cast(frames, out)

    header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert PROVENANCE_KEY not in header


def test_read_cast_provenance_handles_partial_stamp(tmp_path: Path) -> None:
    # Only source_commit set -> fixture_id reads back None.
    out = tmp_path / "demo.cast"
    write_cast([(0.0, "x")], out, source_commit="abc1234")

    assert read_cast_provenance(out) == ("abc1234", None)


def test_read_cast_provenance_rejects_empty_header(tmp_path: Path) -> None:
    out = tmp_path / "empty.cast"
    out.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty cast header"):
        read_cast_provenance(out)
