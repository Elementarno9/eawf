"""Each redraw renders synchronously and falls back to the cached line.

The render runs under a 250 ms budget on a daemon thread. A render that
finishes in time is printed and cached, never the session's older line; one
that raises or overruns prints the cached line and starts the detached
prewarm. The budget is driven by a fake clock and a render held on an event,
never by the host clock.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest

import eawf.__main__ as entry
from eawf.observability.logging import scrub
from eawf.runtime.runtimes.claude import statusline
from eawf.runtime.runtimes.claude.statusline_theme import render_key
from eawf.runtime.statusline_cache import StatuslineCache

_PAYLOAD: dict[str, Any] = {"session_id": "ses-sync", "model": {"display_name": "Opus 5.5"}}
_RAW = json.dumps(_PAYLOAD)


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "ansi")
    monkeypatch.setattr(scrub, "configure_cli_logging", lambda: None)
    return root


def _seed(cache_root: Path, value: str, *, key: str | None = None) -> Path:
    """Write a stale entry for the fixture session under the redraw's render key."""
    path = cache_root / "ses-sync.json"
    cache_root.mkdir(parents=True, exist_ok=True)
    key = render_key(None, os.environ) if key is None else key
    path.write_bytes(orjson.dumps({"value": value, "written_at": 0.0, "key": key}))
    return path


class _Spawns:
    """Records each prewarm the redraw starts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.stamps: list[float] = []

    def __call__(self, raw: str, *, stamp: float, **kwargs: Any) -> None:
        self.calls.append((raw, kwargs))
        self.stamps.append(stamp)


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> _Spawns:
    spy = _Spawns()
    monkeypatch.setattr(statusline, "_spawn_refresh", spy)
    return spy


class _FakeClock:
    """Stands in for the budget wait: it advances by the timeout and reports an overrun."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []
        #: When given, a wait after the first sets it, releasing a held
        #: render, and lasts until the render ends.
        self.release_on_later_wait: threading.Event | None = None

    def __call__(self, done: threading.Event, budget_seconds: float) -> bool:
        self.waits.append(budget_seconds)
        self.now += budget_seconds
        if self.release_on_later_wait is not None and len(self.waits) > 1:
            self.release_on_later_wait.set()
            return done.wait(30)
        return done.is_set()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(statusline, "_await_render", fake)
    return fake


@pytest.fixture
def in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let every render finish inside its budget, however loaded the runner is."""
    monkeypatch.setattr(statusline, "_await_render", lambda done, _budget: done.wait(30))


@pytest.fixture
def held_render(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Make the render block until the returned event is set; set it at teardown."""
    release = threading.Event()

    def _render(*_args: Any, **_kwargs: Any) -> str:
        release.wait(30)
        return "late line"

    monkeypatch.setattr(statusline, "_render_session", _render)
    yield release
    release.set()


def _render_as(monkeypatch: pytest.MonkeyPatch, result: str | Exception) -> None:
    def _render(*_args: Any, **_kwargs: Any) -> str:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(statusline, "_render_session", _render)


def _pipe(monkeypatch: pytest.MonkeyPatch, raw: str | bytes) -> None:
    """Make *raw* the process stdin, as the bytes a pipe from the host carries."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def _print_statusline(monkeypatch: pytest.MonkeyPatch, raw: str | bytes) -> list[int]:
    """Run the console entry point on *raw*; return the exit codes it forced."""
    exits: list[int] = []
    _pipe(monkeypatch, raw)
    monkeypatch.setattr(os, "_exit", exits.append)
    assert entry._serve_statusline(["cc", "statusline"]) is True
    return exits


# ---- the 250 ms rule ---------------------------------------------------------


def test_fallback_after_250ms(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spawns: _Spawns,
    clock: _FakeClock,
    held_render: threading.Event,
) -> None:
    _seed(cache_root, "cached line")
    exits = _print_statusline(monkeypatch, _RAW)
    assert clock.waits == [0.25]
    assert clock.now == pytest.approx(0.25)
    assert capsys.readouterr().out == "cached line\n"
    assert spawns.calls == [(_RAW, {"workspace": None, "theme_name": None})]
    # The overrun render is abandoned, so the entry point exits without it.
    assert exits == [0]


def test_a_render_in_time_never_prints_the_stale_cached_line(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spawns: _Spawns,
    in_time: None,
) -> None:
    path = _seed(cache_root, "stale line")
    _render_as(monkeypatch, "fresh line")
    exits = _print_statusline(monkeypatch, _RAW)
    assert capsys.readouterr().out == "fresh line\n"
    assert spawns.calls == []
    assert exits == []
    entry_now = StatuslineCache(path).read_entry()
    assert entry_now is not None
    assert entry_now.value == "fresh line"


def test_a_render_that_raises_prints_the_cached_line_and_spawns_the_prewarm(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, in_time: None
) -> None:
    # error-path: the exception never reaches the host.
    _seed(cache_root, "cached line")
    _render_as(monkeypatch, RuntimeError("module blew up"))
    served = statusline.serve_line(_RAW, workspace=Path("ws"), theme_name="ansi")
    assert served == statusline.ServedLine("cached line", render_running=False)
    assert spawns.calls == [(_RAW, {"workspace": Path("ws"), "theme_name": "ansi"})]


def test_a_failed_render_without_a_cached_line_prints_the_model(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, in_time: None
) -> None:
    _render_as(monkeypatch, OSError("disk gone"))
    served = statusline.serve_line(_RAW, workspace=None, theme_name=None)
    assert served == statusline.ServedLine("Opus 5.5")
    assert len(spawns.calls) == 1


def test_an_overrun_without_a_cached_line_waits_for_the_render(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: _Spawns,
    clock: _FakeClock,
    held_render: threading.Event,
) -> None:
    # boundary: no entry, so the render is the only line there is.
    clock.release_on_later_wait = held_render
    served = statusline.serve_line(_RAW, workspace=None, theme_name=None)
    assert served == statusline.ServedLine("late line")
    assert clock.waits == [0.25, 3.0]
    assert spawns.calls == []
    assert (cache_root / "ses-sync.json").exists()


def test_statusline_repro_hung_render_without_a_cached_line(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: _Spawns,
    clock: _FakeClock,
    held_render: threading.Event,
) -> None:
    # A render that never ends is waited on for a bounded time, then abandoned.
    served = statusline.serve_line(_RAW, workspace=None, theme_name=None)
    assert served == statusline.ServedLine("Opus 5.5", render_running=True)
    assert clock.waits == [0.25, 3.0]
    assert len(spawns.calls) == 1


def test_an_entry_under_another_render_key_is_no_fallback(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: _Spawns,
    clock: _FakeClock,
    held_render: threading.Event,
) -> None:
    # A line drawn in another theme is never printed in this one's place.
    _seed(cache_root, "other theme", key="catppuccin-latte/nerd")
    clock.release_on_later_wait = held_render
    assert statusline.serve_line(_RAW, workspace=None, theme_name=None).text == "late line"
    assert spawns.calls == []


@pytest.mark.parametrize("raw", ["", "not json", "[1]", json.dumps({"session_id": ""})])
def test_a_payload_without_a_session_renders_on_the_callers_time(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, raw: str
) -> None:
    # boundary / error-path: empty, undecodable, non-mapping and empty-id payloads.
    monkeypatch.setattr(statusline, "render_pipeline", lambda *_a, **_k: "rendered")
    assert statusline.serve_line(raw, workspace=None, theme_name=None) == statusline.ServedLine(
        "rendered"
    )
    assert not cache_root.exists()
    assert spawns.calls == []


def test_a_sessionless_render_that_raises_prints_the_minimal_line(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns
) -> None:
    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("bad config")

    monkeypatch.setattr(statusline, "render_pipeline", _raise)
    assert statusline.serve_line("{}", workspace=None, theme_name=None).text == "eawf"
    assert spawns.calls == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"model": {"display_name": "Opus 5.5", "id": "x"}}, "Opus 5.5"),
        ({"model": {"id": "claude-opus-5-5"}}, "claude-opus-5-5"),
        ({"model": {"name": "haiku"}}, "haiku"),
        ({"model": "sonnet"}, "sonnet"),
        ({"model": ""}, "eawf"),
        ({"model": {"display_name": ""}}, "eawf"),
        ({"model": 7}, "eawf"),
        ({"model": {"display_name": ["x"]}}, "eawf"),
        ({}, "eawf"),
    ],
)
def test_minimal_line_names_the_model_or_eawf(payload: dict[str, Any], expected: str) -> None:
    assert statusline._minimal_line(payload) == expected


def test_run_with_cache_reads_stdin_and_returns_the_served_rows(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, in_time: None
) -> None:
    _render_as(monkeypatch, "fresh line")
    _pipe(monkeypatch, _RAW)
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "fresh line"


@pytest.fixture
def unmanaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real render from a directory no eawf repository holds."""
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.chdir(tmp_path)


def test_statusline_repro_non_utf8_stdin(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spawns: _Spawns,
    in_time: None,
    unmanaged: None,
) -> None:
    # error-path: a byte that is not UTF-8 is replaced, never raised to the host.
    raw = b'{"session_id": "ses-bytes", "model": {"display_name": "Opus \xff"}}'
    assert _print_statusline(monkeypatch, raw) == []
    assert capsys.readouterr().out.startswith("\x1b[")
    _pipe(monkeypatch, b"\xff\xfe")
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "eawf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "eawf"),
        ("not json", "eawf"),
        ("[1]", "eawf"),
        ('{"session_id": "ses-nan", "a": NaN}', "eawf"),
        ('{"session_id": "ses-big", "a": 99999999999999999999999}', "eawf"),
        ('{"model": null}', "eawf"),
        ('{"session_id": "ses-null", "model": null}', "eawf"),
        ('{"session_id": "ses-named", "model": {"display_name": ""}}', "eawf"),
    ],
)
def test_statusline_repro_blank_line_on_a_bad_payload(
    cache_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: _Spawns,
    in_time: None,
    unmanaged: None,
    raw: str,
    expected: str,
) -> None:
    # boundary / error-path: a payload no row can be drawn from still prints a line.
    assert statusline.serve_line(raw, workspace=None, theme_name=None).text == expected


def test_a_render_with_no_rows_prints_the_model_name(
    monkeypatch: pytest.MonkeyPatch, unmanaged: None
) -> None:
    monkeypatch.setattr(statusline, "_build_segments", lambda *_a: [])
    monkeypatch.setattr(statusline, "_build_bars", lambda *_a: [])
    payload = {"model": {"display_name": "Opus 5.5"}}
    assert statusline.render_pipeline(payload, workspace=None) == "Opus 5.5"


def test_statusline_repro_one_prewarm_per_overrun(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, in_time: None
) -> None:
    # A render that always raises spawns one prewarm per claim, not one per redraw.
    _seed(cache_root, "cached line")
    _render_as(monkeypatch, RuntimeError("always"))
    for _ in range(6):
        assert statusline.serve_line(_RAW, workspace=None, theme_name=None).text == "cached line"
    assert len(spawns.calls) == 1
    claim = cache_root / "ses-sync.refresh"
    expired = claim.stat().st_mtime - statusline._REFRESH_EXPIRY_SECONDS - 0.001
    os.utime(claim, (expired, expired))
    for _ in range(3):
        statusline.serve_line(_RAW, workspace=None, theme_name=None)
    assert len(spawns.calls) == 2


def test_the_spawned_prewarm_carries_the_payload_stamp(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, spawns: _Spawns, in_time: None
) -> None:
    _seed(cache_root, "cached line")
    _render_as(monkeypatch, RuntimeError("always"))
    monkeypatch.setattr(statusline.time, "time", lambda: 1234.5)
    statusline.serve_line(_RAW, workspace=None, theme_name=None)
    assert spawns.stamps == [1234.5]


def test_a_claim_just_inside_its_expiry_holds(cache_root: Path) -> None:
    # boundary: one tick short of the expiry still holds the slot.
    assert statusline._claim_refresh("ses-claim", now=1000.0) is True
    claim = cache_root / "ses-claim.refresh"
    os.utime(claim, (1000.0, 1000.0))
    assert statusline._claim_refresh("ses-claim", now=1009.999) is False
    assert statusline._claim_refresh("ses-claim", now=1010.0) is True
    assert claim.stat().st_mtime == pytest.approx(1010.0)


def test_no_claim_is_made_in_an_unwritable_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # error-path: the cache root is a file, so no prewarm could write either.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(blocked))
    assert statusline._claim_refresh("ses-claim", now=1.0) is False


def test_dropping_cached_lines_keeps_a_refresh_claim(cache_root: Path) -> None:
    assert statusline._claim_refresh("ses-claim", now=1.0) is True
    statusline.drop_cached_lines()
    assert (cache_root / "ses-claim.refresh").exists()
