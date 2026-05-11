"""Audit-area mutators: add / run / integrity / show / list.

* ``add`` registers an audit pointing at a report artifact. The audit itself
  *is* the evidence anchor for downstream verdict-bearing commands, so it does
  not require an outer ``--audit`` reference. Status is ``complete`` whenever
  ``--report`` is provided (the matrix row's "report missing" error path
  prevents headless ``add`` of a complete audit without evidence).
* ``run`` is a Phase-2 stub: it executes a fixture-driven check spec, writes
  an artifact whose path goes into ``audits.jsonl``, and stores results on
  the audit record. Full check runner deferred to Phase 4.
* ``integrity`` appends a single integrity-check result.
* ``show`` / ``list`` are read-only.

Mutators take a typed :class:`State` and mutate it in place; the CLI handler
runs them inside :func:`eawf.cli._mutation.state_transaction`.
"""

# TODO(Phase 4): full check-runner replaces the fixture-driven stub in `run`.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.cli.errors import InvalidInput, NotFound
from eawf.evidence import _io
from eawf.state.enums import AuditKind, AuditStatus, AuditVerdict, StoreKind
from eawf.state.models import Audit, State
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


def add_audit(
    state: State,
    *,
    audit_id: str,
    scope_id: str,
    kind: AuditKind,
    report_artifact_id: str | None = None,
    verdict: AuditVerdict | None = None,
) -> tuple[Envelope, Envelope]:
    """Register a new audit in place and return (record, event) envelopes.

    When ``report_artifact_id`` is provided, the audit lands in
    :attr:`AuditStatus.COMPLETE`; otherwise it stays
    :attr:`AuditStatus.PENDING` so downstream verdict-bearing commands fail
    closed via :func:`require_complete_audit`.

    Raises:
        InvalidInput: When ``audit_id`` already exists, when ``verdict`` is
            given without ``report_artifact_id``, or when ``report_artifact_id``
            is non-None but absent from :attr:`State.artifacts` (orphan-ref
            guard — the audit-evidence anchor must point at an existing
            artifact, not a placeholder id).
    """
    audits: dict[str, Audit] = dict(state.audits or {})
    if audit_id in audits:
        raise InvalidInput(f"audit {audit_id!r} already exists")

    if verdict is not None and report_artifact_id is None:
        raise InvalidInput(f"audit {audit_id!r} carries verdict={verdict.value!r} but no report")

    if report_artifact_id is not None and report_artifact_id not in state.artifacts:
        raise InvalidInput(f"unknown artifact id: {report_artifact_id!r}")

    status = AuditStatus.COMPLETE if report_artifact_id is not None else AuditStatus.PENDING

    now = datetime.now(UTC)
    audit = Audit(
        id=audit_id,
        scope_id=scope_id,
        kind=kind,
        status=status,
        report_artifact_id=report_artifact_id,
        check_results=[],
        integrity_results=[],
        created_at=now,
        verdict=verdict,
    )
    audits[audit_id] = audit
    state.audits = audits
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=audit_id,
        kind=StoreKind.AUDIT,
        scope_id=scope_id,
        summary=f"audit {audit_id} {kind.value} status={status.value}",
        payload={
            "audit_kind": kind.value,
            "verdict": verdict.value if verdict else None,
            "check_results": [],
            "report_artifact_id": report_artifact_id,
        },
        artifact_ids=[report_artifact_id] if report_artifact_id else [],
    )
    event = _io.event_envelope(
        event_id=f"EVT-audit-add-{audit_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="audit.add",
        actor="cli",
        command="audit add",
        args={
            "audit_id": audit_id,
            "kind": kind.value,
            "report_artifact_id": report_artifact_id,
        },
        summary=f"audit {audit_id} added",
        artifact_ids=[report_artifact_id] if report_artifact_id else [],
    )
    return record, event


def run_audit(
    state: State,
    *,
    audit_id: str,
    scope_id: str,
    kind: AuditKind,
    fixture_path: Path | None = None,
    check_results: list[dict[str, Any]] | None = None,
) -> tuple[Envelope, Envelope]:
    """Register a complete audit using fixture / DSL / stub check_results.

    Resolution order for the check_results payload:

    1. ``check_results`` kwarg (Phase 4 path — populated by
       :mod:`eawf.audit_dsl.runner` via the ``--checks`` option). Wins
       whenever non-None.
    2. ``fixture_path`` (Phase 2 escape hatch — kept for v0.2).
    3. The legacy single-pass stub.

    ``check_results`` and ``fixture_path`` are mutually exclusive at the
    CLI layer; the library accepts whichever the caller supplies.
    """
    audits: dict[str, Audit] = dict(state.audits or {})
    if audit_id in audits:
        raise InvalidInput(f"audit {audit_id!r} already exists")

    resolved: list[dict[str, Any]]
    if check_results is not None:
        resolved = list(check_results)
    elif fixture_path is not None and fixture_path.exists():
        # Fixture-driven check_results: list of {"name", "passed", "details"}.
        import json

        try:
            resolved = json.loads(fixture_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise InvalidInput(f"audit run fixture {fixture_path} not valid JSON: {exc}") from exc
    else:
        resolved = [{"name": "stub", "passed": True, "details": "Phase 2 stub"}]

    now = datetime.now(UTC)
    all_passed = all(bool(r.get("passed", False)) for r in resolved)
    verdict = AuditVerdict.PASS if all_passed else AuditVerdict.MAJOR

    audit = Audit(
        id=audit_id,
        scope_id=scope_id,
        kind=kind,
        status=AuditStatus.COMPLETE,
        report_artifact_id=None,
        check_results=resolved,
        integrity_results=[],
        created_at=now,
        verdict=verdict,
    )
    audits[audit_id] = audit
    state.audits = audits
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=audit_id,
        kind=StoreKind.AUDIT,
        scope_id=scope_id,
        summary=f"audit {audit_id} run verdict={verdict.value}",
        payload={
            "audit_kind": kind.value,
            "verdict": verdict.value,
            "check_results": [
                {
                    "name": str(r.get("name", "")),
                    "passed": bool(r.get("passed", False)),
                    "details": (str(r["details"]) if r.get("details") else None),
                }
                for r in resolved
            ],
            "report_artifact_id": None,
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-audit-run-{audit_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="audit.run",
        actor="cli",
        command="audit run",
        args={
            "audit_id": audit_id,
            "scope_id": scope_id,
            "kind": kind.value,
            "fixture_path": (str(fixture_path) if fixture_path else None),
            "checks_source": (
                "dsl"
                if check_results is not None
                else ("fixture" if fixture_path is not None else "stub")
            ),
        },
        summary=f"audit {audit_id} run completed verdict={verdict.value}",
    )
    return record, event


def set_verdict(
    state: State,
    *,
    audit_id: str,
    verdict: AuditVerdict,
    report_artifact_id: str | None = None,
) -> tuple[Envelope, Envelope]:
    """Set ``verdict`` on an existing audit; lift ``PENDING`` audits to
    ``COMPLETE`` when ``report_artifact_id`` is supplied.

    Behaviour matrix:

    * ``audit_id`` absent → :class:`NotFound`.
    * Audit ``PENDING`` and ``report_artifact_id`` is ``None`` →
      :class:`InvalidInput` (a verdict requires evidence).
    * Audit ``PENDING`` and ``report_artifact_id`` provided → orphan-ref
      guard mirrors :func:`add_audit`; on success the audit lifts to
      ``COMPLETE`` with the supplied ``report_artifact_id`` and ``verdict``.
    * Audit ``COMPLETE`` → ``verdict`` is updated in place. Supplying a
      ``report_artifact_id`` that *differs* from the existing one is rejected
      so callers cannot silently relink the evidence anchor; passing the
      same id (or omitting it) is a no-op for the report field.
    """
    audits: dict[str, Audit] = dict(state.audits or {})
    if audit_id not in audits:
        raise NotFound(f"audit {audit_id!r} not found")

    prior = audits[audit_id]
    new_status: AuditStatus
    new_report: str | None

    if prior.status == AuditStatus.PENDING:
        if report_artifact_id is None:
            raise InvalidInput(
                f"audit {audit_id!r} is pending; --report required to lift to complete"
            )
        if report_artifact_id not in state.artifacts:
            raise InvalidInput(f"unknown artifact id: {report_artifact_id!r}")
        new_status = AuditStatus.COMPLETE
        new_report = report_artifact_id
    else:
        if report_artifact_id is not None and report_artifact_id != prior.report_artifact_id:
            raise InvalidInput(
                f"audit {audit_id!r} already has report={prior.report_artifact_id!r}; "
                f"cannot replace with {report_artifact_id!r}"
            )
        new_status = prior.status
        new_report = prior.report_artifact_id

    now = datetime.now(UTC)
    updated = prior.model_copy(
        update={
            "status": new_status,
            "report_artifact_id": new_report,
            "verdict": verdict,
        }
    )
    audits[audit_id] = updated
    state.audits = audits
    state.updated_at = now

    ts_ms = int(now.timestamp() * 1000)
    record = _io.kind_envelope(
        record_id=f"{audit_id}-VERDICT-{ts_ms}",
        kind=StoreKind.AUDIT,
        scope_id=updated.scope_id,
        summary=f"audit {audit_id} verdict={verdict.value}",
        payload={
            "audit_kind": updated.kind.value,
            "verdict": verdict.value,
            "check_results": list(updated.check_results),
            "report_artifact_id": new_report,
        },
        artifact_ids=[new_report] if new_report else [],
    )
    event = _io.event_envelope(
        event_id=f"EVT-audit-set-verdict-{audit_id}-{ts_ms}",
        scope_id=updated.scope_id,
        event_type="audit.set_verdict",
        actor="cli",
        command="audit set-verdict",
        args={
            "audit_id": audit_id,
            "verdict": verdict.value,
            "report_artifact_id": report_artifact_id,
        },
        summary=f"audit {audit_id} verdict={verdict.value}",
        artifact_ids=[new_report] if new_report else [],
    )
    return record, event


def add_integrity(
    state: State,
    *,
    audit_id: str,
    check: str,
    passed: bool,
    details: str | None = None,
) -> tuple[Envelope, Envelope]:
    """Append an integrity-check entry to an existing audit (in place)."""
    audits: dict[str, Audit] = dict(state.audits or {})
    if audit_id not in audits:
        raise NotFound(f"audit {audit_id!r} not found")

    now = datetime.now(UTC)
    prior = audits[audit_id]
    new_integrity = list(prior.integrity_results)
    new_integrity.append(
        {
            "check": check,
            "passed": passed,
            "details": details,
            "added_at": now.isoformat(),
        }
    )
    updated = prior.model_copy(update={"integrity_results": new_integrity})
    audits[audit_id] = updated
    state.audits = audits
    state.updated_at = now

    record = _io.kind_envelope(
        record_id=f"{audit_id}-INT-{len(new_integrity):03d}",
        kind=StoreKind.AUDIT,
        scope_id=updated.scope_id,
        summary=f"audit {audit_id} integrity {check} passed={passed}",
        payload={
            "audit_kind": updated.kind.value,
            "verdict": updated.verdict.value if updated.verdict else None,
            "check_results": [
                {"name": check, "passed": passed, "details": details},
            ],
            "report_artifact_id": updated.report_artifact_id,
        },
    )
    event = _io.event_envelope(
        event_id=f"EVT-audit-integrity-{audit_id}-{int(now.timestamp() * 1000)}",
        scope_id=updated.scope_id,
        event_type="audit.integrity",
        actor="cli",
        command="audit integrity",
        args={
            "audit_id": audit_id,
            "check": check,
            "passed": passed,
        },
        summary=f"audit {audit_id} integrity {check} passed={passed}",
    )
    return record, event


def show_audit(state: State, audit_id: str) -> Audit:
    """Read-only lookup."""
    audits = state.audits or {}
    if audit_id not in audits:
        raise NotFound(f"audit {audit_id!r} not found")
    return audits[audit_id]


def list_audits(
    state: State,
    *,
    scope_id: str | None = None,
    kind: AuditKind | None = None,
    status: AuditStatus | None = None,
) -> list[Audit]:
    """Filtered list of audits, sorted by ``id``."""
    out: list[Audit] = []
    for audit in (state.audits or {}).values():
        if scope_id is not None and audit.scope_id != scope_id:
            continue
        if kind is not None and audit.kind != kind:
            continue
        if status is not None and audit.status != status:
            continue
        out.append(audit)
    out.sort(key=lambda a: a.id)
    return out
