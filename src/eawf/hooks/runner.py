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
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict

from eawf.hooks.event import HookEvent, HookEventType
from eawf.lock import portalock

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

    1. Acquires the sibling :func:`eawf.lock.portalock.acquire` so
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
    "HookCallable",
    "HookResult",
    "HookRunner",
    "append_event_idempotent",
]
