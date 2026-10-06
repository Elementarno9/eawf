"""The statusline orchestrator serves its per-session line stale-while-revalidate.

A miss renders on the caller's time and writes the entry; a hit (fresh or
stale) renders nothing; a stale hit starts a detached ``eawf cc statusline
prewarm`` fed the same payload; the counter sidecar is written whenever a
render runs.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.runtime.runtimes.claude import statusline
from eawf.runtime.statusline_cache import StatuslineCache

_PAYLOAD = {"session_id": "ses-swr", "cost": {"api_duration_ms": 17000}}


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(root))
    return root


class _Spy:
    """Records calls to a stubbed function."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.result = result

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result


def _stdin(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | str) -> str:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    return raw


def _seed(cache_root: Path, session_id: str, value: str, written_at: float) -> Path:
    path = cache_root / f"{session_id}.json"
    cache_root.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps({"value": value, "written_at": written_at}))
    return path


@pytest.fixture
def render(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy("rendered")
    monkeypatch.setattr(statusline, "render_pipeline", spy)
    return spy


@pytest.fixture
def spawn(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy()
    monkeypatch.setattr(statusline, "_spawn_refresh", spy)
    return spy


# --- run_with_cache ----------------------------------------------------------


def test_a_miss_renders_and_writes_the_entry(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy, spawn: _Spy
) -> None:
    _stdin(monkeypatch, _PAYLOAD)
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "rendered"
    entry = StatuslineCache(cache_root / "ses-swr.json").read_entry()
    assert entry is not None
    assert entry.value == "rendered"
    assert (cache_root / "ses-swr.runtime-counters.json").exists()
    assert spawn.calls == []


def test_a_fresh_hit_renders_nothing(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy, spawn: _Spy
) -> None:
    _seed(cache_root, "ses-swr", "cached", written_at=4_000_000_000.0)
    _stdin(monkeypatch, _PAYLOAD)
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "cached"
    assert render.calls == []
    assert spawn.calls == []


def test_a_stale_hit_serves_the_old_line_and_spawns_a_refresh(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy, spawn: _Spy
) -> None:
    _seed(cache_root, "ses-swr", "old", written_at=0.0)
    raw = _stdin(monkeypatch, _PAYLOAD)
    workspace = Path("ws")
    assert statusline.run_with_cache(workspace=workspace, theme_name="ascii-fallback") == "old"
    assert render.calls == []
    assert spawn.calls == [((raw,), {"workspace": workspace, "theme_name": "ascii-fallback"})]


@pytest.mark.parametrize("payload", ["", "not json", "[1]", json.dumps({"session_id": ""})])
def test_a_payload_without_a_session_renders_uncached(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy, spawn: _Spy, payload: str
) -> None:
    # boundary / error-path: empty, undecodable, non-mapping and empty-id payloads.
    _stdin(monkeypatch, payload)
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "rendered"
    assert not cache_root.exists()
    assert spawn.calls == []


# --- serve_cached_line -------------------------------------------------------


def test_serve_cached_line_returns_none_on_a_miss(cache_root: Path, spawn: _Spy) -> None:
    assert statusline.serve_cached_line(json.dumps(_PAYLOAD), theme_name=None) is None
    assert spawn.calls == []


def test_serve_cached_line_returns_none_without_a_session(cache_root: Path) -> None:
    assert statusline.serve_cached_line("{}", theme_name=None) is None


def test_serve_cached_line_serves_a_stale_hit_and_spawns_a_refresh(
    cache_root: Path, spawn: _Spy
) -> None:
    _seed(cache_root, "ses-swr", "old", written_at=0.0)
    raw = json.dumps(_PAYLOAD)
    assert statusline.serve_cached_line(raw, theme_name="powerline") == "old"
    assert spawn.calls == [((raw,), {"workspace": None, "theme_name": "powerline"})]


# --- prewarm -----------------------------------------------------------------


def test_prewarm_overwrites_the_entry_with_a_fresh_render(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy
) -> None:
    _seed(cache_root, "ses-swr", "old", written_at=0.0)
    _stdin(monkeypatch, _PAYLOAD)
    assert statusline.prewarm(workspace=None, theme_name=None) == "rendered"
    entry = StatuslineCache(cache_root / "ses-swr.json").read_entry()
    assert entry is not None
    assert entry.value == "rendered"
    assert entry.written_at > 0.0


def test_prewarm_without_a_session_writes_nothing(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy
) -> None:
    _stdin(monkeypatch, "{}")
    assert statusline.prewarm(workspace=None, theme_name=None) == "rendered"
    assert not cache_root.exists()


# --- _spawn_refresh ----------------------------------------------------------

_Spawned = list[tuple[list[str], bytes, dict[str, Any]]]


@pytest.fixture
def popen(monkeypatch: pytest.MonkeyPatch) -> _Spawned:
    """Replace ``subprocess.Popen`` with a recorder of argv, stdin bytes and options."""
    seen: _Spawned = []

    def _fake(argv: list[str], *, stdin: Any, **kwargs: Any) -> None:
        seen.append((argv, stdin.read(), kwargs))

    monkeypatch.setattr("subprocess.Popen", _fake)
    return seen


def test_spawn_refresh_runs_prewarm_detached_with_the_payload(popen: _Spawned) -> None:
    statusline._spawn_refresh('{"session_id":"s"}', workspace=Path("ws"), theme_name="ascii")
    [(argv, stdin, kwargs)] = popen
    assert argv == [
        sys.executable,
        "-m",
        "eawf",
        "--workspace",
        "ws",
        "cc",
        "statusline",
        "prewarm",
        "--theme",
        "ascii",
    ]
    assert stdin == b'{"session_id":"s"}'
    assert kwargs["start_new_session"] is True


def test_spawn_refresh_without_workspace_or_theme(popen: _Spawned) -> None:
    statusline._spawn_refresh("", workspace=None, theme_name=None)
    [(argv, stdin, _)] = popen
    assert argv == [sys.executable, "-m", "eawf", "cc", "statusline", "prewarm"]
    assert stdin == b""


def test_spawn_refresh_swallows_a_failed_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    # error-path: the redraw still succeeds; the next stale redraw retries.
    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("fork refused")

    monkeypatch.setattr("subprocess.Popen", _refuse)
    statusline._spawn_refresh("{}", workspace=None, theme_name=None)
