"""DispatchCost-always-recorded invariant test (P27-I01-W16).

The operator decision (D24-adjacent, C09 §5.9) is that a
:class:`~eawf.kernel.store.kinds.events.dispatch_cost.DispatchCostPayload` lands
in ``event.jsonl`` **regardless of** ``telemetry.enabled``: telemetry
gates the *projection* + the ``eawf metrics`` surface, never the raw cost
event. The cost row is part of the canonical event ledger so a later
projection (after the operator opts in) can re-roll historical cost from
events that were emitted while telemetry was off.

This test drives :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch` against a
real tmp ``event.jsonl`` while the merged layered config reports
``telemetry.enabled=false``, and asserts the ``dispatch_cost`` envelope is
present on disk. The runner's emit path consults no telemetry flag — this
test pins that contract so a future gate cannot silently suppress the cost
event.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from eawf.kernel.config import layered
from eawf.kernel.config.layered import get_dotted, merge_config
from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.models import RuntimeErrorClass
from eawf.runtime.daemon.dispatch_runner import (
    DispatchTokens,
    run_dispatch,
)
from eawf.runtime.daemon.methods import MethodContext


def _ctx(event_path: Path) -> MethodContext:
    """Build a daemon method context wired to *event_path* (no bus)."""
    return MethodContext(
        started_at="2026-05-22T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=None,
        event_path=event_path,
    )


def _read_event_types(event_path: Path) -> list[str]:
    """Return the ``event_type`` of every envelope appended to *event_path*."""
    types: list[str] = []
    with event_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            env = Envelope.model_validate_json(line)
            event_type = env.payload.get("event_type")
            if isinstance(event_type, str):
                types.append(event_type)
    return types


def test_dispatch_cost_recorded_when_telemetry_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dispatch_cost`` lands in event.jsonl even with telemetry disabled.

    The merged config defaults ``telemetry.enabled`` to ``False`` (strict-
    local opt-in). The runner emits the cost event unconditionally, so the
    on-disk ledger carries a ``dispatch_cost`` row regardless.
    """
    # Confirm the precondition against the built-in default: isolate the
    # global overlay (~/.config/eawf/config.yaml) and the env layer so a
    # developer who opted telemetry on locally cannot flip this assertion
    # (B71 -- the test must be hermetic across machines and CI alike).
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "absent-global.yaml")
    merged, _sources = merge_config(repo=tmp_path, env={})
    assert get_dotted(merged, "telemetry.enabled") is False

    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _ctx(event_path)

    result = run_dispatch(
        ctx,
        wave_id="P27-I01-W16",
        primary_runtime="claude",
        fallback_runtime="codex",
        model="claude-opus-4-7",
        pricing_version="2026.05.17",
        primary_error=None,
        tokens=DispatchTokens(
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=4000,
        ),
        cost_usd=Decimal("0.0123"),
    )

    assert not result.switched
    types = _read_event_types(event_path)
    assert "dispatch_cost" in types, types


def test_dispatch_cost_recorded_on_runtime_fallback_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A V5 fallback still records both events with telemetry disabled.

    With ``primary_error`` set the runner switches runtimes: it must emit a
    ``runtime_switched`` event AND the post-dispatch ``dispatch_cost`` —
    neither gated by ``telemetry.enabled``.
    """
    # Isolate the global overlay + env layer so the precondition reflects the
    # built-in strict-local default rather than a developer's local opt-in (B71).
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "absent-global.yaml")
    merged, _sources = merge_config(repo=tmp_path, env={})
    assert get_dotted(merged, "telemetry.enabled") is False

    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _ctx(event_path)

    result = run_dispatch(
        ctx,
        wave_id="P27-I01-W16",
        primary_runtime="claude",
        fallback_runtime="codex",
        model="codex-model",
        pricing_version="2026.05.17",
        primary_error=RuntimeErrorClass.RUNTIME_RATE_LIMIT,
        tokens=DispatchTokens(
            input_tokens=2000,
            output_tokens=900,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        cost_usd=Decimal("0.0456"),
    )

    assert result.switched
    types = _read_event_types(event_path)
    assert types.count("dispatch_cost") == 1, types
    assert "runtime_switched" in types, types
