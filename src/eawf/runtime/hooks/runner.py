"""Hook runner: register, dispatch, return :class:`HookResult` rows.

:class:`HookRunner` is the in-process registry that subcommands and
runtime adapters route :class:`HookEvent` instances through. Each
registered hook is a callable taking the event and returning either a
:class:`HookResult` (or a ``(block, output)`` tuple — see
:meth:`HookRunner.run_event`'s coercion rules below).

Design rules per Phase 4 W04 design spec §3.3 / acceptance §3:

- The runner MUST NOT propagate exceptions raised by hook callables.
  Exceptions are captured into a :class:`HookResult` with
  ``block=False`` and the ``repr(exc)`` text in ``output``. This mirrors
  :func:`eawf.workflow.skills.engine.run_skill`'s never-crash contract.
- The runner MUST dispatch hooks in registration order (stable list,
  not a set) so the order surfaced by ``eawf hook run`` matches the
  declared order in the user's config.
- Each hook's wall-clock duration is recorded in
  :attr:`HookResult.duration_ms`; tests assert ``>= 0``.
- The runner MUST NOT mutate ``state.json`` directly (rule 4). Append-only
  bookkeeping is the caller's responsibility (the CLI handler routes
  results to ``events.jsonl`` via :func:`eawf.kernel.store.append.append_envelope`).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.lock import portalock

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)


class HookResult(BaseModel):
    """One row returned by :meth:`HookRunner.run_event`.

    Attributes:
        name: Identifier of the registered hook (callable
            ``__qualname__`` if not supplied at registration time).
        block: ``True`` when the hook signalled fail-closed; the CLI
            handler maps any ``True`` to exit code 9 (``HOOK_BLOCKED``).
        output: Free-form text body — error text, log line, or summary.
            When the hook raises, this carries ``repr(exc)``.
        duration_ms: Wall-clock duration of the hook in milliseconds.
        raised: ``True`` when the hook callable raised an exception
            (informational; ``block`` is left at the default ``False``
            because an exceptional hook is a runner-level fault, not a
            policy block).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    block: bool = False
    output: str = ""
    duration_ms: float = 0.0
    raised: bool = False


# A hook callable: takes the typed :class:`HookEvent`, returns either a
# :class:`HookResult` or a ``(block, output)`` tuple. Tuple returns are
# coerced into :class:`HookResult` by the runner so simple hooks do not
# need to import the model.
HookCallable = Callable[[HookEvent], "HookResult | tuple[bool, str]"]
DaemonClientFactory = Callable[[], Any]


class HookRunner:
    """In-process registry + dispatcher for Eä hook callables.

    Usage::

        runner = HookRunner()
        runner.register(HookEventType.PRE_COMMIT, my_lint_hook, name="lint")
        results = runner.run_event(event)
        if any(r.block for r in results):
            ...

    The registry is plain Python — no portalock, no I/O. Persistence of
    fired events is the caller's concern.
    """

    def __init__(self) -> None:
        self._hooks: dict[HookEventType, list[tuple[str, HookCallable]]] = {}

    def register(
        self,
        event_type: HookEventType,
        hook: HookCallable,
        *,
        name: str | None = None,
    ) -> None:
        """Register *hook* to fire on *event_type*.

        Args:
            event_type: The :class:`HookEventType` the hook listens on.
                The hook only fires when an event of exactly this type
                is dispatched (see :meth:`run_event`).
            hook: Callable taking a :class:`HookEvent`. May return a
                :class:`HookResult` or a ``(block, output)`` tuple.
            name: Optional display name; defaults to
                ``hook.__qualname__`` (or ``"hook"`` if the callable is
                anonymous). Must be unique across all hooks for the
                same event type — duplicates raise :class:`ValueError`.
        """
        bucket = self._hooks.setdefault(event_type, [])
        resolved_name = name or getattr(hook, "__qualname__", "") or "hook"
        for existing_name, _ in bucket:
            if existing_name == resolved_name:
                raise ValueError(
                    f"hook {resolved_name!r} already registered for event {event_type.value!r}"
                )
        bucket.append((resolved_name, hook))

    def hooks_for(self, event_type: HookEventType) -> Iterable[tuple[str, HookCallable]]:
        """Yield ``(name, callable)`` pairs registered for *event_type*."""
        yield from self._hooks.get(event_type, [])

    def run_event(self, event: HookEvent) -> list[HookResult]:
        """Dispatch *event* to every hook registered for its event type.

        Hooks fire in registration order. The runner catches any
        :class:`Exception` raised by a hook and records it as
        ``HookResult(block=False, output=repr(exc), raised=True)`` so
        the dispatch loop never crashes.

        Args:
            event: The :class:`HookEvent` to dispatch.

        Returns:
            The list of :class:`HookResult` rows, one per registered
            hook (empty when no hooks are registered for the event
            type — the CLI treats this as the success path).
        """
        bucket = self._hooks.get(event.event_type, [])
        results: list[HookResult] = []
        for name, hook in bucket:
            started = time.perf_counter()
            try:
                raw = hook(event)
            except Exception as exc:
                duration_ms = (time.perf_counter() - started) * 1000.0
                logger.exception(
                    f"run_event hook={name!r} event={event.event_type.value!r} "
                    f"status=raised; hook raised during event dispatch"
                )
                results.append(
                    HookResult(
                        name=name,
                        block=False,
                        output=repr(exc),
                        duration_ms=duration_ms,
                        raised=True,
                    )
                )
                continue
            duration_ms = (time.perf_counter() - started) * 1000.0
            results.append(_coerce_result(name, raw, duration_ms))
        return results


def _default_daemon_client_factory() -> Any:
    """Return a daemon client context manager for runtime.capture."""
    from eawf.surfaces.cli._daemon_client import DaemonClient

    return DaemonClient()


def _session_end_payload(event: HookEvent) -> dict[str, Any]:
    """Return the provider payload for a runtime lifecycle event."""
    for key in (event.runtime, "claude_code", event.event_type.value):
        payload = event.payloads.get(key)
        if isinstance(payload, dict):
            return payload
    return {}


def _coerce_cost_usd_string(raw: Any) -> Decimal | None:
    """Return a string ``cost_usd`` (e.g. ``"0.82"``) as a non-negative Decimal.

    Claude Code's Stop / SessionEnd / SubagentStop hook stdin carries **no** cost
    block at all -- the counters live in the session transcript the payload
    points at (see :func:`_transcript_counters`). This coercion exists for the
    statusline-shaped payloads a wrapper may forward instead, where ``cost_usd``
    can arrive as a JSON string while
    :func:`~eawf.runtime.runtimes.claude.runtime_counters.parse_runtime_counters`
    accepts only a numeric value. Returning a :class:`~decimal.Decimal` keeps the
    value exact through the parser without touching the statusline contract. A
    non-string, malformed, non-finite, or negative value yields ``None`` so the
    field is dropped rather than crashing the fail-open hook.
    """
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _normalise_claude_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a counter-carrying hook stdin payload to the statusline parser shape.

    This is the *fallback* counter path. A real Claude Code Stop / SessionEnd /
    SubagentStop payload carries neither ``cost`` nor ``usage`` -- its counters
    are read from the transcript instead (:func:`_transcript_counters`) -- but a
    forwarding wrapper may hand the hook a payload that does carry a flat
    ``usage`` block or a string ``cost_usd`` inside ``cost``, while
    :func:`~eawf.runtime.runtimes.claude.runtime_counters.parse_runtime_counters`
    was built against the statusline shape (tokens under
    ``context_window.current_usage``, a numeric ``cost_usd``). This adapter
    bridges the two so the shared parser stays the single counter authority:

    - a flat ``usage`` mapping is lifted to ``context_window.current_usage``
      (only when the payload does not already carry a ``context_window``, so a
      genuine statusline payload passes through untouched); and
    - a string ``cost_usd`` inside ``cost`` is coerced to a numeric value.

    The function is a no-op for a payload already in the statusline shape, so
    existing statusline callers keep their behaviour.
    """
    normalised = dict(payload)

    usage = payload.get("usage")
    if isinstance(usage, dict) and not isinstance(payload.get("context_window"), dict):
        normalised["context_window"] = {"current_usage": usage}

    cost = payload.get("cost")
    if isinstance(cost, dict):
        cost_copy = dict(cost)
        if isinstance(cost_copy.get("cost_usd"), str):
            coerced = _coerce_cost_usd_string(cost_copy["cost_usd"])
            if coerced is not None:
                cost_copy["cost_usd"] = coerced
            else:
                cost_copy.pop("cost_usd", None)
        normalised["cost"] = cost_copy

    return normalised


def _transcript_counters(payload: dict[str, Any]) -> RuntimeCounters | None:
    """Aggregate the session transcript *payload* points at, when it has one.

    The Stop / SessionEnd payload's ``transcript_path`` is where Claude Code's
    runtime facts actually live (token usage, turn durations, the billed model
    id). Reading it is therefore the primary counter source; a payload without a
    usable ``transcript_path`` yields ``None`` and the caller falls back to the
    statusline-shaped parse.
    """
    from eawf.runtime.runtimes.claude.transcript_counters import aggregate_transcript_counters

    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return None
    return aggregate_transcript_counters(raw)


def _codex_lifecycle_params(
    event: HookEvent,
    payload: dict[str, Any],
    *,
    repo_root: Path | None,
) -> tuple[dict[str, Any] | None, RuntimeCounters | None]:
    """Build strict Codex lifecycle params plus exact rollout counters."""
    provider_session_id = payload.get("session_id")
    if not isinstance(provider_session_id, str) or not provider_session_id:
        return None, None

    params: dict[str, Any] = {
        "event_type": event.event_type.value,
        "provider_session_id": provider_session_id,
        "occurred_at": event.occurred_at.isoformat(),
    }
    if repo_root is not None:
        params["repo_root"] = str(repo_root)
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        params["agent_id"] = agent_id

    counters: RuntimeCounters | None = None
    if event.event_type in {HookEventType.SUBAGENT_STOP, HookEventType.SESSION_END}:
        from eawf.runtime.runtimes.codex.rollout_counters import (
            read_codex_rollout_counters,
        )

        transcript_key = (
            "agent_transcript_path"
            if event.event_type == HookEventType.SUBAGENT_STOP
            else "transcript_path"
        )
        raw_path = payload.get(transcript_key)
        if isinstance(raw_path, str) and raw_path:
            expected_session_id = (
                agent_id
                if event.event_type == HookEventType.SUBAGENT_STOP
                and isinstance(agent_id, str)
                and agent_id
                else provider_session_id
            )
            capture = read_codex_rollout_counters(
                Path(raw_path),
                expected_session_id=expected_session_id,
            )
            counters = capture.counters
            params["agent_transcript_path"] = raw_path
            params["measurement_quality"] = capture.measurement_quality.value
            params["measurement_status"] = capture.measurement_status.value
            params["measurement_reason"] = capture.measurement_reason
        else:
            params["measurement_quality"] = "unavailable"
            params["measurement_status"] = "usage_unavailable"
            params["measurement_reason"] = "missing_transcript_path"
    if counters is not None:
        params["counters"] = counters.model_dump(mode="json")
    return params, counters


def capture_codex_lifecycle(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> tuple[HookResult, dict[str, Any] | None, RuntimeCounters | None]:
    """Forward one provider-native Codex lifecycle event to the daemon."""
    if event.runtime != "codex":
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output="runtime.codex_lifecycle skipped: non-codex runtime",
            ),
            None,
            None,
        )
    payload = _session_end_payload(event)
    params, counters = _codex_lifecycle_params(event, payload, repo_root=repo_root)
    if params is None:
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output="runtime.codex_lifecycle skipped: missing session_id",
            ),
            None,
            None,
        )
    factory = daemon_client_factory or _default_daemon_client_factory
    try:
        with factory() as client:
            response = client.call("runtime.codex_lifecycle", params)
    except Exception as exc:
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output=repr(exc),
            ),
            None,
            counters,
        )
    correlated = response.get("correlated") is True
    reason = response.get("reason")
    output = (
        "runtime.codex_lifecycle ok"
        if correlated
        else f"runtime.codex_lifecycle unavailable: {reason or 'uncorrelated'}"
    )
    return (
        HookResult(
            name="runtime.codex_lifecycle",
            block=False,
            output=output,
        ),
        response,
        counters,
    )


def _capture_codex_session_end(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None,
    repo_root: Path | None,
) -> HookResult:
    lifecycle_result, response, counters = capture_codex_lifecycle(
        event,
        daemon_client_factory=daemon_client_factory,
        repo_root=repo_root,
    )
    if response is None or response.get("correlated") is not True:
        return lifecycle_result
    if counters is None:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=(f"{lifecycle_result.output}; runtime.capture skipped: usage unavailable"),
        )
    params = counters.model_dump(mode="json")
    payload = _session_end_payload(event)
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["session_id"] = session_id
    wave_id = response.get("wave_id")
    if isinstance(wave_id, str) and wave_id:
        params["wave_id"] = wave_id
    params["captured_at"] = event.occurred_at.isoformat()
    if repo_root is not None:
        params["repo_root"] = str(repo_root)
    factory = daemon_client_factory or _default_daemon_client_factory
    try:
        with factory() as client:
            client.call("runtime.capture", params)
    except Exception as exc:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=repr(exc),
        )
    return HookResult(
        name="runtime.capture",
        block=False,
        output=f"{lifecycle_result.output}; runtime.capture ok",
    )


def capture_runtime_on_session_end(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Forward parsed SESSION_END runtime counters to ``runtime.capture``.

    Counters come from the session transcript the payload points at, falling
    back to a statusline-shaped parse of the payload itself. The hook never
    blocks the source runtime: a payload with no usable counter (no readable
    transcript and no statusline block) is a clean no-op, and daemon failures
    are surfaced in a non-blocking result so Claude's Stop hook degrades like the
    statusline path.
    """
    from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters

    if event.runtime == "codex":
        return _capture_codex_session_end(
            event,
            daemon_client_factory=daemon_client_factory,
            repo_root=repo_root,
        )

    payload = _session_end_payload(event)
    counters = _transcript_counters(payload) or parse_runtime_counters(
        _normalise_claude_hook_payload(payload)
    )
    if counters is None:
        return HookResult(
            name="runtime.capture",
            block=False,
            output="runtime.capture skipped: no usable counters",
        )

    params = counters.model_dump(mode="json")
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["session_id"] = session_id
    params["captured_at"] = event.occurred_at.isoformat()
    if repo_root is not None:
        params["repo_root"] = str(repo_root)

    factory = daemon_client_factory or _default_daemon_client_factory
    try:
        with factory() as client:
            client.call("runtime.capture", params)
    except Exception as exc:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=repr(exc),
        )
    return HookResult(
        name="runtime.capture",
        block=False,
        output="runtime.capture ok",
    )


def register_runtime_capture_hooks(
    runner: HookRunner,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> None:
    """Register built-in runtime capture hooks on *runner*."""

    def _hook(event: HookEvent) -> HookResult:
        return capture_runtime_on_session_end(
            event,
            daemon_client_factory=daemon_client_factory,
            repo_root=repo_root,
        )

    def _codex_hook(event: HookEvent) -> HookResult:
        result, _response, _counters = capture_codex_lifecycle(
            event,
            daemon_client_factory=daemon_client_factory,
            repo_root=repo_root,
        )
        return result

    for event_type in (
        HookEventType.SESSION_START,
        HookEventType.SUBAGENT_START,
        HookEventType.SUBAGENT_STOP,
    ):
        runner.register(
            event_type,
            _codex_hook,
            name="runtime.codex_lifecycle",
        )
    runner.register(HookEventType.SESSION_END, _hook, name="runtime.capture")


def _coerce_result(name: str, raw: Any, duration_ms: float) -> HookResult:
    """Coerce a hook return value into a :class:`HookResult`.

    Accepts:

    - :class:`HookResult` — returned with the runner-recorded
      ``duration_ms`` overlaid (the hook can fill it but the runner is
      authoritative).
    - ``(block, output)`` tuple — built into a fresh :class:`HookResult`
      with ``name`` as supplied.
    - ``None`` — treated as ``HookResult(block=False, output="")``.

    Anything else raises :class:`TypeError`. Tests pin the accepted
    shapes so a future widening surfaces explicitly.
    """
    if isinstance(raw, HookResult):
        return raw.model_copy(update={"duration_ms": duration_ms, "name": name})
    if raw is None:
        return HookResult(name=name, block=False, output="", duration_ms=duration_ms)
    if isinstance(raw, tuple) and len(raw) == 2:
        block, output = raw
        if not isinstance(block, bool):
            raise TypeError(
                f"hook {name!r} returned tuple with non-bool first element: {type(block).__name__}"
            )
        if not isinstance(output, str):
            raise TypeError(
                f"hook {name!r} returned tuple with non-str second element: {type(output).__name__}"
            )
        return HookResult(name=name, block=block, output=output, duration_ms=duration_ms)
    raise TypeError(
        f"hook {name!r} returned unsupported type {type(raw).__name__}; "
        "expected HookResult, (bool, str) tuple, or None"
    )


# Sentinel field reference so a future ``model_dump`` style change does
# not silently break the schema_version:1.0 contract on the JSONL writer.
_HOOK_RESULT_FIELDS: frozenset[str] = frozenset(HookResult.model_fields.keys())
_HOOK_RESULT_FIELDS_EXPECTED: frozenset[str] = frozenset(
    {"name", "block", "output", "duration_ms", "raised"}
)
if _HOOK_RESULT_FIELDS != _HOOK_RESULT_FIELDS_EXPECTED:  # pragma: no cover - boot guard
    raise RuntimeError(
        "hook result fields drift; update _HOOK_RESULT_FIELDS_EXPECTED and bump the consumers"
    )


def _normalise_iso8601(value: str) -> str:
    """Normalise an ISO-8601 datetime string to a canonical UTC form.

    Pydantic's ``model_dump_json`` emits ``...Z``-suffixed strings while
    :meth:`datetime.isoformat` emits ``...+00:00``. We pick the
    ``+00:00`` form as canonical so the in-memory key (computed via
    :meth:`datetime.isoformat`) and the on-disk key (decoded from JSON)
    compare equal.
    """
    if not value:
        return value
    if value.endswith("Z"):
        return value[:-1] + "+00:00"
    return value


def _event_idempotence_key(event: HookEvent) -> tuple[str, str, str]:
    """Return the ``(event_type, scope_id, occurred_at)`` triple.

    The triple is the canonical idempotence key per Phase 4 W04
    acceptance §4. The ``occurred_at`` value is normalised through the
    ISO-8601 string form so comparing across re-loaded events is stable
    regardless of the serializer's UTC-suffix choice (``Z`` vs ``+00:00``).
    """
    return (
        event.event_type.value,
        event.scope_id,
        _normalise_iso8601(event.occurred_at.isoformat()),
    )


def append_event_idempotent(path: Path, event: HookEvent, *, timeout: float = 5.0) -> bool:
    """Append *event* to ``path`` (JSONL) iff its idempotence key is novel.

    The idempotence contract per acceptance §4: re-dispatching the same
    :class:`HookEvent` adds at most one row to ``events.jsonl`` keyed by
    ``(event_type, scope_id, occurred_at)``. The helper:

    1. Acquires the sibling :func:`eawf.runtime.lock.portalock.acquire` so
       concurrent appenders across processes serialise.
    2. Scans the existing file (if any) line-by-line, decoding the
       three idempotence-key fields only.
    3. Appends the full ``event.model_dump_json()`` payload only when
       the key is not already present.

    Returns:
        ``True`` when a new row was appended; ``False`` when the row
        was suppressed because the triple already existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _event_idempotence_key(event)
    serialised = event.model_dump_json() + "\n"
    with portalock.acquire(path, timeout=timeout):
        if path.exists():
            with path.open("rb") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        record = orjson.loads(text)
                    except orjson.JSONDecodeError:
                        # Skip malformed lines rather than abort — the
                        # writer is append-only so corruption can only
                        # come from outside our process; we still want
                        # to land idempotent semantics for our triple.
                        continue
                    existing = (
                        str(record.get("event_type", "")),
                        str(record.get("scope_id", "")),
                        _normalise_iso8601(str(record.get("occurred_at", ""))),
                    )
                    if existing == key:
                        return False
        with path.open("ab") as fh:
            fh.write(serialised.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
    return True


__all__ = [
    "DaemonClientFactory",
    "HookCallable",
    "HookResult",
    "HookRunner",
    "append_event_idempotent",
    "capture_codex_lifecycle",
    "capture_runtime_on_session_end",
    "register_runtime_capture_hooks",
]
