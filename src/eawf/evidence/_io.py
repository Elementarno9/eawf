"""Internal I/O helpers shared by the evidence-area mutators.

These helpers live behind a leading-underscore module name because they are
part of the implementation contract for ``evidence/<area>.py`` only — the CLI
layer never imports them directly. They centralise:

* Loading + parsing ``state.json`` into a :class:`State`.
* Computing a stable args-hash for the event envelope.
* Building event envelopes with consistent shape.

The append helper and canonical store-path resolver live in
``eawf.kernel.store.append`` and ``eawf.kernel.store.paths`` respectively. This module
re-exports them so existing callers keep working.

The mutation pattern every CLI handler uses is::

    with state_transaction(state_path) as state:
        record, event = mutate(state, ...)  # in-place
        append_jsonl(store_paths(state_path)[StoreKind.<kind>], record)
        append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)

The :func:`eawf.cli._mutation.state_transaction` context manager owns
``portalock(state.json)`` for the entire load + mutate + validate + write
cycle; the JSONL appenders acquire sibling locks for their own files.
State and events are on different files so the locks never compete.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from eawf.cli.errors import UserError, ValidationError
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.append import append_envelope as append_jsonl
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_paths
from eawf.kernel.validate.strict import validate_state as validate_payload

logger = logging.getLogger(__name__)

__all__ = [
    "append_jsonl",
    "artifact_urn",
    "event_envelope",
    "kind_envelope",
    "load_state",
    "store_paths",
]


def load_state(state_path: Path) -> State:
    """Read *state_path* and return a typed :class:`State`.

    Raises:
        UserError: when ``state_path`` does not exist (``kind="NotFound"``).
        ValidationError: when the on-disk payload is not a valid State.
    """
    if not state_path.exists():
        raise UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(Path(state_path).read_bytes())
    report = validate_payload(payload, strict_optional=False)
    if report.state is None:
        raise ValidationError(
            "state.json failed schema validation: " + "; ".join(report.schema_errors[:3])
        )
    return report.state


def validate_or_raise(state: State) -> None:
    """Run the post-mutation validator and raise on schema/invariant errors.

    The mutated state has already been built by the caller; this helper
    re-runs the strict validator (no ``strict_optional`` because that is
    user-facing) and surfaces both schema and invariant violations under
    :class:`eawf.cli.errors.ValidationError`.
    """
    payload = json.loads(state.model_dump_json())
    report = validate_payload(payload, strict_optional=False)
    if not report.ok:
        parts: list[str] = []
        for err in report.schema_errors:
            parts.append(f"schema: {err}")
        for v in report.violations:
            parts.append(f"{v.code} at {v.path}: {v.message}")
        raise ValidationError("post-mutation validation failed: " + "; ".join(parts))


def atomic_write_state(state_path: Path, state: State) -> None:
    """Persist *state* via the LOCKED atomic writer.

    Caller MUST already hold ``portalock(state_path)``. Use this only inside
    a :func:`eawf.cli._mutation.state_transaction` (or an equivalent
    explicit ``with portalock.acquire(state_path):`` block).
    """
    from eawf.kernel.state.writer import atomic_write_json_locked

    payload = json.loads(state.model_dump_json())
    atomic_write_json_locked(state_path, payload)


def args_hash(args: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of *args* for event envelopes.

    Keys are sorted before hashing so equivalent calls hash identically. The
    digest goes into :class:`~eawf.kernel.store.kinds.event.EventPayload.args_hash`.
    """
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def now_iso() -> str:
    """Return UTC now as an ISO-8601 string with the trailing ``+00:00``."""
    return datetime.now(UTC).isoformat()


def event_envelope(
    *,
    event_id: str,
    scope_id: str | None,
    event_type: str,
    actor: str,
    command: str,
    args: dict[str, Any],
    summary: str,
    status: str = "ok",
    message: str = "",
    artifact_ids: list[str] | None = None,
) -> Envelope:
    """Construct an :class:`Envelope` for the events.jsonl stream."""
    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload={
            "timestamp": now.isoformat(),
            "event_type": event_type,
            "actor": actor,
            "command": command,
            "args_hash": args_hash(args),
            "before_state_version": None,
            "after_state_version": None,
            "status": status,
            "message": message,
        },
        blob_refs=[],
        artifact_ids=artifact_ids or [],
    )


def kind_envelope(
    *,
    record_id: str,
    kind: StoreKind,
    scope_id: str | None,
    summary: str,
    payload: dict[str, Any],
    artifact_ids: list[str] | None = None,
    blob_refs: list[str] | None = None,
) -> Envelope:
    """Construct a payload-bearing :class:`Envelope` (non-event store kinds)."""
    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=record_id,
        kind=kind,
        scope_id=scope_id,
        created_at=now,
        updated_at=now,
        summary=summary,
        payload=payload,
        blob_refs=blob_refs or [],
        artifact_ids=artifact_ids or [],
    )


def artifact_urn(scope_id: str, artifact_id: str) -> str:
    """Build a canonical ``urn:eawf:v1:artifact:<scope_id>/<id>`` URN."""
    return build_urn("artifact", owner=scope_id, id=artifact_id)
