"""``evidence.*`` JSON-RPC methods: typed append to ``evidence.jsonl``.

The :func:`append` method is the daemon-canonical writer for
``<state_dir>/store/evidence.jsonl``. Callers — verify-spine gates,
attestation flows, waiver bookkeeping — proxy through this RPC so the
single-writer invariant in AGENTS rule 4 holds.

The append is **non-state**: no
:class:`~eawf.kernel.state.mutations.MutationKind` is allocated and the
daemon's WAL recovery path treats evidence rows as derivable replay
no-ops, same as event / audit appends. Downstream consumers re-validate
the row by reading the envelope back and running
``EvidenceRecord.model_validate(envelope.payload)``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.workflow.lifecycle._errors import WAIVER_MODE_DISABLED
from eawf.workflow.lifecycle.waivers import RUNTIME_ZERO_WAIVER_REF

logger = logging.getLogger(__name__)


class AppendParams(BaseModel):
    """Params for :func:`append`.

    Attributes:
        record: Serialized :class:`EvidenceRecord` fields. Validated by
            :meth:`EvidenceRecord.model_validate` before any side effect.
    """

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]


class AppendResult(BaseModel):
    """Result of :func:`append`.

    Attributes:
        id: Envelope id of the row just appended (mirrors
            :attr:`EvidenceRecord.id`).
        appended_at: ISO-8601 timestamp the daemon wrote the row.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    appended_at: str


@register("evidence.append")
async def append(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Validate *record* and append one row to ``evidence.jsonl``.

    The handler validates the input through :class:`EvidenceRecord`,
    wraps it in an :class:`Envelope` with ``kind=StoreKind.EVIDENCE``,
    and appends via :func:`eawf.kernel.store.append.append_envelope` (per-file
    portalock + fsync). The on-disk row is the single source of truth;
    no projection runs because evidence is a non-state append.

    Args:
        ctx: Server context — must carry ``state_path`` so the daemon
            can resolve ``<state_dir>/store/evidence.jsonl``.
        params: JSON-RPC params per :class:`AppendParams`.

    Returns:
        Dict matching :class:`AppendResult`.

    Raises:
        DaemonValidationError: When disabled policy rejects a gate waiver.
        ValueError: When ``params.record`` does not validate against
            :class:`EvidenceRecord`. The server maps this to
            ``-32602 invalid params``.
        RuntimeError: When ``ctx.state_path`` is unset (unit tests
            running the daemon without an on-disk store).
    """
    args = AppendParams.model_validate(params)
    record = EvidenceRecord.model_validate(args.record)
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    state_path = Path(ctx.state_path)
    runtime_only_waiver = record.refs == [RUNTIME_ZERO_WAIVER_REF]
    if record.status == "waived" and not runtime_only_waiver:
        # Defense in depth: direct RPC callers cannot bypass the CLI's
        # pre-persistence policy check with a hand-built waived row.
        from eawf.workflow.verify.readiness import load_active_waiver_mode

        config_root = (
            state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent
        )
        mode = load_active_waiver_mode(
            record.scope_id,
            None,
            repo_root=config_root,
            config_root=config_root,
        )
        if mode == "disabled":
            raise DaemonValidationError(
                f"validation_failed: {WAIVER_MODE_DISABLED}: gate waiver creation "
                f"is disabled (scope_id={record.scope_id!r})"
            )
    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    envelope = Envelope(
        id=record.id,
        kind=StoreKind.EVIDENCE,
        scope_id=record.scope_id,
        created_at=record.created_at,
        summary=record.summary,
        payload=record.model_dump(mode="json"),
    )
    append_envelope(evidence_path, envelope)
    appended_at = datetime.now(UTC).isoformat()
    # Log keys: bare ``kind`` on the store envelope (StoreKind namespaces)
    # and ``evidence_kind`` on the structured payload field per AGENTS
    # rule 17.
    logger.info(
        f"append id={record.id!r} scope={record.scope_id!r} "
        f"evidence_kind={record.evidence_kind!r} status={record.status!r}"
    )
    return AppendResult(id=record.id, appended_at=appended_at).model_dump(mode="json")


__all__ = [
    "AppendParams",
    "AppendResult",
    "append",
]
