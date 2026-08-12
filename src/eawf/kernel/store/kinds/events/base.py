"""Shared base + primitives for C09 typed event payload sub-classes.

:class:`TracedEventPayload` is the common base every C09 ``EventPayload``
sub-class derives from. It carries the §5.8 correlation-ID chain so the
three trace IDs flow uniformly across the runtime, session, cost, and
cache-alarm payload families:

* ``trace_request_id`` — daemon RPC request id (UUID-v4), ties
  CLI -> daemon -> projection writes.
* ``trace_wave_id`` — the ``W<NN>`` wave id; matches the state
  ``wave_id`` so subagent activity correlates to a wave.
* ``trace_attempt_id`` — per-dispatch-attempt id (UUID-v4); a new
  attempt is minted on every V5 runtime switchover.

All three are optional during the v0.3-v0.5 migration window: existing
rows + emitters that predate the trace chain stay valid; new daemon-side
emitters populate them from the dispatch envelope (or, on the subagent
side, from the ``EAWF_TRACE_*`` environment variables).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

RuntimeTriple = Literal["claude", "codex", "opencode"]
"""Closed runtime-name literal used across C09 event payloads.

Distinct from :data:`eawf.runtime.runtimes.manifest.RuntimeId` (which uses the
``"claude-code"`` plugin-manifest spelling). The event/telemetry surface
keys on the short ``"claude"`` form per the C09 spec §5.11 and the
``config/registry`` adapter choices.
"""


#: The one canonical runtime label a cost-by-runtime rollup keys on. The
#: event surface uses the short ``RuntimeTriple`` spelling (``"claude"``) while
#: sessions / actuals use the adapter-manifest id (``"claude-code"``); both name
#: the SAME runtime, so a rollup that groups a dispatch_cost row (``"claude"``)
#: with a session row (``"claude-code"``) must first pass each through
#: :func:`canonical_runtime_label` or it double-counts one runtime as two.
_RUNTIME_LABEL_CANON: dict[str, str] = {"claude": "claude-code"}


def canonical_runtime_label(label: str) -> str:
    """Return the one canonical runtime label for *label*.

    Maps the event-surface ``RuntimeTriple`` spelling (``"claude"``) onto the
    adapter-manifest id (``"claude-code"``) that sessions / actuals carry, so a
    cost-by-runtime rollup treats the two spellings of one runtime as one key.
    ``codex`` / ``opencode`` (and any already-canonical label) pass through
    unchanged.

    Args:
        label: A runtime label from either surface (event triple or adapter id).

    Returns:
        The canonical runtime label.
    """
    return _RUNTIME_LABEL_CANON.get(label, label)


#: Inverse of :data:`_RUNTIME_LABEL_CANON`: adapter-manifest id -> the short
#: event-surface ``RuntimeTriple`` spelling.
_RUNTIME_LABEL_TRIPLE: dict[str, str] = {
    canonical: triple for triple, canonical in _RUNTIME_LABEL_CANON.items()
}


def runtime_triple_label(label: str) -> str:
    """Return the event-surface ``RuntimeTriple`` spelling for *label*.

    The inverse of :func:`canonical_runtime_label`: maps the adapter-manifest
    id (``"claude-code"``) onto the short event triple (``"claude"``) so a
    consumer whose closed key set uses the triple spelling (the telemetry
    ``RuntimeName`` literal) folds rows from BOTH surfaces onto one key
    instead of double-counting one runtime as two. ``codex`` / ``opencode``
    (and any already-triple label) pass through unchanged.

    Args:
        label: A runtime label from either surface (event triple or adapter id).

    Returns:
        The event-triple runtime label.
    """
    return _RUNTIME_LABEL_TRIPLE.get(label, label)


class TracedEventPayload(BaseModel):
    """Base for C09 typed event payloads carrying the trace-ID chain.

    Attributes:
        trace_request_id: Daemon RPC request id (UUID-v4) tying
            CLI -> daemon -> projection writes for one method call.
        trace_wave_id: The ``W<NN>`` wave id; matches the state
            ``wave_id`` so dispatched-subagent activity correlates to
            its wave.
        trace_attempt_id: Per-dispatch-attempt id (UUID-v4); a new
            attempt id is minted on every V5 runtime switchover.
    """

    model_config = ConfigDict(extra="forbid")

    trace_request_id: str | None = None
    trace_wave_id: str | None = None
    trace_attempt_id: str | None = None
