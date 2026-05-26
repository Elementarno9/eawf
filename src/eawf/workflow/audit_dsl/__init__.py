"""Audit-check DSL (B019, D02).

Yaml-declarative check spec + check-kind registry. The CLI command
``eawf audit run --checks <yaml>`` wires this package; the legacy
``--fixture`` escape hatch remains for the v0.2 cycle.
"""

from __future__ import annotations

from eawf.workflow.audit_dsl.models import (
    CheckFile,
    CheckKind,
    CheckResult,
    CheckSpec,
    CheckStatus,
    CommandExitZeroArgs,
    Scope,
    TimeoutClass,
)
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY, CheckFn
from eawf.workflow.audit_dsl.runner import load_spec, run_checks

__all__ = [
    "CHECK_REGISTRY",
    "CheckFile",
    "CheckFn",
    "CheckKind",
    "CheckResult",
    "CheckSpec",
    "CheckStatus",
    "CommandExitZeroArgs",
    "Scope",
    "TimeoutClass",
    "load_spec",
    "run_checks",
]
