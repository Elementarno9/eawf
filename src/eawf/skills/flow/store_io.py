"""``flow.jsonl`` store readers and writers for the ``/flow`` skill.

Holds the append-only ``flow.jsonl`` I/O layer of the
:mod:`eawf.skills.flow` package: the ``flow_record`` / ``flow_checkpoint``
envelope emitters and the read-only stream parsers consumed by
``eawf flow status`` / ``--resume``. The drift-detection helpers,
``compute_drift``, and the :class:`FlowSkill` runner live in the package
``__init__`` (which re-exports every name here) so the historical flat
import surface — ``from eawf.skills.flow import load_flow_records`` —
keeps resolving unchanged.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from eawf.kernel.state.enums import FlowStatus, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.kernel.store.paths import store_path
from eawf.render.envelope import SkillName

logger = logging.getLogger(__name__)


# ---- Checkpoint emission ---------------------------------------------------


def _flow_jsonl_path(state_path: Path) -> Path:
    """Return ``<state>/store/flow.jsonl``."""
    return store_path(state_path, StoreKind.FLOW)


def _new_envelope_id(prefix: str = "EV") -> str:
    """Mint a fresh ``<prefix>-<uuid12>`` envelope id."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _emit_flow_record(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    goal: str,
    policy: dict[str, Any],
    status: FlowStatus,
    last_safe_checkpoint: str | None,
    next_action: str | None,
) -> str:
    """Append a :class:`FlowPayload` (``flow_record``) envelope.

    Returns the freshly minted envelope id so the caller can fold it
    into ``persisted_store_records``.
    """
    payload = FlowPayload(
        flow_id=flow_id,
        goal=goal,
        policy=policy,
        last_safe_checkpoint=last_safe_checkpoint,
        next_action=next_action,
        status=status,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"flow: {flow_id} status={status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


def abort_flow_record(
    state_path: Path,
    *,
    scope_id: str,
    previous: FlowPayload,
    reason: str | None = None,
) -> tuple[str, FlowPayload]:
    """Append an ``abandoned`` flow_record envelope.

    Builds a :class:`FlowPayload` derived from *previous* with
    ``status = ABANDONED`` (and ``policy['abort_reason'] = reason``
    when supplied), wraps it in an :class:`Envelope`, and appends to
    the flow JSONL via :func:`append_envelope`.

    Returns ``(envelope_id, new_payload)`` so the caller can surface
    the new status and envelope id without re-reading the store.
    """
    policy: dict[str, Any] = dict(previous.policy)
    if reason is not None:
        policy["abort_reason"] = reason
    new_payload = FlowPayload(
        flow_id=previous.flow_id,
        goal=previous.goal,
        policy=policy,
        last_safe_checkpoint=previous.last_safe_checkpoint,
        next_action=None,
        status=FlowStatus.ABANDONED,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=(f"flow: {previous.flow_id} abort previous={previous.status.value}"),
        payload=new_payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id, new_payload


def _emit_checkpoint(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    step_index: int,
    step_name: SkillName,
    started_at: datetime,
    completed_at: datetime,
    last_safe: bool,
    payload_hash: str,
    parent_state_hash: str,
    parent_git_head: str | None,
    parent_profile_ids: list[str],
    args_per_step_hash: str,
) -> str:
    """Append a :class:`FlowCheckpointPayload` envelope.

    Returns the freshly minted envelope id (``EV-...``). The append
    routes through :func:`eawf.kernel.store.append.append_envelope`, so the
    line is fsynced before this function returns.
    """
    payload = FlowCheckpointPayload(
        flow_id=flow_id,
        step_index=step_index,
        step_name=step_name,
        started_at=started_at,
        completed_at=completed_at,
        last_safe=last_safe,
        payload_hash=payload_hash,
        parent_state_hash=parent_state_hash,
        parent_git_head=parent_git_head,
        parent_profile_ids=parent_profile_ids,
        args_per_step_hash=args_per_step_hash,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=completed_at,
        updated_at=None,
        summary=f"flow: {flow_id} checkpoint step_index={step_index} {step_name}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


# ---- flow.jsonl readers (read-only) ----------------------------------------


def load_flow_records(state_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Stream-parse ``flow.jsonl`` and return ``(envelope_id, payload)`` pairs.

    Records are returned in append order (oldest first). Malformed lines
    (orjson decode failure or non-flow kind) are skipped with a debug
    log so a partially-corrupted file still surfaces the parseable
    suffix.
    """
    path = _flow_jsonl_path(state_path)
    if not path.exists():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            decoded = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            logger.debug(f"load_flow_records skipping-malformed-line exc={exc}")
            continue
        if not isinstance(decoded, dict):
            continue
        envelope_id = decoded.get("id")
        payload = decoded.get("payload")
        if not isinstance(envelope_id, str) or not isinstance(payload, dict):
            continue
        out.append((envelope_id, payload))
    return out


def load_latest_records_per_flow(state_path: Path) -> dict[str, FlowPayload]:
    """Return a ``{flow_id: latest FlowPayload}`` mapping.

    Discriminator-aware: only ``kind == "flow_record"`` lines contribute
    to the mapping. A flow with no ``flow_record`` lines (only
    checkpoints) does not appear in the mapping.
    """
    out: dict[str, FlowPayload] = {}
    for _envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_record":
            continue
        try:
            record = FlowPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_records_per_flow validation-failed exc={exc}")
            continue
        out[record.flow_id] = record
    return out


def load_latest_safe_checkpoint(
    state_path: Path,
    flow_id: str,
) -> tuple[str, FlowCheckpointPayload] | None:
    """Return the latest ``(envelope_id, payload)`` with ``last_safe=True``.

    Returns ``None`` when the flow has no safe checkpoints — the runner
    treats that as "nothing to resume to" and refuses with an
    ``INTEGRITY_VIOLATION`` exit code.
    """
    safe: tuple[str, FlowCheckpointPayload] | None = None
    for envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_checkpoint":
            continue
        if payload.get("flow_id") != flow_id:
            continue
        try:
            ckpt = FlowCheckpointPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_safe_checkpoint validation-failed exc={exc}")
            continue
        if ckpt.last_safe:
            safe = (envelope_id, ckpt)
    return safe


def in_progress_flow_ids(state_path: Path) -> list[str]:
    """Return the ids of flows whose latest record status is ``in_progress``."""
    latest = load_latest_records_per_flow(state_path)
    return [fid for fid, record in latest.items() if record.status == FlowStatus.IN_PROGRESS]


def latest_active_flow_id(state_path: Path) -> str | None:
    """Return the flow_id of the most recently-appended ``flow_record`` envelope.

    Append order in ``flow.jsonl`` reflects chronological order (each
    append is single-writer), so the last seen ``flow_record`` flow_id
    is the most recently-active flow. Used by ``eawf flow status`` to
    pick a deterministic default when the operator did not pass
    ``--flow-id`` and no flow is in-progress.
    """
    out: str | None = None
    for _envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_record":
            continue
        flow_id = payload.get("flow_id")
        if isinstance(flow_id, str):
            out = flow_id
    return out


__all__ = [
    "abort_flow_record",
    "in_progress_flow_ids",
    "latest_active_flow_id",
    "load_flow_records",
    "load_latest_records_per_flow",
    "load_latest_safe_checkpoint",
]
