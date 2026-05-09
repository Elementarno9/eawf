"""Evidence-area domain package: goals, outcomes, hypotheses, audits, decisions,
incidents, artifacts, backlog.

Each module exposes pure mutator functions that take an in-memory
:class:`~eawf.state.models.State` plus arguments and return a tuple of
``(updated_state, jsonl_record, event_record)`` so the CLI handler layer can
serialise them under the sibling lock without leaking I/O concerns into the
business logic. The audit-evidence invariant from
:mod:`eawf.validate.invariants` (``check_audit_evidence``) is the source of
truth for verdict-bearing rules and is invoked at write time via
:func:`require_complete_audit`.
"""

from __future__ import annotations

from eawf.evidence.guards import require_complete_audit

__all__ = ["require_complete_audit"]
