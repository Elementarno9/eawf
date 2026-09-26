"""Shared result types and probe constants for the idle-contract gate modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

#: Repo root, derived once from this file's location (``tools/`` is a sibling
#: of ``src/`` and ``tests/``). Tree scans and store reads resolve against
#: this root so the gate works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Probe file scope that IS UI surface (per
#: :func:`eawf.kernel.spec.heuristics.is_ui_scope`). A band profile MUST resolve
#: to ``enforce=True`` for a wave touching this scope.
_UI_SCOPE = "src/eawf/surfaces/tui/app.py"

#: Probe file scope that is NOT UI surface. A band-scoped (not global) profile
#: MUST resolve to ``enforce=False`` for a wave touching this scope.
_NON_UI_SCOPE = "src/eawf/kernel/state/models.py"

_PROBE_OPENED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class GateFailure(StrEnum):
    """The mutually exclusive ways the idle-contract gate can fail.

    The order encodes precedence: an absent producer wiring (idle) is reported
    before a band/global resolution defect, so a single run names the more
    fundamental problem first.
    """

    PRODUCER_IDLE = "producer_idle"
    BAND_ENFORCES_GLOBALLY = "band_enforces_globally"
    BODY_VALIDATION_IDLE = "body_validation_idle"
    REQUIRED_INTENT_IDLE = "required_intent_idle"
    UI_REQUIRE_GATE_IDLE = "ui_require_gate_idle"
    MOCKUP_GOLDEN_DIFF_IDLE = "mockup_golden_diff_idle"
    RESOLVE_ROUTING_IDLE = "resolve_routing_idle"
    RUNTIME_GATE_IDLE = "runtime_gate_idle"
    AUDIT_DSL_KIND_IDLE = "audit_dsl_kind_idle"
    SPEC_JURY_BALLOT_FN_IDLE = "spec_jury_ballot_fn_idle"
    JURY_RELIABILITY_MAP_IDLE = "jury_reliability_map_idle"
    JURY_BLOCK_AUTHORITY_IDLE = "jury_block_authority_idle"
    VALIDATE_JURY_CLI_IDLE = "validate_jury_cli_idle"
    PHASE_TRACK_TAG_IDLE = "phase_track_tag_idle"
    DRIVE_LADDERS_IDLE = "drive_ladders_idle"
    LIVE_OUTPUT_TEXT_IDLE = "live_output_text_idle"
    CAMPAIGN_CLAIM_FOLD_IDLE = "campaign_claim_fold_idle"
    CAMPAIGN_CARRYOVER_PRUNE_IDLE = "campaign_carryover_prune_idle"
    EAWF023_ARTIFACT_PLACEMENT_IDLE = "eawf023_artifact_placement_idle"
    EAWF024_TEST_TIER_IDLE = "eawf024_test_tier_idle"
    EAWF025_TEST_PLACEMENT_IDLE = "eawf025_test_placement_idle"
    COVERAGE_GATE_IDLE = "coverage_gate_idle"
    CAMPAIGN_PRODUCER_STUB_IDLE = "campaign_producer_stub_idle"
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
