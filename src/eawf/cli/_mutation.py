"""Transactional wrapper for state-mutating CLI handlers.

Acquires the sibling lock for ``state.json``, yields the typed
:class:`~eawf.state.models.State` for the caller to mutate in place,
then validates and atomically writes it back — all under one lock
acquisition so concurrent writers serialise.

Library mutators consumed by this wrapper must:

1. Take the typed ``State`` and mutate it in place (e.g.
   ``state.goals = goals``; ``state.updated_at = now``).
2. Return any envelope/event records that the handler appends after
   the transaction commits the new state.

The handler is responsible for appending events to ``events.jsonl``
(and any kind-specific JSONL) inside the transaction body — the
helper holds ``portalock(state.json)`` only; sibling locks for
``events.jsonl`` etc. are acquired separately by the appender.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import orjson

from eawf.cli import errors as cli_errors
from eawf.lock import portalock
from eawf.state.models import State
from eawf.state.writer import atomic_write_json_locked
from eawf.validate.strict import validate_state


@contextmanager
def state_transaction(
    state_path: Path,
    *,
    timeout: float = 5.0,
) -> Iterator[State]:
    """Yield a typed :class:`State` under ``portalock(state_path)``.

    Procedure:

    1. Acquire :func:`eawf.lock.portalock.acquire` on *state_path* with
       *timeout* (default 5 s, matching the rest of the CLI).
    2. Read + decode + schema-validate the on-disk state. Schema errors
       raise :class:`~eawf.cli.errors.ValidationFailed`.
    3. ``yield`` the typed :class:`State` to the caller for in-place
       mutation.
    4. On caller success, re-validate the mutated state (schema +
       invariants). Failures raise :class:`ValidationFailed` and the
       on-disk file is left unchanged.
    5. ``atomic_write_json_locked`` persists the new payload while
       the lock is still held.

    Raises:
        NotFound: When *state_path* does not exist.
        ValidationFailed: When the loaded payload fails schema
            validation, or the post-mutation payload fails schema
            or invariant checks.
        LockConflict: When the sibling lock cannot be acquired within
            *timeout*.

    .. warning::

        This context manager is **not re-entrant**. Calling
        ``state_transaction`` from inside an already-active
        ``state_transaction`` body will deadlock — ``portalock`` (and
        ``flock`` underneath it) is non-recursive. Composition pattern:
        *outer handler opens the transaction, inner helpers receive the
        already-loaded* :class:`State` *as a parameter and return mutations
        rather than acquiring the lock themselves.*
    """
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    try:
        with portalock.acquire(state_path, timeout=timeout):
            raw = state_path.read_bytes()
            payload = orjson.loads(raw)
            report = validate_state(payload, strict_optional=False)
            if report.state is None:
                raise cli_errors.ValidationFailed(
                    "state schema invalid: " + "; ".join(report.schema_errors[:3])
                )
            state = report.state
            yield state
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise cli_errors.ValidationFailed(
                    "post-mutation schema invalid: " + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise cli_errors.ValidationFailed(
                    f"post-mutation invariants violated: {violation_codes}"
                )
            atomic_write_json_locked(state_path, new_payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.LockConflict(str(exc)) from exc
