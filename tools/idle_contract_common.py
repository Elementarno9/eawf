"""Shared result types and probe constants for the idle-contract gate modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Repo root, derived once from this file's location (``tools/`` is a sibling
#: of ``src/`` and ``tests/``). Tree scans and store reads resolve against
#: this root so the gate works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent


class GateFailure(StrEnum):
    """The mutually exclusive ways the idle-contract gate can fail.

    The order encodes precedence, so a single run names the more fundamental
    problem first.
    """

    BODY_VALIDATION_IDLE = "body_validation_idle"
    MOCKUP_GOLDEN_DIFF_IDLE = "mockup_golden_diff_idle"
    RUNTIME_GATE_IDLE = "runtime_gate_idle"
    AUDIT_DSL_KIND_IDLE = "audit_dsl_kind_idle"
    JURY_RELIABILITY_MAP_IDLE = "jury_reliability_map_idle"
    JURY_BLOCK_AUTHORITY_IDLE = "jury_block_authority_idle"
    VALIDATE_JURY_CLI_IDLE = "validate_jury_cli_idle"
    EAWF023_ARTIFACT_PLACEMENT_IDLE = "eawf023_artifact_placement_idle"
    EAWF024_TEST_TIER_IDLE = "eawf024_test_tier_idle"
    EAWF025_TEST_PLACEMENT_IDLE = "eawf025_test_placement_idle"
    COVERAGE_GATE_IDLE = "coverage_gate_idle"
    CONSOLE_APP_CONSTRUCTION_IDLE = "console_app_construction_idle"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one idle-contract check.

    Attributes:
        passed: Whether both the not-idle and band-scoped contracts held.
        failure: The failure kind when ``passed`` is ``False``; ``None`` on a
            pass.
        message: A human-readable line; on failure it names the violated
            contract and the offending profile.
    """

    passed: bool
    failure: GateFailure | None
    message: str
