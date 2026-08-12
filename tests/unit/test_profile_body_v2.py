"""Unit tests for the ProfileBody v2 closed schema.

The v2 schema adds three composability fields and a ``schema_version``
literal:

- ``schema_version: Literal["1.0"] = "1.0"`` — gate unknown future formats.
- ``conflicts_with: list[str] = []`` — profiles that cannot coexist.
- ``overrides: list[str] = []`` — profiles this one claims contributions over.
- ``dispatch_session_policy: Literal["fresh", "continue", "hybrid"] | None``
  — closed enum; ``None`` defers to skill / global default.

The corresponding :class:`ComposedProfile` v2 fields are also covered:
``dispatch_session_policy`` (last-non-``None``-wins), ``override_audit``
(field-path → chain), ``conflict_warnings`` (non-fatal warnings).

The tests exercise:

- ``ConfigDict(extra="forbid")`` rejects unknown keys on both shapes.
- Default values for every v2 field.
- Closed-enum rejection on ``dispatch_session_policy`` invalid values.
- ``schema_version`` rejection on unknown versions.
- Round-trip through ``model_dump`` / ``model_validate``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.platform.profiles import load_profile
from eawf.platform.profiles.models import (
    RENDER_BLOCK_SUMMARY_MAX,
    ComposedProfile,
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
)

# --- Schema v2 defaults ----------------------------------------------------


def test_profile_body_v2_defaults_for_new_fields() -> None:
    """Bodies that omit every v2 field default to empty / ``None``."""
    body = ProfileBody(name="alpha")
    assert body.schema_version == "1.0"
    assert body.conflicts_with == []
    assert body.overrides == []
    assert body.dispatch_session_policy is None


def test_profile_body_v2_round_trip_through_dump() -> None:
    """``model_dump`` + ``model_validate`` preserves every v2 field."""
    original = ProfileBody(
        name="alpha",
        conflicts_with=["beta"],
        overrides=["gamma"],
        dispatch_session_policy="hybrid",
    )
    dumped = original.model_dump(mode="python")
    restored = ProfileBody.model_validate(dumped)
    assert restored == original


def test_profile_body_v2_dispatch_session_policy_accepts_three_enums() -> None:
    for value in ("fresh", "continue", "hybrid"):
        body = ProfileBody(name="alpha", dispatch_session_policy=value)
        assert body.dispatch_session_policy == value


def test_profile_body_v2_dispatch_session_policy_accepts_none() -> None:
    body = ProfileBody(name="alpha", dispatch_session_policy=None)
    assert body.dispatch_session_policy is None


# --- Closed-shape rejection (extra="forbid") -------------------------------


def test_profile_body_v2_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ProfileBody(name="alpha", future_field="something")  # type: ignore[call-arg]
    assert (
        "Extra inputs are not permitted" in str(excinfo.value)
        or "extra" in str(excinfo.value).lower()
    )


def test_profile_body_v2_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError):
        ProfileBody.model_validate({"name": "alpha", "schema_version": "9.9"})


def test_profile_body_v2_rejects_unknown_dispatch_policy() -> None:
    with pytest.raises(ValidationError):
        ProfileBody.model_validate(
            {"name": "alpha", "dispatch_session_policy": "bogus"},
        )


def test_profile_body_v2_conflicts_with_must_be_string_list() -> None:
    with pytest.raises(ValidationError):
        ProfileBody.model_validate({"name": "alpha", "conflicts_with": [1, 2]})


def test_profile_body_v2_overrides_must_be_string_list() -> None:
    with pytest.raises(ValidationError):
        ProfileBody.model_validate({"name": "alpha", "overrides": {"not": "a list"}})


# --- Existing v1 fields still load ----------------------------------------


def test_profile_body_v2_keeps_v1_field_defaults() -> None:
    """Existing v1 callers that omit every v2 field still parse cleanly."""
    body = ProfileBody(
        name="alpha",
        state_extensions=StateExtensions(fields_required=["foo"]),
        instrument_requirements=[InstrumentReq(name="git")],
        render_blocks=[
            RenderBlock(id="block-1", target="AGENTS.md", body_template="x"),
        ],
        skills_referenced=["research"],
        hooks_referenced=["pre-commit"],
    )
    assert body.state_extensions.fields_required == ["foo"]
    assert body.instrument_requirements[0].name == "git"
    assert body.render_blocks[0].id == "block-1"
    assert body.skills_referenced == ["research"]
    assert body.hooks_referenced == ["pre-commit"]
    # v2 fields default safely
    assert body.conflicts_with == []
    assert body.overrides == []
    assert body.dispatch_session_policy is None


# --- RenderBlock body-shape XOR ------------------------------


def test_render_block_prose_shape_validates() -> None:
    """A legacy prose block (non-empty body_template, no triad) validates."""
    block = RenderBlock(id="rules", target="AGENTS.md", body_template="## Rules\n\n- one")
    assert block.body_template == "## Rules\n\n- one"
    assert block.rationale is None
    assert block.tier == "reference"
    assert block.is_structured is False


def test_render_block_structured_shape_validates() -> None:
    """A fully-populated triad (no body_template) validates as structured."""
    block = RenderBlock(
        id="verify-rule",
        target="AGENTS.md",
        rationale="Claims must be backed by evidence.",
        mechanism="Read source, grep call sites, inspect fixtures.",
        verification="Quote the implementation, not the design doc.",
    )
    assert block.is_structured is True
    assert block.body_template == ""
    assert block.rationale == "Claims must be backed by evidence."
    assert block.tier == "reference"


def test_render_block_accepts_tier0() -> None:
    """A block may opt into the short tier-0 AGENTS.md budget."""
    block = RenderBlock(
        id="core-rule",
        target="AGENTS.md",
        body_template="## Core\n\n- Keep this short.",
        tier="tier0",
    )
    assert block.tier == "tier0"


def test_render_block_rejects_unknown_tier() -> None:
    """Render block tiers are closed so budget accounting cannot drift."""
    with pytest.raises(ValidationError):
        RenderBlock.model_validate(
            {
                "id": "bad-tier",
                "target": "AGENTS.md",
                "body_template": "body",
                "tier": "hot",
            }
        )


# --- RenderBlock placement / summary pairing --------------------------------


def test_render_block_placement_defaults_to_root_without_summary() -> None:
    """An undeclared block keeps rendering its whole body into the managed file."""
    block = RenderBlock(id="core-rule", target="AGENTS.md", body_template="## Core")
    assert block.placement == "root"
    assert block.summary is None
    assert not block.is_reference_placed


def test_render_block_reference_placement_requires_summary() -> None:
    """Reference placement carries the one line the managed file will show."""
    block = RenderBlock(
        id="long-rule",
        target="AGENTS.md",
        body_template="## Long\n\nMany paragraphs.",
        placement="reference",
        summary="Do the thing.",
    )
    assert block.is_reference_placed
    assert block.summary == "Do the thing."


def test_render_block_rejects_reference_placement_without_summary() -> None:
    """A reference block with no summary would leave the root a bare link."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="no-summary",
            target="AGENTS.md",
            body_template="## Long",
            placement="reference",
        )
    assert "sets no summary" in str(excinfo.value)


def test_render_block_rejects_reference_placement_with_blank_summary() -> None:
    """A whitespace-only summary is as unusable as a missing one."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="blank-summary",
            target="AGENTS.md",
            body_template="## Long",
            placement="reference",
            summary="   ",
        )
    assert "sets no summary" in str(excinfo.value)


def test_render_block_rejects_summary_on_root_placement() -> None:
    """A root block renders its whole body, so a summary would go unread."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="stray-summary",
            target="AGENTS.md",
            body_template="## Short",
            summary="Do the thing.",
        )
    assert "summary is reserved for reference placement" in str(excinfo.value)


def test_render_block_accepts_a_summary_at_the_cap() -> None:
    """A summary of exactly the cap is legal -- the bound is inclusive."""
    block = RenderBlock(
        id="at-cap",
        target="AGENTS.md",
        body_template="## Long",
        placement="reference",
        summary="x" * RENDER_BLOCK_SUMMARY_MAX,
    )
    assert len(block.summary or "") == RENDER_BLOCK_SUMMARY_MAX


def test_render_block_rejects_an_over_cap_summary() -> None:
    """One character past the cap is rejected at the ingestion boundary.

    The summary is the line the always-loaded managed file keeps when a rule's
    body moves out, so an unbounded one would let the body creep back into the
    byte budget the move exists to protect.
    """
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="over-cap",
            target="AGENTS.md",
            body_template="## Long",
            placement="reference",
            summary="x" * (RENDER_BLOCK_SUMMARY_MAX + 1),
        )
    assert "at most 200 characters" in str(excinfo.value)


def test_shipped_profile_summaries_are_within_the_cap() -> None:
    """Every summary the shipped profiles declare clears the bound.

    The model would reject an over-cap body at load time, so this asserts the
    cap was chosen to fit the shipped prose rather than silently breaking a
    profile the composer never loads in this suite.
    """
    over_cap = [
        (profile_id, block.id, len(block.summary))
        for profile_id in ("core", "python", "research", "agent_driven", "quality")
        for block in load_profile(profile_id).render_blocks
        if block.summary is not None and len(block.summary) > RENDER_BLOCK_SUMMARY_MAX
    ]
    assert over_cap == []


def test_render_block_rejects_unknown_placement() -> None:
    """Placements are closed so the renderer cannot be handed a third mode."""
    with pytest.raises(ValidationError):
        RenderBlock.model_validate(
            {
                "id": "bad-placement",
                "target": "AGENTS.md",
                "body_template": "body",
                "placement": "appendix",
            }
        )


def test_render_block_rejects_both_prose_and_triad() -> None:
    """Filling both body_template and the full triad is invalid (not XOR)."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="both",
            target="AGENTS.md",
            body_template="## Prose",
            rationale="r",
            mechanism="m",
            verification="v",
        )
    assert "provide exactly one" in str(excinfo.value)


def test_render_block_rejects_neither_prose_nor_triad() -> None:
    """A block with empty body_template and no triad is invalid (not XOR)."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(id="empty", target="AGENTS.md")
    assert "provide exactly one" in str(excinfo.value)


def test_render_block_rejects_half_filled_triad_one_field() -> None:
    """Only ``rationale`` set (no mechanism/verification) fails validation."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(id="partial", target="AGENTS.md", rationale="just one")
    assert "incomplete structured triad" in str(excinfo.value)


def test_render_block_rejects_half_filled_triad_two_fields() -> None:
    """Two of three triad fields set (no verification) fails validation."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="partial2",
            target="AGENTS.md",
            rationale="r",
            mechanism="m",
        )
    assert "incomplete structured triad" in str(excinfo.value)


def test_render_block_rejects_partial_triad_alongside_prose() -> None:
    """Prose body plus a single triad field is an incomplete-triad error."""
    with pytest.raises(ValidationError) as excinfo:
        RenderBlock(
            id="mixed",
            target="AGENTS.md",
            body_template="## Prose",
            rationale="dangling",
        )
    assert "incomplete structured triad" in str(excinfo.value)


def test_render_block_structured_round_trips_through_dump() -> None:
    """``model_dump`` + ``model_validate`` preserves a structured block."""
    original = RenderBlock(
        id="verify-rule",
        target="AGENTS.md",
        rationale="r",
        mechanism="m",
        verification="v",
    )
    restored = RenderBlock.model_validate(original.model_dump(mode="python"))
    assert restored == original


def test_render_block_rejects_unknown_key() -> None:
    """``extra="forbid"`` rejects an unknown field on the block."""
    with pytest.raises(ValidationError):
        RenderBlock.model_validate(
            {"id": "x", "target": "AGENTS.md", "body_template": "y", "bogus": 1},
        )


# --- ComposedProfile v2 envelope shape -------------------------------------


def test_composed_profile_v2_defaults_for_new_fields() -> None:
    composed = ComposedProfile(name="composed:empty")
    assert composed.schema_version == "1.0"
    assert composed.dispatch_session_policy is None
    assert composed.override_audit == {}
    assert composed.conflict_warnings == []


def test_composed_profile_v2_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValidationError):
        ComposedProfile(name="composed:empty", future_field="x")  # type: ignore[call-arg]


def test_composed_profile_v2_rejects_unknown_dispatch_policy() -> None:
    with pytest.raises(ValidationError):
        ComposedProfile.model_validate(
            {"name": "composed:empty", "dispatch_session_policy": "off"},
        )


def test_composed_profile_v2_override_audit_must_map_strings_to_chains() -> None:
    with pytest.raises(ValidationError):
        ComposedProfile.model_validate(
            {"name": "composed:empty", "override_audit": {"path": "not-a-list"}},
        )


def test_composed_profile_v2_conflict_warnings_must_be_string_list() -> None:
    with pytest.raises(ValidationError):
        ComposedProfile.model_validate(
            {"name": "composed:empty", "conflict_warnings": [{"obj": True}]},
        )
