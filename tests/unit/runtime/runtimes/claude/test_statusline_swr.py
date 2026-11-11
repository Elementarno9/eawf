"""The statusline orchestrator's per-session cache entry and its prewarm.

A render writes the entry and the counter sidecar; the detached ``eawf cc
statusline prewarm`` is fed the same payload. The budgeted fallback to the
cached line is pinned in ``test_statusline_sync_render``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.runtime.runtimes.claude import statusline
from eawf.runtime.runtimes.claude.statusline_theme import render_key
from eawf.runtime.statusline_cache import StatuslineCache

_PAYLOAD = {"session_id": "ses-swr", "cost": {"api_duration_ms": 17000}}


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "cache"
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EAWF_STATUSLINE_THEME", "dark")
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
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode())))
    return raw


def _seed(cache_root: Path, session_id: str, value: str, written_at: float) -> Path:
    path = cache_root / f"{session_id}.json"
    cache_root.mkdir(parents=True, exist_ok=True)
    key = render_key(None, os.environ)
    path.write_bytes(orjson.dumps({"value": value, "written_at": written_at, "key": key}))
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
    assert (entry.value, entry.key) == ("rendered", render_key(None, os.environ))
    assert entry.key.startswith("catppuccin-mocha/")
    assert (cache_root / "ses-swr.runtime-counters.json").exists()
    assert spawn.calls == []


@pytest.mark.parametrize("payload", ["", "not json", "[1]", json.dumps({"session_id": ""})])
def test_a_payload_without_a_session_renders_uncached(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy, spawn: _Spy, payload: str
) -> None:
    # boundary / error-path: empty, undecodable, non-mapping and empty-id payloads.
    _stdin(monkeypatch, payload)
    assert statusline.run_with_cache(workspace=None, theme_name=None) == "rendered"
    assert not cache_root.exists()
    assert spawn.calls == []


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


def test_a_prewarm_for_an_older_payload_keeps_the_newer_entry(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch, render: _Spy
) -> None:
    # A redraw answered a later payload while this prewarm still rendered.
    _seed(cache_root, "ses-swr", "newer", written_at=200.0)
    _stdin(monkeypatch, _PAYLOAD)
    statusline.prewarm(workspace=None, theme_name=None, payload_at=100.0)
    entry = StatuslineCache(cache_root / "ses-swr.json").read_entry()
    assert entry is not None
    assert (entry.value, entry.written_at) == ("newer", 200.0)


def test_prewarm_releases_the_refresh_claim_even_when_the_render_raises(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # error-path: a raising prewarm must not hold refreshes off until expiry.
    assert statusline._claim_refresh("ses-swr", now=1.0) is True

    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(statusline, "_render_session", _raise)
    _stdin(monkeypatch, _PAYLOAD)
    with pytest.raises(RuntimeError):
        statusline.prewarm(workspace=None, theme_name=None, payload_at=1.0)
    assert not (cache_root / "ses-swr.refresh").exists()


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
    statusline._spawn_refresh(
        '{"session_id":"s"}', workspace=Path("ws"), theme_name="ascii", stamp=12.5
    )
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
        "--payload-at",
        "12.5",
        "--theme",
        "ascii",
    ]
    assert stdin == b'{"session_id":"s"}'
    assert kwargs["start_new_session"] is True


def test_spawn_refresh_without_workspace_or_theme(popen: _Spawned) -> None:
    statusline._spawn_refresh("", workspace=None, theme_name=None, stamp=1.0)
    [(argv, stdin, _)] = popen
    assert argv == [
        sys.executable,
        "-m",
        "eawf",
        "cc",
        "statusline",
        "prewarm",
        "--payload-at",
        "1.0",
    ]
    assert stdin == b""


def test_spawn_refresh_swallows_a_failed_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    # error-path: the redraw still succeeds; the next redraw renders again.
    def _refuse(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("fork refused")

    monkeypatch.setattr("subprocess.Popen", _refuse)
    statusline._spawn_refresh("{}", workspace=None, theme_name=None, stamp=1.0)
