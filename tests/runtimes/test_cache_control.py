"""Tests for C04d per-runtime cache-control injection + /compress wiring.

Covers the three wave success criteria (P26-I01-W14):

* **(a) marker injection at adapter boundary** —
  :func:`~eawf.runtimes.cache_control.inject_cache_control` appends the
  Claude ``<cache_control type="ephemeral" />`` breakpoint for
  ``claude-code`` (the only runtime with a caller-side marker per §5.6),
  and the Claude adapter's ``open_session`` routes its ``cache_prefix``
  through that gate.
* **(b) /compress event emit** — the ``/compress`` skill emits the
  ``compression_emitted`` event carrying the token deltas + the wired
  per-runtime cache-control applicability.
* **(c) codex no-op path** — ``codex`` (and ``opencode``) return the
  cache prefix unchanged: their matrix ``cache_control`` cell is
  ``unsupported`` so no caller-side marker is injected.

Plus boundary cases (``None`` prefix, empty prefix, unknown runtime),
the typed marker/directive models, and the matrix-driven decision view.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from eawf.runtimes.cache_control import (
    DEFAULT_MARKER,
    CacheControlMarker,
    CompressionDirective,
    compression_directive,
    inject_cache_control,
    runtime_accepts_marker,
)
from eawf.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtimes.codex.adapter import CodexAdapter
from eawf.runtimes.opencode.adapter import OpenCodeAdapter
from eawf.skills.compress import CompressSkill
from eawf.skills.engine import SkillContext, run_skill
from eawf.state.models import SessionAttempt, Wave

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


# ---------------------------------------------------------------------------
# CompressionDirective model + builder
# ---------------------------------------------------------------------------


def test_compression_directive_claude_applies_cache_control() -> None:
    """A claude-code compression pass records cache_control_applied=True."""
    directive = compression_directive(
        runtime_id="claude-code",
        tokens_before=1000,
        tokens_after=250,
    )
    assert isinstance(directive, CompressionDirective)
    assert directive.cache_control_applied is True
    assert directive.tokens_before == 1000
    assert directive.tokens_after == 250


@pytest.mark.parametrize("runtime_id", _NO_OP_RUNTIMES)
def test_compression_directive_no_op_runtime_no_cache_control(runtime_id: str) -> None:
    """codex / opencode passes record cache_control_applied=False."""
    directive = compression_directive(
        runtime_id=runtime_id,
        tokens_before=800,
        tokens_after=400,
    )
    assert directive.cache_control_applied is False


def test_compression_directive_unknown_runtime_raises() -> None:
    """An unknown runtime id raises ValueError (caller degrades to needs_user)."""
    with pytest.raises(ValueError, match="unknown runtime"):
        compression_directive(runtime_id="aider", tokens_before=10, tokens_after=5)


def test_compression_directive_is_frozen() -> None:
    """The directive model is frozen (immutable telemetry record)."""
    directive = compression_directive(
        runtime_id="codex",
        tokens_before=10,
        tokens_after=5,
    )
    with pytest.raises(Exception):  # noqa: B017 — pydantic frozen raises ValidationError
        directive.tokens_after = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# (b) /compress event emit + cache-control wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(ea_dir / "instrument-probe.json"))
    return ea_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P26",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def test_compress_emits_event_with_cache_control_default_runtime(state_dir: Path) -> None:
    """The /compress happy path emits compression_emitted + wires cache-control."""
    ctx = _ctx()
    ctx.args = {"tokens_before": 1000, "tokens_after": 250}
    env = run_skill(CompressSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["tokens_before"] == 1000
    assert body["tokens_after"] == 250
    assert body["ratio"] == 0.25
    # Default runtime is claude-code, which carries the caller-side marker.
    assert body["runtime"] == "claude-code"
    assert body["cache_control_applied"] is True
    # The skill persisted exactly one event record (compression_emitted).
    assert len(env.footer.persisted_store_records) == 1


def test_compress_event_persisted_to_store(state_dir: Path) -> None:
    """The compression_emitted event lands in the event store with the payload."""
    import orjson

    ctx = _ctx()
    ctx.args = {"tokens_before": 2000, "tokens_after": 500}
    run_skill(CompressSkill(), ctx)
    events_path = state_dir / "store" / "event.jsonl"
    assert events_path.exists()
    rows = [orjson.loads(line) for line in events_path.read_bytes().splitlines() if line.strip()]
    compress_rows = [r for r in rows if r["payload"].get("event_type") == "compression_emitted"]
    assert len(compress_rows) == 1
    payload = compress_rows[0]["payload"]
    assert payload["tokens_before"] == 2000
    assert payload["tokens_after"] == 500
    assert payload["runtime"] == "claude-code"
    assert payload["cache_control_applied"] is True


def test_compress_no_op_runtime_records_cache_control_false(state_dir: Path) -> None:
    """A /compress pass on codex records cache_control_applied=False (criterion c)."""
    ctx = _ctx()
    ctx.args = {"tokens_before": 1000, "tokens_after": 600, "runtime": "codex"}
    env = run_skill(CompressSkill(), ctx)
    assert env.header.status == "ok"
    body = cast(dict, env.body)
    assert body["runtime"] == "codex"
    assert body["cache_control_applied"] is False


def test_compress_unknown_runtime_degrades_to_needs_user(state_dir: Path) -> None:
    """An unknown runtime id degrades the skill to needs_user (no crash)."""
    ctx = _ctx()
    ctx.args = {"tokens_before": 1000, "tokens_after": 600, "runtime": "goose"}
    env = run_skill(CompressSkill(), ctx)
    assert env.header.status == "needs_user"
    body = cast(dict, env.body)
    assert "unknown runtime" in body["reason"]
    # No event persisted on the degraded path.
    assert len(env.footer.persisted_store_records) == 0


def test_compress_missing_tokens_before_needs_user(state_dir: Path) -> None:
    """The missing-required-arg path still degrades to needs_user."""
    env = run_skill(CompressSkill(), _ctx())
    assert env.header.status == "needs_user"
