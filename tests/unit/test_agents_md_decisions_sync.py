"""Unit tests for the typed-Decisions render path on AGENTS.md.

Covers :func:`eawf.surfaces.render.agents_md.render_decisions_section`, the
pure body builder, and pins that
:func:`eawf.surfaces.render.agents_md.render_agents_md` called without a
``state`` emits no managed Decisions region.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    DecisionStatus,
)
from eawf.kernel.state.models import Decision
from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.surfaces.render import regions
from eawf.surfaces.render.agents_md import (
    DECISIONS_REGION_ID,
    render_agents_md,
    render_decisions_section,
)
from eawf.surfaces.render.manifest import Manifest

# ---- Builders ---------------------------------------------------------------


def _decision(
    id_: str,
    *,
    scope_id: str = "QR",
    summary: str | None = None,
    rationale: str | None = None,
    alternatives: list[str] | None = None,
    consequences: list[str] | None = None,
    status: DecisionStatus = DecisionStatus.ACTIVE,
) -> Decision:
    """Build a :class:`Decision` fixture with sane defaults for tests."""
    return Decision(
        id=id_,
        scope_id=scope_id,
        title=summary if summary is not None else f"{id_} summary text",
        rationale=rationale if rationale is not None else f"{id_} rationale paragraph.",
        alternatives=alternatives if alternatives is not None else [],
        consequences=consequences if consequences is not None else [],
        status=status,
        created_at=datetime.now(UTC),
        superseded_by=None,
    )


def _composed_with_blocks(blocks: list[RenderBlock] | None = None) -> ComposedProfile:
    """Build a ComposedProfile carrying the supplied (or empty) render_blocks."""
    return ComposedProfile(
        name="test",
        version="1.0",
        description="",
        render_blocks=blocks or [],
    )


# ---- render_decisions_section (pure) ----------------------------------------


def test_render_decisions_section_empty_dict_returns_none_marker() -> None:
    """Empty decisions dict emits a stable ``## Decisions\\n\\nNone.`` body."""
    body = render_decisions_section({})
    assert body == "## Decisions\n\nNone."


def test_render_decisions_section_none_treated_as_empty() -> None:
    """``None`` is accepted (mirrors optional-typed sister State fields)."""
    body = render_decisions_section(None)
    assert body == "## Decisions\n\nNone."


def test_render_decisions_section_single_decision_emits_summary_and_rationale() -> None:
    """A single Decision renders id + summary heading and rationale paragraph."""
    pool = {
        "D01": _decision(
            "D01",
            summary="Cherry-pick worktrees, never merge",
            rationale="Merges break the [P-W] / [P-CORE] history audit trail.",
        )
    }
    body = render_decisions_section(pool)
    assert body.startswith("## Decisions\n")
    assert "### D01: Cherry-pick worktrees, never merge" in body
    assert "Merges break the [P-W] / [P-CORE] history audit trail." in body


def test_render_decisions_section_alternatives_block_when_present() -> None:
    """Alternatives list renders as bulleted ``Alternatives considered:`` block."""
    pool = {
        "D02": _decision(
            "D02",
            summary="State CLI is the only writer",
            rationale="Direct edits bypass the audit-side event.jsonl.",
            alternatives=["allow direct edits", "deferred consistency check"],
        )
    }
    body = render_decisions_section(pool)
    assert "Alternatives considered:" in body
    assert "- allow direct edits" in body
    assert "- deferred consistency check" in body


def test_render_decisions_section_omits_alternatives_block_when_empty() -> None:
    """Missing alternatives means no ``Alternatives considered:`` line at all."""
    pool = {
        "D03": _decision(
            "D03",
            summary="No-alts decision",
            rationale="One reason.",
            alternatives=[],
        )
    }
    body = render_decisions_section(pool)
    assert "Alternatives considered:" not in body


def test_render_decisions_section_consequences_block_when_present() -> None:
    """Consequences list renders as a bulleted ``Consequences:`` block."""
    pool = {
        "D14": _decision(
            "D14",
            summary="Adopt the daemon as sole writer",
            rationale="Single mutator removes write races.",
            consequences=["state writes serialise", "CLI gains a daemon dep"],
        )
    }
    body = render_decisions_section(pool)
    assert "Consequences:" in body
    assert "- state writes serialise" in body
    assert "- CLI gains a daemon dep" in body


def test_render_decisions_section_omits_consequences_block_when_empty() -> None:
    """Missing consequences means no ``Consequences:`` line at all."""
    pool = {
        "D15": _decision(
            "D15",
            summary="No-consequences decision",
            rationale="One reason.",
            consequences=[],
        )
    }
    body = render_decisions_section(pool)
    assert "Consequences:" not in body


def test_render_decisions_section_sorts_by_id_lexicographically() -> None:
    """D02 must appear after D01 even when inserted in reverse order."""
    pool = {
        "D02": _decision("D02"),
        "D01": _decision("D01"),
        "D10": _decision("D10"),
    }
    body = render_decisions_section(pool)
    assert body.index("### D01") < body.index("### D02") < body.index("### D10")


def test_render_decisions_section_status_badge_for_superseded() -> None:
    """Non-active status renders as ``[<status>]`` badge on the heading."""
    pool = {
        "D04": _decision(
            "D04",
            summary="Old policy",
            status=DecisionStatus.SUPERSEDED,
        )
    }
    body = render_decisions_section(pool)
    assert "### D04 [superseded]: Old policy" in body


def test_render_decisions_section_active_no_badge() -> None:
    """Active decisions render WITHOUT a status badge (the default)."""
    pool = {"D05": _decision("D05", summary="Live policy")}
    body = render_decisions_section(pool)
    assert "### D05: Live policy" in body
    assert "[active]" not in body


def test_render_decisions_section_scope_filter_includes_match_only() -> None:
    """``scope_id`` filter drops records belonging to a different scope."""
    pool = {
        "D06": _decision("D06", scope_id="QR"),
        "D99": _decision("D99", scope_id="OTHER"),
    }
    body = render_decisions_section(pool, scope_id="QR")
    assert "### D06" in body
    assert "D99" not in body


def test_render_decisions_section_scope_filter_no_match_returns_none_marker() -> None:
    """Filtering with no matches yields the empty-section sentinel body."""
    pool = {"D07": _decision("D07", scope_id="OTHER")}
    body = render_decisions_section(pool, scope_id="QR")
    assert body == "## Decisions\n\nNone."


def test_render_decisions_section_does_not_mutate_input() -> None:
    """Pure function: caller's dict survives unmodified after the call."""
    pool = {"D08": _decision("D08"), "D09": _decision("D09")}
    snapshot = dict(pool)
    render_decisions_section(pool)
    assert pool == snapshot


# ---- render_agents_md without state -----------------------------------------


def test_render_agents_md_state_none_skips_decisions_region(tmp_path: Path) -> None:
    """Without *state* the renderer never emits a managed Decisions region."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks(
        [RenderBlock(id="rules", target="AGENTS.md", body_template="## Rules\n\n- one")]
    )
    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))
    text = target.read_text(encoding="utf-8")
    parsed = {r.id for r in regions.find_regions(text)}
    assert DECISIONS_REGION_ID not in parsed
    assert DECISIONS_REGION_ID not in result.regions_added


# ---- Error-path tests -------------------------------------------------------


def test_render_decisions_section_rejects_invalid_decision_shape() -> None:
    """A non-Decision value would raise AttributeError on attribute access."""
    # Build a malformed dict that bypasses the Pydantic gate (callers MUST pass
    # typed Decision objects; we surface the failure rather than silently
    # emitting garbage if a stale shim leaks an untyped dict through).
    with pytest.raises(AttributeError):
        render_decisions_section({"D-BAD": "not-a-decision"})  # type: ignore[dict-item]


# ---- Decision.consequences model contract -----------------------------------


def test_decision_consequences_round_trips() -> None:
    """A Decision with consequences validates, serializes, and re-loads stably."""
    decision = _decision("D20", consequences=["x", "y"])
    dumped = decision.model_dump(mode="json")
    assert dumped["consequences"] == ["x", "y"]
    reloaded = Decision.model_validate(dumped)
    assert reloaded.consequences == ["x", "y"]
    assert reloaded == decision


def test_decision_consequences_defaults_empty_when_omitted() -> None:
    """A decision dict lacking the field still validates (back-compat default)."""
    legacy = {
        "id": "D21",
        "scope_id": "QR",
        "title": "Pre-consequences decision",
        "rationale": "Written before the field existed.",
        "status": DecisionStatus.ACTIVE.value,
        "created_at": datetime.now(UTC).isoformat(),
    }
    decision = Decision.model_validate(legacy)
    assert decision.consequences == []


def test_decision_consequences_rejects_non_list() -> None:
    """A scalar consequences value fails validation (list[str] contract)."""
    with pytest.raises(ValidationError):
        _decision("D22", consequences="not-a-list")  # type: ignore[arg-type]
