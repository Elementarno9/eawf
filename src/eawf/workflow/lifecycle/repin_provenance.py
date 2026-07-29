"""Append-only provenance writer for historical commit re-pins."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.commit_repin import CommitRepinProvenance
from eawf.kernel.store.paths import store_path
from eawf.workflow.lifecycle.wave_sha import RepairAction


def _envelope_id(action: RepairAction, status: str) -> str:
    identity = (
        f"{action.wave_id}\0{action.old_commit or ''}\0{action.new_commit}\0{status}"
    ).encode()
    return f"CRP-{hashlib.sha256(identity).hexdigest()[:16].upper()}"


def append_commit_repin_provenance(
    state_path: Path,
    actions: list[RepairAction],
    *,
    status: Literal["planned", "applied"] = "applied",
) -> int:
    """Append missing deterministic intent or completion rows for re-pins."""
    path = store_path(state_path, StoreKind.COMMIT_REPIN)
    existing: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.add(Envelope.model_validate_json(line).id)
    appended = 0
    for action in actions:
        envelope_id = _envelope_id(action, status)
        if envelope_id in existing:
            continue
        now = datetime.now(UTC)
        payload = CommitRepinProvenance(
            wave_id=action.wave_id,
            old_commit=action.old_commit,
            new_commit=action.new_commit,
            commit_identity_digest=action.identity_digest,
            basis=action.basis,
            status=status,
            repaired_at=now,
        )
        append_envelope(
            path,
            Envelope(
                id=envelope_id,
                kind=StoreKind.COMMIT_REPIN,
                scope_id=action.wave_id,
                created_at=now,
                summary=f"historical commit pin repaired for {action.wave_id}",
                payload=payload.model_dump(mode="json"),
            ),
        )
        existing.add(envelope_id)
        appended += 1
    return appended


def complete_commit_repin_provenance(state_path: Path, state: State) -> int:
    """Append completion rows for durable intents already reflected in state."""
    path = store_path(state_path, StoreKind.COMMIT_REPIN)
    if not path.is_file():
        return 0
    planned: list[RepairAction] = []
    applied_ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        payload = CommitRepinProvenance.model_validate(envelope.payload)
        action = RepairAction(
            wave_id=payload.wave_id,
            kind="pinned_mismatch",
            old_commit=payload.old_commit,
            new_commit=payload.new_commit,
            identity_digest=payload.commit_identity_digest,
            basis=payload.basis,
        )
        if payload.status == "planned":
            planned.append(action)
        else:
            applied_ids.add(_envelope_id(action, "applied"))
    completed = [
        action
        for action in planned
        if _envelope_id(action, "applied") not in applied_ids
        and action.wave_id in state.waves
        and state.waves[action.wave_id].commit == action.new_commit
    ]
    return append_commit_repin_provenance(
        state_path,
        completed,
        status="applied",
    )


__all__ = [
    "append_commit_repin_provenance",
    "complete_commit_repin_provenance",
]
