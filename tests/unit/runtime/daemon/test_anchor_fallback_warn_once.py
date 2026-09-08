"""Warn-once regression for both copies of the anchor-fallback flag.

``daemon_anchor_fallback`` fires when a client omits the per-request
``repo_root`` param and the daemon has to fall back to its boot-time anchor.
The one-shot flag that keeps that warning out of the daemon log on every
subsequent call exists twice - once in the ``state.*`` context module and once
in the ``config.*`` handler module - and each copy has its own ``global``
statement, so moving or renaming one leaves the other silently spamming.

The state-side copy was already pinned end-to-end by
``tests/integration/test_daemon_cross_repo_anchor.py``; the config-side copy
had no test at all. Both are pinned here at the resolver level, so either copy
losing its flag reds.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from eawf import __version__
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext, config, state_context

pytestmark = pytest.mark.unit

_CONFIG_LOGGER = "eawf.runtime.daemon.methods.config"
_STATE_LOGGER = "eawf.runtime.daemon.methods.state_context"
_WARN_MARKER = "daemon_anchor_fallback"


def _build_ctx(tmp_path: Path, *, state_path: Path | None) -> MethodContext:
    """Build a minimal daemon context anchored at *state_path*."""
    return MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=state_path,
        idempotency_cache={},
    )


def _anchor_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if _WARN_MARKER in record.getMessage()]


def test_config_warn_once_flag_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config copy emits the fallback warning once across repeated calls."""
    monkeypatch.setattr(config, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    caplog.set_level(logging.WARNING, logger=_CONFIG_LOGGER)
    state_path = tmp_path / "repo" / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path=state_path)

    first = config._resolve_state_anchor(repo_root=None, ctx=ctx)
    second = config._resolve_state_anchor(repo_root=None, ctx=ctx)
    third = config._resolve_state_anchor(repo_root=None, ctx=ctx)

    assert first == second == third == state_path
    assert len(_anchor_warnings(caplog)) == 1


def test_config_warn_once_flag_stays_silent_for_an_explicit_repo_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: the canonical call site passes repo_root and warns never."""
    monkeypatch.setattr(config, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    caplog.set_level(logging.WARNING, logger=_CONFIG_LOGGER)
    other = tmp_path / "other-repo"
    ctx = _build_ctx(tmp_path, state_path=tmp_path / "repo" / ".ea" / "state.json")

    resolved = config._resolve_state_anchor(repo_root=str(other), ctx=ctx)

    assert resolved == other / ".ea" / "state.json"
    assert _anchor_warnings(caplog) == []


def test_config_warn_once_flag_stays_silent_without_any_anchor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary (empty): no repo_root and no boot anchor resolves to None."""
    monkeypatch.setattr(config, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    caplog.set_level(logging.WARNING, logger=_CONFIG_LOGGER)
    ctx = _build_ctx(tmp_path, state_path=None)

    assert config._resolve_state_anchor(repo_root=None, ctx=ctx) is None
    assert _anchor_warnings(caplog) == []


def test_state_context_warn_once_flag_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state-side twin of the flag keeps its own warn-once behaviour."""
    monkeypatch.setattr(state_context, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    caplog.set_level(logging.WARNING, logger=_STATE_LOGGER)
    state_path = tmp_path / "repo" / ".ea" / "state.json"
    ctx = _build_ctx(tmp_path, state_path=state_path)

    first = state_context.resolve_state_path(repo_root=None, ctx=ctx)
    second = state_context.resolve_state_path(repo_root=None, ctx=ctx)

    assert first == second == state_path
    assert len(_anchor_warnings(caplog)) == 1


def test_state_context_warn_once_flag_raises_without_any_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: the state resolver has no None to return, so it raises."""
    monkeypatch.setattr(state_context, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    ctx = _build_ctx(tmp_path, state_path=None)

    with pytest.raises(RuntimeError, match="state_path not configured"):
        state_context.resolve_state_path(repo_root=None, ctx=ctx)
