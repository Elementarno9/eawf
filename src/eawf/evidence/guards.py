"""Audit-evidence write-time guards.

The :mod:`eawf.kernel.validate.invariants` module owns the post-mutation invariant
``check_audit_evidence`` which fires ``INV.AUDIT.OUTCOME_MISSING_AUDIT`` and
``INV.AUDIT.HYPOTHESIS_MISSING_AUDIT`` when a met/missed outcome or a
verdicted hypothesis lacks an ``audit_id``. That invariant is necessary but
not sufficient: it cannot tell the difference between an ``audit_id`` that
points to a real *complete* audit and one that names a missing or pending
audit.

This module enforces the stronger v0.1 rule at *write time*: every
verdict-bearing mutation (``outcome set``, ``hypothesis verdict``,
``incident close``, ``backlog close``) must provide ``--audit <id>`` of an
audit that exists in ``state.audits`` and has ``status == complete``.

Failures are surfaced as :class:`eawf.cli.errors.ValidationError` so the
caller exits with code ``4`` (``VALIDATION_FAILED``), matching the canonical
exit-code table from :mod:`eawf.cli.exit_codes`.
"""

from __future__ import annotations

import logging

from eawf.cli.errors import ValidationError
from eawf.kernel.state.enums import AuditStatus
from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


def require_complete_audit(state: State, audit_id: str | None) -> None:
    """Raise :class:`ValidationError` if *audit_id* is not a complete audit.

    Args:
        state: The candidate state already holding any new audit record(s).
        audit_id: The audit ID supplied via ``--audit``. ``None`` is rejected
            because verdict-bearing commands MUST cite an audit.

    Raises:
        ValidationError: With a code surrogate (one of ``MISSING``,
            ``UNKNOWN``, ``NOT_COMPLETE``) embedded in the message so callers
            can correlate the exit envelope with the failure mode.
    """
    if audit_id is None:
        logger.debug("require_complete_audit missing-audit")
        raise ValidationError(
            "audit-evidence: --audit is required for verdict-bearing commands (INV.AUDIT.MISSING)"
        )
    audits = state.audits or {}
    if audit_id not in audits:
        logger.debug(f"require_complete_audit unknown-audit audit_id={audit_id!r}")
        raise ValidationError(
            f"audit-evidence: audit {audit_id!r} not found in state.audits (INV.AUDIT.UNKNOWN)"
        )
    audit = audits[audit_id]
    if audit.status != AuditStatus.COMPLETE:
        logger.debug(
            f"require_complete_audit incomplete audit_id={audit_id!r} status={audit.status.value}"
        )
        raise ValidationError(
            f"audit-evidence: audit {audit_id!r} has status "
            f"{audit.status.value!r}; must be 'complete' (INV.AUDIT.NOT_COMPLETE)"
        )
