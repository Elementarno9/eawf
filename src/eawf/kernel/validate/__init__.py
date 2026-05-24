"""Validation for eawf state.json: schema layer + invariant layer.

Public surface re-exports the report and helpers from :mod:`eawf.kernel.validate.strict`
and the violation type from :mod:`eawf.kernel.validate.invariants`.
"""

from __future__ import annotations

from eawf.kernel.validate.invariants import (
    ALL_INVARIANTS,
    Invariant,
    ValidationIndex,
    Violation,
    build_validation_index,
)
from eawf.kernel.validate.strict import ValidationReport, validate_path, validate_state

__all__ = [
    "ALL_INVARIANTS",
    "Invariant",
    "ValidationIndex",
    "ValidationReport",
    "Violation",
    "build_validation_index",
    "validate_path",
    "validate_state",
]
