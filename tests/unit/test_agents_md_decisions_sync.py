"""Unit tests for the typed-Decisions render path on AGENTS.md.

Covers :func:`eawf.render.agents_md.render_decisions_section` (pure body
builder) and the opt-in ``state``/``decisions_scope_id`` plumbing on
:func:`eawf.render.agents_md.render_agents_md` (managed-region injection).

Round-trip contract:

    typed Decision rows  ->  render_decisions_section / render_agents_md
                          ->  Markdown section containing every Decision's
                              id + summary + rationale + alternatives
                          ->  parseable via eawf.render.regions.find_regions

The round-trip test seeds the same 23 ids the P19-I02 plan-absorption
commit writes to .ea/state.json (D01..D23) so the section emitted by
the renderer mirrors what ``eawf sync`` would write on a real project.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.profiles.models import ComposedProfile, RenderBlock
from eawf.render import regions
from eawf.render.agents_md import (
    DECISIONS_REGION_ID,
    DECISIONS_REGION_VERSION,
    render_agents_md,
    render_decisions_section,
)
from eawf.render.manifest import Manifest
from eawf.state.enums import (
    DecisionStatus,
    ProjectStatus,
    ScopeKind,
)
from eawf.state.models import (
    CurrentPointers,
    Decision,
    Project,
    State,
)

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


def _state_with_decisions(decisions: dict[str, Decision]) -> State:
    """Return a minimal State carrying the supplied decisions dict."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
            "decisions": {did: d.model_dump(mode="json") for did, d in decisions.items()},
        }
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


# ---- render_agents_md state-injection plumbing ------------------------------


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


def test_render_agents_md_state_injects_decisions_region(tmp_path: Path) -> None:
    """With *state* the renderer appends a managed ``decisions`` region."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks(
        [RenderBlock(id="rules", target="AGENTS.md", body_template="## Rules\n\n- one")]
    )
    state = _state_with_decisions(
        {
            "D01": _decision("D01", summary="First decision"),
            "D02": _decision("D02", summary="Second decision"),
        }
    )

    result, manifest_after = render_agents_md(
        composed,
        target,
        Manifest(version=1, generated={}),
        state=state,
    )

    text = target.read_text(encoding="utf-8")
    parsed = {r.id: r for r in regions.find_regions(text)}
    assert DECISIONS_REGION_ID in parsed
    decisions_region = parsed[DECISIONS_REGION_ID]
    assert decisions_region.version == DECISIONS_REGION_VERSION
    assert "### D01: First decision" in decisions_region.body
    assert "### D02: Second decision" in decisions_region.body
    assert DECISIONS_REGION_ID in result.regions_added
    # Manifest carries the new region.
    composite_key = f"{target.as_posix()}::{DECISIONS_REGION_ID}"
    assert composite_key in manifest_after.generated
    entry = manifest_after.generated[composite_key]
    assert entry.region_id == DECISIONS_REGION_ID
    assert entry.version == DECISIONS_REGION_VERSION
    assert entry.hash == regions.compute_hash(decisions_region.body)


def test_render_agents_md_decisions_scope_filter(tmp_path: Path) -> None:
    """``decisions_scope_id`` forwards to render_decisions_section."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks()
    state = _state_with_decisions(
        {
            "D10": _decision("D10", scope_id="QR", summary="In-scope"),
            "D99": _decision("D99", scope_id="OTHER", summary="Out-of-scope"),
        }
    )

    render_agents_md(
        composed,
        target,
        Manifest(version=1, generated={}),
        state=state,
        decisions_scope_id="QR",
    )

    text = target.read_text(encoding="utf-8")
    parsed = {r.id: r for r in regions.find_regions(text)}
    body = parsed[DECISIONS_REGION_ID].body
    assert "### D10: In-scope" in body
    assert "D99" not in body


def test_render_agents_md_re_render_with_unchanged_state_is_unchanged(
    tmp_path: Path,
) -> None:
    """Identical state on a second pass reports ``regions_unchanged``."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks()
    state = _state_with_decisions({"D11": _decision("D11")})

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}), state=state)
    result, _ = render_agents_md(composed, target, manifest, state=state)

    assert DECISIONS_REGION_ID in result.regions_unchanged
    assert DECISIONS_REGION_ID not in result.regions_updated
    assert DECISIONS_REGION_ID not in result.regions_added


def test_render_agents_md_re_render_with_changed_state_marks_updated(
    tmp_path: Path,
) -> None:
    """Changing a Decision rationale flips the region to ``regions_updated``."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks()
    state_a = _state_with_decisions({"D12": _decision("D12", rationale="First.")})
    state_b = _state_with_decisions({"D12": _decision("D12", rationale="Second.")})

    _, manifest = render_agents_md(
        composed, target, Manifest(version=1, generated={}), state=state_a
    )
    result, _ = render_agents_md(composed, target, manifest, state=state_b)

    assert DECISIONS_REGION_ID in result.regions_updated
    text = target.read_text(encoding="utf-8")
    parsed = {r.id: r for r in regions.find_regions(text)}
    assert "Second." in parsed[DECISIONS_REGION_ID].body
    assert "First." not in parsed[DECISIONS_REGION_ID].body


# ---- Round-trip with the P19-I02 D01..D23 fixture ---------------------------


def _build_d01_through_d23() -> dict[str, Decision]:
    """Build the 23-row Decision fixture mirroring the P19-I02 plan absorption.

    The actual P19-I02 decisions live in ``.ea/state.json``; this fixture
    uses synthetic summary/rationale text per id but covers the same id
    population (D01..D23) so the round-trip test demonstrates that every
    id surfaces in the rendered AGENTS.md section.
    """
    pool: dict[str, Decision] = {}
    for n in range(1, 24):
        did = f"D{n:02d}"
        pool[did] = _decision(
            did,
            summary=f"{did} policy summary",
            rationale=f"{did} rationale block — multiple sentences. Second sentence.",
            alternatives=[f"{did}-alt-a", f"{did}-alt-b"] if n % 3 == 0 else [],
        )
    return pool


def test_render_agents_md_round_trip_d01_through_d23(tmp_path: Path) -> None:
    """Typed D01..D23 -> rendered AGENTS.md -> parseable section with every id."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks(
        [
            RenderBlock(
                id="non-negotiable-rules",
                target="AGENTS.md",
                body_template="## Non-negotiable rules (core)\n\n- placeholder",
            )
        ]
    )
    decisions = _build_d01_through_d23()
    state = _state_with_decisions(decisions)

    result, manifest_after = render_agents_md(
        composed,
        target,
        Manifest(version=1, generated={}),
        state=state,
    )

    # File parses back into managed regions including the Decisions region.
    text = target.read_text(encoding="utf-8")
    parsed = {r.id: r for r in regions.find_regions(text)}
    assert DECISIONS_REGION_ID in parsed
    assert "non-negotiable-rules" in parsed
    decisions_body = parsed[DECISIONS_REGION_ID].body
    # Every D## id, summary, and rationale survives the round-trip.
    for did in decisions:
        assert f"### {did}: {did} policy summary" in decisions_body, did
        assert f"{did} rationale block" in decisions_body, did
    # Ids appear in lexicographic order in the rendered text.
    positions = [decisions_body.index(f"### {did}") for did in sorted(decisions)]
    assert positions == sorted(positions)
    # Manifest reports the typed-region delta correctly.
    assert DECISIONS_REGION_ID in result.regions_added
    composite_key = f"{target.as_posix()}::{DECISIONS_REGION_ID}"
    assert composite_key in manifest_after.generated


def test_render_agents_md_decisions_region_preserves_hand_edits(
    tmp_path: Path,
) -> None:
    """User content outside the Decisions managed region round-trips byte-stably."""
    target = tmp_path / "AGENTS.md"
    composed = _composed_with_blocks()
    state = _state_with_decisions({"D13": _decision("D13")})

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}), state=state)

    hand_edit = "\n\n## Hand-written addendum\n\nNot managed by the renderer.\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(hand_edit)
    before = target.read_text(encoding="utf-8")

    result, _ = render_agents_md(composed, target, manifest, state=state)
    after = target.read_text(encoding="utf-8")

    assert before == after, "decisions region re-render must preserve hand-edits"
    assert "## Hand-written addendum" in after
    assert result.hand_edits_preserved is True


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
