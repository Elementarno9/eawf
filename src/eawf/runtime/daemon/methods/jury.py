"""``jury.*`` JSON-RPC methods: the gold-label writer (P30-I23-W17).

The jury-calibration substrate needs operator ground truth, but until
this verb NO writer surface existed for ``gold_label.jsonl`` — only the
``eawf metrics jury-validation`` reader. ``jury.label`` appends a
schema-valid :class:`~eawf.observability.eval.jury_validation.GoldLabel`
row through the daemon (rule 4: the daemon is the canonical mutator for
the committed stores) and emits a matching event envelope so the label
is auditable.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    register,
    require_bound_state_root,
)

logger = logging.getLogger(__name__)

#: The plain-JSONL gold-label store the calibration reader consumes
#: (one GoldLabel JSON object per line — mirrors ``_GOLD_LABEL_STORE`` in
#: :mod:`eawf.observability.eval.jury_validation`).
_GOLD_LABEL_STORE = "gold_label.jsonl"


class LabelParams(BaseModel):
    """Params for :func:`label`.

    Attributes:
        wave_id: The wave the ground-truth label is about. Must name a
            wave present in the bound state.
        ground_truth: ``True`` = the wave was actually a good outcome.
        reason: Why the operator pinned this label (>= 20 chars, so a
            label always carries a real rationale).
        repo_root: Caller's intended repo root (the EP3 guard refuses a
            mismatch).
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    ground_truth: bool
    reason: str = Field(min_length=20, max_length=500)
    repo_root: str | None = None


class LabelResult(BaseModel):
    """Result of :func:`label`."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    ground_truth: bool
    labeled_at: str
    envelope: dict[str, Any]


@register("jury.label")
async def label(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Append an operator gold label for a wave (the calibration writer).

    Validates the target wave exists in the bound state, appends one
    :class:`~eawf.observability.eval.jury_validation.GoldLabel` JSON line
    to ``<state_dir>/store/gold_label.jsonl`` (append-only; the latest
    ``labeled_at`` per wave wins), and appends + publishes a matching
    event envelope.

    Args:
        ctx: Server context; ``ctx.state_path`` must be configured.
        params: JSON-RPC params per :class:`LabelParams`.

    Returns:
        Dict matching :class:`LabelResult`.

    Raises:
        DaemonValidationError: On a mismatched state root (EP3 guard), an
            unknown wave, or invalid params.
        RuntimeError: When ``ctx.state_path`` is unset.
    """
    from eawf.observability.eval.jury_validation import GoldLabel

    try:
        args = LabelParams.model_validate(params)
    except ValidationError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    require_bound_state_root(ctx, repo_root=args.repo_root, command="jury label")
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if args.wave_id not in (payload.get("waves") or {}):
        raise DaemonValidationError(
            f"validation_failed: unknown wave: {args.wave_id!r} (a gold label "
            "must anchor on a wave present in state)"
        )

    now = datetime.now(UTC)
    gold = GoldLabel(wave_id=args.wave_id, ground_truth=args.ground_truth, labeled_at=now)
    store = state_path.parent / "store" / _GOLD_LABEL_STORE
    store.parent.mkdir(parents=True, exist_ok=True)
    with store.open("a", encoding="utf-8") as fh:
        fh.write(gold.model_dump_json() + "\n")

    summary = (
        f"jury.label wave={args.wave_id} ground_truth={args.ground_truth} "
        f"reason={args.reason[:80]!r}"
    )
    event_payload = EventPayload(
        timestamp=now,
        event_type="jury_gold_label",
        actor="operator",
        command="jury.label",
        args_hash="",
        before_state_version=None,
        after_state_version=None,
        status="ok",
        message=summary,
        extras={
            "wave_id": args.wave_id,
            "ground_truth": args.ground_truth,
            "reason": args.reason,
        },
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=args.wave_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=event_payload,
        blob_refs=[],
        artifact_ids=[],
    )
    if ctx.event_path is not None:
        append_envelope(Path(ctx.event_path), envelope)
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id
    logger.info(summary)

    return LabelResult(
        wave_id=args.wave_id,
        ground_truth=args.ground_truth,
        labeled_at=now.isoformat(),
        envelope=envelope.model_dump(mode="json"),
    ).model_dump(mode="json")


__all__ = ["LabelParams", "LabelResult", "label"]
