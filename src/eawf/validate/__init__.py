"""Validation for eawf state.json: schema layer + invariant layer.

Public surface re-exports the report and helpers from :mod:`eawf.validate.strict`
and the violation type from :mod:`eawf.validate.invariants`.
"""

from __future__ import annotations

from eawf.validate.invariants import ALL_INVARIANTS, Invariant, Violation
from eawf.validate.strict import ValidationReport, validate_path, validate_state

__all__ = [
    "ALL_INVARIANTS",
    "Invariant",
    "ValidationReport",
    "Violation",
    "validate_path",
    "validate_state",
]
