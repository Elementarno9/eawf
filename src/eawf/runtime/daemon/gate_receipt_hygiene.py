"""Daemon-owned GateReceipt persistence and one-time legacy scrubbing."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

import orjson
from pydantic import ValidationError

from eawf.kernel.fsync import fsync_parent_dir
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import (
    GateDiagnostic,
    GateReceipt,
    LegacyGateReceipt,
    canonical_gate_digest,
)
from eawf.kernel.store.paths import store_path
from eawf.platform.scrub.scan import scan_text
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)

_DIAGNOSTIC_DIR_NAME = "gate-diagnostics"


@dataclass(frozen=True)
class GateReceiptScrubReport:
    """Outcome of one idempotent gate-receipt store scrub."""

    records_in: int
    records_out: int
    migrated_count: int
    quarantined_count: int
    changed: bool
    quarantine_path: Path | None


@dataclass(frozen=True)
class _ScrubbedRow:
    """Internal result for one legacy/safe/corrupt source row."""

    envelope: Envelope | None
    migrated: bool = False
    quarantined: bool = False
    changed: bool = False


def diagnostic_id(receipt_id: str) -> str:
    """Return deterministic local diagnostic id for one receipt."""
    return f"GD-{receipt_id}"


def diagnostic_dir(state_path: Path, receipt_id: str) -> Path:
    """Return owner-local diagnostic directory containing one receipt."""
    del receipt_id
    return state_path.parent / "local" / _DIAGNOSTIC_DIR_NAME


def diagnostic_path(state_path: Path, receipt_id: str) -> Path:
    """Return typed local diagnostic path for one receipt."""
    return diagnostic_dir(state_path, receipt_id) / f"{receipt_id}.json"


def diagnostic_log_path(state_path: Path, receipt_id: str) -> Path:
    """Return copied raw-output path for one receipt."""
    return diagnostic_dir(state_path, receipt_id) / f"{receipt_id}.log"


def diagnostic_digest(diagnostic: GateDiagnostic) -> str:
    """Return canonical SHA-256 digest for a typed local diagnostic."""
    payload = orjson.dumps(diagnostic.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_bytes_locked(path: Path, payload: bytes) -> None:
    """Atomically replace one local file while caller holds its sibling lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.parent.parent.name == _DIAGNOSTIC_DIR_NAME:
        os.chmod(path.parent.parent, 0o700)
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("wb") as handle:
            os.chmod(tmp, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_parent_dir(path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_local_file(path: Path, payload: bytes) -> None:
    """Write one local artifact atomically, refusing content collisions."""
    with portalock.acquire(path, timeout=5.0):
        if path.is_file():
            existing = path.read_bytes()
            if existing == payload:
                os.chmod(path, 0o600)
                return
            raise ValueError(f"gate diagnostic collision: {path.name!r}")
        _atomic_write_bytes_locked(path, payload)


def write_gate_diagnostic(
    state_path: Path,
    diagnostic: GateDiagnostic,
    *,
    log_bytes: bytes | None,
) -> str:
    """Persist typed raw observations below gitignored local storage.

    Returns:
        Canonical diagnostic content digest stored by the committed receipt.

    Raises:
        ValueError: When an existing diagnostic or output file has different
            content for the same receipt id.
    """
    expected_log_digest = hashlib.sha256(log_bytes).hexdigest() if log_bytes is not None else None
    if diagnostic.log_digest != expected_log_digest:
        raise ValueError("gate diagnostic log digest mismatch")
    if diagnostic.log_present != (log_bytes is not None):
        raise ValueError("gate diagnostic log presence mismatch")
    payload = (
        orjson.dumps(
            diagnostic.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        + b"\n"
    )
    manifest_path = diagnostic_path(state_path, diagnostic.receipt_id)
    output_path = diagnostic_log_path(state_path, diagnostic.receipt_id)
    expected_files = [(manifest_path, payload)]
    if log_bytes is not None:
        expected_files.insert(0, (output_path, log_bytes))
    with portalock.acquire(manifest_path, timeout=5.0):
        for path, expected in expected_files:
            if path.is_file() and path.read_bytes() != expected:
                raise ValueError(f"gate diagnostic collision: {path.name!r}")
        for path, expected in expected_files:
            if path.is_file():
                os.chmod(path, 0o600)
            else:
                _atomic_write_bytes_locked(path, expected)
    return diagnostic_digest(diagnostic)


def load_gate_diagnostic(state_path: Path, receipt_id: str) -> GateDiagnostic | None:
    """Load one validated local diagnostic, returning ``None`` when absent."""
    path = diagnostic_path(state_path, receipt_id)
    if not path.is_file():
        return None
    try:
        return GateDiagnostic.model_validate(orjson.loads(path.read_bytes()))
    except (OSError, orjson.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"gate diagnostic unreadable: {receipt_id!r}") from exc


def append_gate_receipt(state_path: Path, receipt: GateReceipt) -> bool:
    """Append one safe receipt exactly once under store lock.

    Returns:
        ``True`` when appended, ``False`` for an identical existing receipt.

    Raises:
        ValueError: When the same receipt id already has different content or
            an existing store row is malformed.
    """
    scrub_gate_receipt_store(state_path)
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    envelope = Envelope(
        id=receipt.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=receipt.scope_id,
        created_at=receipt.ended_at,
        summary=f"gate {receipt.gate_id} {receipt.result.value} for {receipt.scope_id}",
        payload=receipt.model_dump(mode="json"),
    )
    line = envelope.model_dump_json().encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(path, timeout=5.0):
        if path.is_file():
            for raw_line in path.read_bytes().splitlines():
                if not raw_line.strip():
                    continue
                existing = Envelope.model_validate(orjson.loads(raw_line))
                if existing.kind is not StoreKind.GATE_RECEIPT:
                    raise ValueError("gate receipt store contains wrong-kind envelope")
                existing_receipt = GateReceipt.model_validate(existing.payload)
                if existing.id != existing_receipt.id:
                    raise ValueError("gate receipt envelope/payload id mismatch")
                if existing.id != receipt.id:
                    continue
                if existing_receipt != receipt:
                    raise ValueError(f"gate receipt id collision: {receipt.id!r}")
                return False
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_parent_dir(path)
        return True


def _legacy_log_bytes(state_path: Path, log_ref: str) -> bytes | None:
    """Read one legacy repo-local log without following escapes or URLs."""
    candidate = Path(log_ref)
    if candidate.is_absolute() or "://" in log_ref:
        return None
    repo_root = state_path.parent.parent.resolve()
    source = (repo_root / candidate).resolve()
    try:
        source.relative_to(repo_root)
    except ValueError:
        return None
    try:
        return source.read_bytes() if source.is_file() else None
    except OSError:
        return None


def _diagnostic_from_legacy(
    state_path: Path,
    legacy: LegacyGateReceipt,
) -> tuple[GateDiagnostic, bytes | None]:
    log_bytes = _legacy_log_bytes(state_path, legacy.full_log_ref)
    diagnostic = GateDiagnostic(
        id=diagnostic_id(legacy.id),
        receipt_id=legacy.id,
        scope_id=legacy.scope_id,
        criterion_id=legacy.criterion_id,
        gate_id=legacy.gate_id,
        captured_at=legacy.ended_at,
        argv=legacy.argv,
        command=legacy.command,
        details=legacy.details,
        stdout_tail=legacy.stdout_tail,
        stderr_tail=legacy.stderr_tail,
        source_log_ref=legacy.full_log_ref,
        log_digest=(hashlib.sha256(log_bytes).hexdigest() if log_bytes is not None else None),
        log_present=log_bytes is not None,
    )
    return diagnostic, log_bytes


def _safe_from_legacy(
    legacy: LegacyGateReceipt,
) -> GateReceipt:
    return GateReceipt(
        id=legacy.id,
        scope_id=legacy.scope_id,
        criterion_id=legacy.criterion_id,
        gate_id=legacy.gate_id,
        integration_id=legacy.integration_id,
        integrated_sha=legacy.integrated_sha,
        tree_sha=legacy.tree_sha,
        contract_digest=canonical_gate_digest(legacy.contract_digest),
        criteria_digest=canonical_gate_digest(legacy.criteria_digest),
        gate_manifest_digest=canonical_gate_digest(legacy.gate_manifest_digest),
        policy_digest=canonical_gate_digest(legacy.policy_digest),
        dependency_binding_digest=canonical_gate_digest(legacy.dependency_binding_digest),
        runner_environment_digest=canonical_gate_digest(legacy.runner_environment_digest),
        runner_digest=canonical_gate_digest(legacy.runner_digest),
        environment_digest=canonical_gate_digest(legacy.environment_digest),
        freshness_key=legacy.freshness_key,
        argv_digest=(
            canonical_gate_digest(legacy.argv_digest) if legacy.argv_digest is not None else None
        ),
        timeout_class=legacy.timeout_class,
        resolved_timeout_seconds=legacy.resolved_timeout_seconds,
        started_at=legacy.started_at,
        ended_at=legacy.ended_at,
        duration_ms=legacy.duration_ms,
        result=legacy.result,
        exit_status=legacy.exit_status,
        stdout_digest=(
            canonical_gate_digest(legacy.stdout_digest)
            if legacy.stdout_digest is not None
            else None
        ),
        stderr_digest=(
            canonical_gate_digest(legacy.stderr_digest)
            if legacy.stderr_digest is not None
            else None
        ),
        selected_file_digest=(
            canonical_gate_digest(legacy.selected_file_digest)
            if legacy.selected_file_digest is not None
            else None
        ),
        collected_nodeid_digest=(
            canonical_gate_digest(legacy.collected_nodeid_digest)
            if legacy.collected_nodeid_digest is not None
            else None
        ),
        residual_manifest_digest=(
            canonical_gate_digest(legacy.residual_manifest_digest)
            if legacy.residual_manifest_digest is not None
            else None
        ),
    )


def _safe_envelope(envelope: Envelope, receipt: GateReceipt) -> Envelope:
    return Envelope(
        schema_version=envelope.schema_version,
        id=receipt.id,
        kind=StoreKind.GATE_RECEIPT,
        scope_id=receipt.scope_id,
        created_at=envelope.created_at,
        updated_at=envelope.updated_at,
        summary=f"gate {receipt.gate_id} {receipt.result.value} for {receipt.scope_id}",
        payload=receipt.model_dump(mode="json"),
    )


def _quarantine_store(state_path: Path, raw: bytes) -> Path:
    """Preserve exact pre-scrub bytes under owner-local quarantine."""
    digest = hashlib.sha256(raw).hexdigest()
    path = (
        state_path.parent
        / "local"
        / _DIAGNOSTIC_DIR_NAME
        / "quarantine"
        / f"gate_receipt-{digest}.jsonl"
    )
    _write_local_file(path, raw)
    return path


def _scrub_row(state_path: Path, raw_line: bytes) -> _ScrubbedRow:
    """Validate and sanitize one row; local writer errors propagate."""
    try:
        envelope = Envelope.model_validate(orjson.loads(raw_line))
        if envelope.kind is not StoreKind.GATE_RECEIPT:
            raise ValueError("wrong store kind")
    except orjson.JSONDecodeError, ValidationError, ValueError:
        return _ScrubbedRow(None, quarantined=True, changed=True)
    try:
        receipt = GateReceipt.model_validate(envelope.payload)
    except ValidationError:
        try:
            legacy = LegacyGateReceipt.model_validate(envelope.payload)
        except ValidationError:
            return _ScrubbedRow(None, quarantined=True, changed=True)
        if envelope.id != legacy.id:
            return _ScrubbedRow(None, quarantined=True, changed=True)
        local_diagnostic, log_bytes = _diagnostic_from_legacy(state_path, legacy)
        write_gate_diagnostic(
            state_path,
            local_diagnostic,
            log_bytes=log_bytes,
        )
        receipt = _safe_from_legacy(legacy)
        safe_envelope = _safe_envelope(envelope, receipt)
        return _ScrubbedRow(safe_envelope, migrated=True, changed=True)
    if envelope.id != receipt.id:
        return _ScrubbedRow(None, quarantined=True, changed=True)
    safe_envelope = _safe_envelope(envelope, receipt)
    changed = safe_envelope.model_dump(mode="json") != envelope.model_dump(mode="json")
    return _ScrubbedRow(safe_envelope, changed=changed)


def scrub_gate_receipt_store(state_path: Path) -> GateReceiptScrubReport:
    """Move legacy raw observations local and atomically sanitize committed rows.

    Valid receipt/envelope ids stay unchanged, so all ``state.json`` references
    remain valid. A malformed row quarantines the exact input and aborts before
    replacement; silently dropping a referenced receipt is forbidden.
    Re-running on sanitized input performs no writes.
    """
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return GateReceiptScrubReport(0, 0, 0, 0, False, None)

    with portalock.acquire(path, timeout=5.0):
        raw = path.read_bytes()
        source_lines = [line for line in raw.splitlines() if line.strip()]
        output: list[Envelope] = []
        migrated_count = 0
        quarantined_count = 0
        changed = False

        for raw_line in source_lines:
            row = _scrub_row(state_path, raw_line)
            migrated_count += int(row.migrated)
            quarantined_count += int(row.quarantined)
            changed = changed or row.changed
            if row.envelope is not None:
                output.append(row.envelope)

        if not changed:
            return GateReceiptScrubReport(
                len(source_lines),
                len(output),
                0,
                0,
                False,
                None,
            )

        quarantine_path = _quarantine_store(state_path, raw)
        if quarantined_count:
            raise ValueError(
                f"gate receipt scrub blocked by malformed rows; quarantined={quarantined_count}"
            )
        replacement = b"".join(
            envelope.model_dump_json().encode("utf-8") + b"\n" for envelope in output
        )
        for line in replacement.splitlines():
            envelope = Envelope.model_validate(orjson.loads(line))
            receipt = GateReceipt.model_validate(envelope.payload)
            if envelope.id != receipt.id:
                raise ValueError("gate receipt replacement id mismatch")
        findings = scan_text(replacement.decode("utf-8"))
        if findings:
            kinds = sorted({finding.kind for finding in findings})
            raise ValueError(f"gate receipt replacement failed leak scan: {kinds!r}")
        _atomic_write_bytes_locked(path, replacement)
        logger.info(
            f"scrub_gate_receipt_store records={len(source_lines)} "
            f"migrated={migrated_count} quarantined={quarantined_count}"
        )
        return GateReceiptScrubReport(
            len(source_lines),
            len(output),
            migrated_count,
            quarantined_count,
            True,
            quarantine_path,
        )


__all__ = [
    "GateReceiptScrubReport",
    "append_gate_receipt",
    "diagnostic_digest",
    "diagnostic_dir",
    "diagnostic_id",
    "diagnostic_log_path",
    "diagnostic_path",
    "load_gate_diagnostic",
    "scrub_gate_receipt_store",
    "write_gate_diagnostic",
]
