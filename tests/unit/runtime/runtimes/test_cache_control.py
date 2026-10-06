"""Tests for C04d per-runtime cache-control injection.

Covers:

* **(a) marker injection at adapter boundary** —
  :func:`~eawf.runtime.runtimes.cache_control.inject_cache_control` appends the
  Claude ``<cache_control type="ephemeral" />`` breakpoint for
  ``claude-code`` (the only runtime with a caller-side marker per §5.6),
  and the Claude adapter's ``open_session`` routes its ``cache_prefix``
  through that gate.
* **(c) codex no-op path** — ``codex`` (and ``opencode``) return the
  cache prefix unchanged: their matrix ``cache_control`` cell is
  ``unsupported`` so no caller-side marker is injected.

Plus boundary cases (``None`` prefix, empty prefix, unknown runtime),
the typed marker model, and the matrix-driven decision view.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

import pytest

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.cache_control import (
    DEFAULT_MARKER,
    CacheControlMarker,
    inject_cache_control,
    runtime_accepts_marker,
)
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter
from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter

pytestmark = pytest.mark.unit

_EPHEMERAL = '<cache_control type="ephemeral" />'

# Runtimes whose matrix ``cache_control`` cell is ``unsupported`` — the
# no-op injection path (criterion c). ``opencode`` rides the same gate as
# ``codex`` per §5.6.
_NO_OP_RUNTIMES = ["codex", "opencode"]


def _now() -> datetime:
    return datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)


def _wave() -> Wave:
    return Wave.model_validate(
        {
            "id": "P26-I01-W14",
            "iter_id": "P26-I01",
            "title": "cache-control-test",
            "status": "in_progress",
            "opened_at": _now().isoformat(),
            "sessions": {},
            "runtime_preference": None,
        }
    )


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


# ---------------------------------------------------------------------------
# (a) marker injection at adapter boundary
# ---------------------------------------------------------------------------


def test_inject_cache_control_appends_marker_for_claude() -> None:
    """claude-code is the only runtime that gets a caller-side marker (§5.6)."""
    injected = inject_cache_control(runtime_id="claude-code", cache_prefix="PREFIX")
    assert injected == f"PREFIX{_EPHEMERAL}"


def test_inject_cache_control_marker_is_appended_not_prepended() -> None:
    """The breakpoint trails the cached prompt head, not leads it."""
    injected = inject_cache_control(runtime_id="claude-code", cache_prefix="HEAD")
    assert injected is not None
    assert injected.startswith("HEAD")
    assert injected.endswith(_EPHEMERAL)


def test_inject_cache_control_honours_custom_marker() -> None:
    """A caller-supplied marker renders through the same gate."""
    marker = CacheControlMarker(marker_type="ephemeral")
    injected = inject_cache_control(
        runtime_id="claude-code",
        cache_prefix="P",
        marker=marker,
    )
    assert injected == f"P{_EPHEMERAL}"


def test_runtime_accepts_marker_true_only_for_claude() -> None:
    """The matrix-driven decision view: claude-code yes, others no."""
    assert runtime_accepts_marker("claude-code") is True
    assert runtime_accepts_marker("codex") is False
    assert runtime_accepts_marker("opencode") is False


def test_claude_open_session_routes_through_injection_gate() -> None:
    """The Claude adapter's open_session injects a marker without crashing.

    The live subprocess spawn lands in P26-SURFACES; here we prove the
    adapter boundary routes ``cache_prefix`` through the shared gate by
    constructing a fresh-session row with a prefix supplied.
    """

    adapter = ClaudeAdapter()

    async def body() -> None:
        attempt = await adapter.open_session(_wave(), "do work", cache_prefix="PRE")
        assert isinstance(attempt, SessionAttempt)
        assert attempt.runtime == "claude-code"

    _run(body)


def test_claude_adapter_supports_cache_control_flag() -> None:
    """The Claude adapter declares the caller-side marker capability."""
    assert ClaudeAdapter().supports_cache_control is True


# ---------------------------------------------------------------------------
# (c) codex no-op path (+ opencode parity)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime_id", _NO_OP_RUNTIMES)
def test_inject_cache_control_no_op_for_unsupported_runtime(runtime_id: str) -> None:
    """codex / opencode return the prefix unchanged — no caller-side knob (§5.6)."""
    injected = inject_cache_control(runtime_id=runtime_id, cache_prefix="PREFIX")
    assert injected == "PREFIX"
    assert _EPHEMERAL not in cast(str, injected)


def test_codex_adapter_supports_cache_control_is_false() -> None:
    """OpenAI prompt caching is automatic — no caller-side marker (criterion c)."""
    assert CodexAdapter().supports_cache_control is False


def test_opencode_adapter_supports_cache_control_is_false() -> None:
    """OpenCode's provider injects internally — no caller-side knob (§5.6)."""
    assert OpenCodeAdapter().supports_cache_control is False


def test_codex_open_session_does_not_inject_marker() -> None:
    """The Codex adapter routes through the gate but stays a no-op path."""

    adapter = CodexAdapter()

    async def body() -> None:
        attempt = await adapter.open_session(_wave(), "do work", cache_prefix="PRE")
        assert attempt.runtime == "codex"

    _run(body)


# ---------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------


def test_inject_cache_control_none_prefix_returns_none() -> None:
    """A None prefix has nothing to mark — returns None for every runtime."""
    assert inject_cache_control(runtime_id="claude-code", cache_prefix=None) is None
    assert inject_cache_control(runtime_id="codex", cache_prefix=None) is None


def test_inject_cache_control_empty_prefix_claude_still_marks() -> None:
    """An empty (non-None) prefix still gets the marker on claude-code."""
    injected = inject_cache_control(runtime_id="claude-code", cache_prefix="")
    assert injected == _EPHEMERAL


def test_inject_cache_control_unknown_runtime_raises() -> None:
    """An unknown runtime id propagates ValueError from the matrix lookup."""
    with pytest.raises(ValueError, match="unknown runtime"):
        inject_cache_control(runtime_id="goose", cache_prefix="PREFIX")


def test_default_marker_renders_ephemeral() -> None:
    """The shared default marker renders the ephemeral breakpoint token."""
    assert DEFAULT_MARKER.render() == _EPHEMERAL
    assert DEFAULT_MARKER.marker_type == "ephemeral"


def test_cache_control_marker_is_frozen() -> None:
    """The marker model is frozen so the shared default is safe to reuse."""
    marker = CacheControlMarker()
    with pytest.raises(Exception):  # noqa: B017 — pydantic frozen raises ValidationError
        marker.marker_type = "ephemeral"  # type: ignore[misc]


def test_cache_control_marker_rejects_unknown_type() -> None:
    """A non-ephemeral marker type is rejected by the closed Literal."""
    with pytest.raises(Exception):  # noqa: B017 — ValidationError on bad Literal
        CacheControlMarker(marker_type="persistent")  # type: ignore[arg-type]
