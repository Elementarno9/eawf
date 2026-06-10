"""Unit tests for the C06 snapshot harness primitives.

Covers the capture / normalise / golden-compare contract of
:mod:`eawf.surfaces.tui.snapshot.pilot_harness`: the ASCII-text capture, the
clock-cell neutralisation (the single non-deterministic element), the
trailing-blank-row trim, the regen escape hatch, and the drift
assertion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot.pilot_harness import (
    SNAPSHOT_REGEN_ENV,
    assert_screen_snapshot,
    capture_mockup_golden_screen_text,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
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
# normalize_snapshot — clock neutralisation (pure, no app needed)
# --------------------------------------------------------------------------


def test_normalize_snapshot_rewrites_clock_cell() -> None:
    # The breadcrumb separator glyph is intentional UI chrome (header.py).
    raw = " Eä  repo ❯ QR ❯ P01    runtime: active    16:04 UTC"  # noqa: RUF001
    out = normalize_snapshot(raw)
    assert "HH:MM UTC" in out
    assert "16:04 UTC" not in out


def test_normalize_snapshot_rewrites_every_clock_occurrence() -> None:
    raw = "header 09:30 UTC\nfooter 23:59 UTC"
    out = normalize_snapshot(raw)
    assert out == "header HH:MM UTC\nfooter HH:MM UTC"


def test_normalize_snapshot_noop_without_clock() -> None:
    raw = "no clock here\njust content"
    assert normalize_snapshot(raw) == raw


def test_normalize_snapshot_strips_daemon_degraded_banner() -> None:
    # The daemon-degraded banner is env-dependent (rendered only when the
    # daemon socket is unavailable) and embeds the runtime socket path, so it
    # is dropped so snapshots stay byte-stable across machines + leak no path.
    raw = (
        "daemon socket unavailable; polling state.json | "
        "socket=/run/user/1001/eawfd/eawfd.sock EAWF_RUNTIME_DIR=<unset>\n"
        "real content row\n"
    )
    out = normalize_snapshot(raw)
    assert "daemon socket unavailable" not in out
    assert "/run/user/1001/eawfd" not in out
    assert "real content row" in out
    assert out.endswith("\n")


# --------------------------------------------------------------------------
# capture_screen_text — real app, ASCII text + trailing-blank trim
# --------------------------------------------------------------------------


def test_capture_screen_text_returns_ascii_no_svg() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            text = capture_screen_text(app)
            # Plain text — not the SVG that export_screenshot would emit.
            assert "<svg" not in text
            assert "</svg>" not in text
            # Real chrome is present.
            assert "Eä" in text
            assert "ROADMAP" in text

    asyncio.run(body())


def test_capture_screen_text_trims_trailing_blank_rows() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            text = capture_screen_text(app)
            # No trailing blank row — last line carries content.
            assert text == text.rstrip("\n")
            assert text.splitlines()[-1].strip()

    asyncio.run(body())


def test_capture_screen_text_deterministic_across_runs() -> None:
    async def snap() -> str:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            return await settle_screen(pilot)

    async def body() -> None:
        first = await snap()
        second = await snap()
        assert first == second

    asyncio.run(body())


def test_capture_mockup_golden_screen_text_mounts_tui() -> None:
    async def body() -> None:
        text = await capture_mockup_golden_screen_text(
            scope="repo",
            state_path=_REPO_STATE,
            mode=None,
            key_sequence=[],
            size=_SIZE,
        )
        assert "Eä" in text
        assert "ROADMAP" in text
        assert "QR" in text

    asyncio.run(body())


def test_settle_screen_captures_loaded_state() -> None:
    # settle_screen must pump past the empty-scope placeholder to the
    # populated frame (the binder loads state.json asynchronously).
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            settled = await settle_screen(pilot)
            # Populated breadcrumb from the fixture, not the empty fallback.
            assert "QR" in settled
            assert "P01" in settled
            assert app.state is not None

    asyncio.run(body())


# --------------------------------------------------------------------------
# assert_screen_snapshot — regen escape hatch + drift assertion
# --------------------------------------------------------------------------


def test_assert_screen_snapshot_regen_writes_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = tmp_path / "nested" / "repo.txt"
    monkeypatch.setenv(SNAPSHOT_REGEN_ENV, "1")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            # No assertion fires under regen; the golden is created.
            assert_screen_snapshot(app, golden)

    asyncio.run(body())
    assert golden.is_file()
    assert "Eä" in golden.read_text(encoding="utf-8")


def test_assert_screen_snapshot_passes_against_fresh_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = tmp_path / "repo.txt"

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            monkeypatch.setenv(SNAPSHOT_REGEN_ENV, "1")
            assert_screen_snapshot(app, golden)  # writes
            monkeypatch.delenv(SNAPSHOT_REGEN_ENV)
            assert_screen_snapshot(app, golden)  # compares — must pass

    asyncio.run(body())


def test_assert_screen_snapshot_drift_raises(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "repo.txt"
    golden.write_text("totally different content\n", encoding="utf-8")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            with pytest.raises(AssertionError, match="snapshot drift"):
                assert_screen_snapshot(app, golden)

    asyncio.run(body())


def test_assert_screen_snapshot_drift_message_includes_unified_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regenerate the golden, then perturb a single row so the drift diff
    # surfaces the exact differing line rather than a wholesale mismatch.
    golden = tmp_path / "repo.txt"

    async def body() -> str:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            monkeypatch.setenv(SNAPSHOT_REGEN_ENV, "1")
            assert_screen_snapshot(app, golden)  # writes the true golden
            monkeypatch.delenv(SNAPSHOT_REGEN_ENV)

            rows = golden.read_text(encoding="utf-8").splitlines()
            rows[0] = "TAMPERED HEADER ROW"
            golden.write_text("\n".join(rows) + "\n", encoding="utf-8")

            with pytest.raises(AssertionError) as exc:
                assert_screen_snapshot(app, golden)
            return str(exc.value)

    message = asyncio.run(body())
    # Unified-diff hunk markers + line-tagged sides are present.
    assert "snapshot drift" in message
    assert "repo.txt (expected)" in message
    assert "repo.txt (actual)" in message
    assert "@@" in message
    assert "-TAMPERED HEADER ROW" in message
