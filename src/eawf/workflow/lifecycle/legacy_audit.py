"""Append-only disposition seam for invalid historical iter-audit links."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from eawf.kernel.state.enums import AuditKind, IterStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.legacy_audit_disposition import (
    LegacyAuditDisposition,
    LegacyAuditIssue,
)
from eawf.kernel.store.paths import store_path
from eawf.workflow.lifecycle._audit_acceptance import (
    ITER_CLOSE_AUDIT_CHECK_ORDER,
    assess_close_audit,
)

_LEGACY_AUDIT_ISSUE: TypeAdapter[LegacyAuditIssue] = TypeAdapter(LegacyAuditIssue)


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _audit_source_digest(state: State, audit_id: str | None) -> str | None:
    if audit_id is None:
        return None
    audit = (state.audits or {}).get(audit_id)
    if audit is None:
        return None
    payload = audit.model_dump_json(exclude_none=False).encode("utf-8")
    return _digest_bytes(payload)


def load_legacy_audit_dispositions(state_path: Path) -> list[LegacyAuditDisposition]:
    """Load valid disposition payloads; malformed rows remain visible to store validation."""
    path = store_path(state_path, StoreKind.LEGACY_AUDIT_DISPOSITION)
    if not path.exists():
        return []
    rows: list[LegacyAuditDisposition] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        if envelope.kind is not StoreKind.LEGACY_AUDIT_DISPOSITION:
            continue
        rows.append(LegacyAuditDisposition.model_validate(envelope.payload))
    return rows


def disposition_matches(
    dispositions: list[LegacyAuditDisposition],
    *,
    iter_id: str,
    audit_id: str | None,
    issue: str,
) -> bool:
    """Return whether an immutable disposition covers this exact anomaly identity."""
    return any(
        row.iter_id == iter_id and row.audit_id == audit_id and row.observed_issue == issue
        for row in dispositions
    )


def acknowledge_invalid_iter_audits(
    state_path: Path,
    *,
    reason: str,
    operator_session: str | None = None,
) -> tuple[int, int]:
    """Append dispositions for currently-invalid CLOSED iter links.

    Returns:
        ``(appended, existing)``. Replays are idempotent by anomaly identity.
    """
    raw_state = state_path.read_bytes()
    state = State.model_validate_json(raw_state)
    state_digest = _digest_bytes(raw_state)
    existing_rows = load_legacy_audit_dispositions(state_path)
    appended = 0
    existing = 0
    for iter_id, iter_row in sorted(state.iters.items()):
        if iter_row.status is not IterStatus.CLOSED:
            continue
        assessment = assess_close_audit(
            state,
            audit_id=iter_row.audit_id,
            allowed_scope_ids=frozenset({iter_id}),
            required_kind=AuditKind.EVALUATION,
            check_order=ITER_CLOSE_AUDIT_CHECK_ORDER,
            require_passing_check=True,
        )
        if assessment.issue is None:
            continue
        issue = _LEGACY_AUDIT_ISSUE.validate_python(assessment.issue.value)
        if disposition_matches(
            existing_rows,
            iter_id=iter_id,
            audit_id=iter_row.audit_id,
            issue=issue,
        ):
            existing += 1
            continue
        now = datetime.now(UTC)
        payload = LegacyAuditDisposition(
            iter_id=iter_id,
            audit_id=iter_row.audit_id,
            observed_issue=issue,
            source_state_digest=state_digest,
            source_audit_digest=_audit_source_digest(state, iter_row.audit_id),
            source_refs=[
                str(state.urn),
                *([f"urn:eawf:v1:store:audit/{iter_row.audit_id}"] if iter_row.audit_id else []),
            ],
            contradictions=[
                "historical link does not satisfy the current iter-close audit contract"
            ],
            reason=reason,
            acknowledged_at=now,
            operator_session=operator_session,
        )
        identity = json.dumps(
            {
                "iter_id": iter_id,
                "audit_id": iter_row.audit_id,
                "issue": issue,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        envelope_id = f"LAD-{hashlib.sha256(identity).hexdigest()[:16].upper()}"
        envelope = Envelope(
            schema_version="1.0",
            id=envelope_id,
            kind=StoreKind.LEGACY_AUDIT_DISPOSITION,
            scope_id=iter_id,
            created_at=now,
            updated_at=None,
            summary=f"legacy audit anomaly acknowledged for {iter_id}",
            payload=payload.model_dump(mode="json"),
            blob_refs=[],
            artifact_ids=[],
        )
        append_envelope(
            store_path(state_path, StoreKind.LEGACY_AUDIT_DISPOSITION),
            envelope,
        )
        existing_rows.append(payload)
        appended += 1
    return appended, existing


__all__ = [
    "acknowledge_invalid_iter_audits",
    "disposition_matches",
    "load_legacy_audit_dispositions",
]
