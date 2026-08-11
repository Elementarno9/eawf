"""Unit tests for ``eawf.surfaces.render.agents_md``.

Covers:

- Per-block managed-region emission with hashes parseable by
  :mod:`eawf.surfaces.render.regions`.
- Re-render preserves hand-written content byte-stable.
- Manifest gains entries (target, region_id, version, hash) for each region.
- Render blocks targeting non-AGENTS.md files are filtered out.
- Atomic write uses tempfile + ``os.replace`` (mock-based).
- Re-rendering an unchanged composed profile reports ``regions_unchanged``
  and leaves ``regions_updated`` empty.
- ``placement: reference`` blocks keep one line in the managed file and write
  their full body to ``docs/rules/<id>.md``, inside a managed region carrying a
  manifest row so the expansion is drift-checked like an inline block.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.platform.render_block import RenderBlockTier
from eawf.surfaces.render import regions
from eawf.surfaces.render.agents_md import (
    ENTITY_TITLE_MAX,
    ZONE_REFERENCE_REGION_ID,
    ZONE_TIER0_REGION_ID,
    ReferenceCollisionError,
    RenderResult,
    lint_entity_title,
    reference_file_path,
    render_agents_md,
    render_reference_document,
    render_reference_line,
)
from eawf.surfaces.render.drift import detect_drift
from eawf.surfaces.render.manifest import Manifest


def _make_composed(blocks: list[RenderBlock]) -> ComposedProfile:
    """Build a minimal ComposedProfile carrying the supplied render_blocks."""
    return ComposedProfile(
        name="test",
        version="1.0",
        description="",
        render_blocks=blocks,
    )


def _block(
    region_id: str,
    body: str,
    *,
    target: str = "AGENTS.md",
    version: str = "1.0",
    tier: RenderBlockTier = "reference",
) -> RenderBlock:
    """Build a RenderBlock whose body_template is the literal *body* string."""
    return RenderBlock(id=region_id, target=target, body_template=body, version=version, tier=tier)


def test_render_agents_md_writes_managed_regions_for_each_block(tmp_path: Path) -> None:
    """Each AGENTS.md-targeted block round-trips through find_regions with the right hash."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("rules", "## Rules\n\n- one\n- two"),
            _block("style", "## Style\n\n- f-strings only"),
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    parsed = regions.find_regions(text)
    found = {r.id: r for r in parsed}
    # Both blocks are reference-tier (the default), so they land in Zone 2
    # behind its boundary marker; no Zone-1 marker is emitted.
    assert set(found.keys()) == {ZONE_REFERENCE_REGION_ID, "rules", "style"}
    assert ZONE_TIER0_REGION_ID not in found
    assert found["rules"].body == "## Rules\n\n- one\n- two"
    assert found["style"].body == "## Style\n\n- f-strings only"
    # Hashes match — declared on marker == compute_hash(body).
    assert found["rules"].declared_hash == regions.compute_hash(found["rules"].body)
    assert found["style"].declared_hash == regions.compute_hash(found["style"].body)
    assert result.regions_added == [ZONE_REFERENCE_REGION_ID, "rules", "style"]
    assert result.regions_updated == []
    assert result.regions_unchanged == []


def test_render_agents_md_re_render_preserves_user_content(tmp_path: Path) -> None:
    """A hand-edited paragraph below a managed region survives a re-render byte-for-byte."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules\n\n- one")])
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    user_addition = "\n\n## User notes\n\nThis paragraph was hand-written.\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(user_addition)
    before = target.read_text(encoding="utf-8")

    result, _ = render_agents_md(composed, target, manifest)
    after = target.read_text(encoding="utf-8")

    assert before == after, "re-render must be byte-stable when nothing changed"
    assert "## User notes" in after
    assert "This paragraph was hand-written." in after
    assert result.hand_edits_preserved is True
    # The zone-boundary marker rides the same insert-or-replace path, so a
    # no-op re-render reports it unchanged alongside the block.
    assert result.regions_unchanged == [ZONE_REFERENCE_REGION_ID, "rules"]


def test_render_agents_md_updates_manifest(tmp_path: Path) -> None:
    """Manifest gains one entry per emitted region with correct fields."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("alpha", "alpha body", version="1.0"),
            _block("beta", "beta body", version="2.5"),
        ]
    )

    _, manifest = render_agents_md(
        composed,
        target,
        Manifest(version=1, generated={}),
        generator="profile:test",
    )

    # Keys are stored in POSIX form for cross-platform stability. The Zone-2
    # boundary marker is a managed region too, so it earns a manifest entry.
    target_str = target.as_posix()
    keys = set(manifest.generated.keys())
    assert keys == {
        f"{target_str}::{ZONE_REFERENCE_REGION_ID}",
        f"{target_str}::alpha",
        f"{target_str}::beta",
    }

    alpha_entry = manifest.generated[f"{target_str}::alpha"]
    assert alpha_entry.target == target_str
    assert alpha_entry.region_id == "alpha"
    assert alpha_entry.version == "1.0"
    assert alpha_entry.hash == regions.compute_hash("alpha body")
    assert alpha_entry.generator == "profile:test"
    # ISO 8601 with timezone offset is sufficient — exact instant is irrelevant.
    assert "T" in alpha_entry.generated_at
    assert alpha_entry.generated_at.endswith("+00:00")

    beta_entry = manifest.generated[f"{target_str}::beta"]
    assert beta_entry.version == "2.5"
    assert beta_entry.hash == regions.compute_hash("beta body")


def test_render_agents_md_filters_by_target(tmp_path: Path) -> None:
    """Render blocks targeting non-AGENTS.md files are skipped silently."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("rules", "## Rules", target="AGENTS.md"),
            _block("skill-foo", "skill body", target=".claude/skills/foo.md"),
            _block("agent-bar", "agent body", target=".claude/agents/bar.md"),
        ]
    )

    result, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    region_ids = {r.id for r in regions.find_regions(text)}
    assert region_ids == {ZONE_REFERENCE_REGION_ID, "rules"}
    assert result.regions_added == [ZONE_REFERENCE_REGION_ID, "rules"]
    # Manifest only holds the AGENTS.md regions (the Zone-2 marker + the block).
    assert set(manifest.generated.keys()) == {
        f"{target.as_posix()}::{ZONE_REFERENCE_REGION_ID}",
        f"{target.as_posix()}::rules",
    }


def test_render_agents_md_atomic_write(tmp_path: Path) -> None:
    """The renderer writes via a sibling tempfile then ``os.replace``."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules")])

    # Patch the shared helper's ``os.replace`` — extraction in
    # :mod:`eawf.surfaces.render._atomic` is the actual call site now.
    with patch(
        "eawf.surfaces.render._atomic.os.replace",
        wraps=__import__("os").replace,
    ) as spy:
        render_agents_md(composed, target, Manifest(version=1, generated={}))

    assert spy.called, "render must go through os.replace"
    src_arg = spy.call_args.args[0]
    dst_arg = spy.call_args.args[1]
    assert str(dst_arg) == str(target)
    assert str(src_arg).startswith(str(target) + ".tmp.")


def test_render_agents_md_unchanged_no_update_needed(tmp_path: Path) -> None:
    """Two consecutive renders of the same composed profile: second is a no-op delta."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("alpha", "alpha body"),
            _block("beta", "beta body"),
        ]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    result, _ = render_agents_md(composed, target, manifest)

    assert sorted(result.regions_unchanged) == sorted([ZONE_REFERENCE_REGION_ID, "alpha", "beta"])
    assert result.regions_updated == []
    assert result.regions_added == []


def test_render_agents_md_updated_when_body_changes(tmp_path: Path) -> None:
    """Changing a block's body marks it ``regions_updated`` (not ``unchanged``)."""
    target = tmp_path / "AGENTS.md"
    composed_v1 = _make_composed([_block("rules", "## Rules v1")])
    _, manifest = render_agents_md(composed_v1, target, Manifest(version=1, generated={}))

    composed_v2 = _make_composed([_block("rules", "## Rules v2")])
    result, _ = render_agents_md(composed_v2, target, manifest)

    assert result.regions_updated == ["rules"]
    # The Zone-2 boundary marker body did not change, so it stays unchanged.
    assert result.regions_unchanged == [ZONE_REFERENCE_REGION_ID]
    assert result.regions_added == []
    text = target.read_text(encoding="utf-8")
    region = regions.extract_region(text, "rules")
    assert region is not None
    assert region.body == "## Rules v2"


def test_render_agents_md_returns_render_result_with_target(tmp_path: Path) -> None:
    """``RenderResult.target`` is the path the renderer wrote."""
    target = tmp_path / "nested" / "AGENTS.md"
    composed = _make_composed([_block("rules", "## Rules")])
    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))
    assert isinstance(result, RenderResult)
    assert result.target == target


def test_render_agents_md_preserves_other_target_manifest_entries(tmp_path: Path) -> None:
    """Manifest entries for OTHER targets survive an AGENTS.md render."""
    target = tmp_path / "AGENTS.md"
    other_target = str(tmp_path / ".claude" / "skills" / "foo.md")
    seed = Manifest(
        version=1,
        generated={
            f"{other_target}::skill-foo": __import__(
                "eawf.surfaces.render.manifest", fromlist=["ManifestEntry"]
            ).ManifestEntry(
                target=other_target,
                region_id="skill-foo",
                version="1.0",
                hash="0123456789abcdef",
                generator="profile:other",
                generated_at="2026-01-01T00:00:00+00:00",
            ),
        },
    )
    composed = _make_composed([_block("rules", "## Rules")])

    _, manifest = render_agents_md(composed, target, seed)

    assert f"{other_target}::skill-foo" in manifest.generated
    assert f"{target.as_posix()}::rules" in manifest.generated


def test_render_agents_md_structured_block_emits_triad_layout(tmp_path: Path) -> None:
    """A structured RenderBlock renders Rationale/Mechanism/Verification headings."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            RenderBlock(
                id="verify-rule",
                target="AGENTS.md",
                rationale="Claims must be backed by evidence.",
                mechanism="Read source, grep call sites, inspect fixtures.",
                verification="Quote the implementation, not the design doc.",
            )
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    region = regions.extract_region(text, "verify-rule")
    assert region is not None
    body = region.body
    assert "### Rationale" in body
    assert "### Mechanism" in body
    assert "### Verification" in body
    # Sub-headings appear in triad order.
    assert (
        body.index("### Rationale") < body.index("### Mechanism") < body.index("### Verification")
    )
    # The triad values are emitted under their headings.
    assert "Claims must be backed by evidence." in body
    assert "Read source, grep call sites, inspect fixtures." in body
    assert "Quote the implementation, not the design doc." in body
    assert result.regions_added == [ZONE_REFERENCE_REGION_ID, "verify-rule"]


def test_render_agents_md_structured_block_is_byte_stable_on_rerender(tmp_path: Path) -> None:
    """Re-rendering a structured block is a no-op delta (round-trips)."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            RenderBlock(
                id="verify-rule",
                target="AGENTS.md",
                rationale="r",
                mechanism="m",
                verification="v",
            )
        ]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    before = target.read_text(encoding="utf-8")
    result, _ = render_agents_md(composed, target, manifest)
    after = target.read_text(encoding="utf-8")

    assert before == after
    assert result.regions_unchanged == [ZONE_REFERENCE_REGION_ID, "verify-rule"]
    assert result.regions_updated == []
    assert result.regions_added == []


def test_render_agents_md_mixed_prose_and_structured_blocks(tmp_path: Path) -> None:
    """Prose and structured blocks coexist in one render."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("rules", "## Rules\n\n- one"),
            RenderBlock(
                id="verify-rule",
                target="AGENTS.md",
                rationale="r",
                mechanism="m",
                verification="v",
            ),
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    assert regions.extract_region(text, "rules").body == "## Rules\n\n- one"  # type: ignore[union-attr]
    structured_body = regions.extract_region(text, "verify-rule").body  # type: ignore[union-attr]
    assert "### Rationale" in structured_body
    assert result.regions_added == [ZONE_REFERENCE_REGION_ID, "rules", "verify-rule"]


def _region_spans(text: str) -> dict[str, tuple[int, int]]:
    """Return ``{region_id: (start, end)}`` byte spans for every parsed region."""
    return {r.id: r.span for r in regions.find_regions(text)}


def test_render_agents_md_partitions_tier0_into_zone1_reference_into_zone2(
    tmp_path: Path,
) -> None:
    """Mixed tier0 + reference blocks land in their own zones (affordance_parity both ways).

    The renderer must put every ``tier0`` block id in the Zone-1 region (after
    the Zone-1 marker, before the Zone-2 marker) and every ``reference`` block
    id in the Zone-2 region (after the Zone-2 marker). Neither direction may
    leak: no tier0 id appears in the Zone-2 span, and no reference id appears in
    the Zone-1 span.
    """
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            # Deliberately interleaved in source order to prove the partition
            # is keyed on tier, not on declaration order.
            _block("ref-a", "## Ref A", tier="reference"),
            _block("t0-a", "## Tier0 A", tier="tier0"),
            _block("ref-b", "## Ref B", tier="reference"),
            _block("t0-b", "## Tier0 B", tier="tier0"),
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    spans = _region_spans(text)
    # Both zone markers are emitted (each tier is non-empty).
    assert ZONE_TIER0_REGION_ID in spans
    assert ZONE_REFERENCE_REGION_ID in spans

    zone1_start = spans[ZONE_TIER0_REGION_ID][0]
    zone2_start = spans[ZONE_REFERENCE_REGION_ID][0]
    assert zone1_start < zone2_start, "Zone 1 must render before Zone 2"

    tier0_ids = {"t0-a", "t0-b"}
    reference_ids = {"ref-a", "ref-b"}

    # Affordance parity, forward: every composed tier0 block id is present in
    # the Zone-1 region (between the Zone-1 marker and the Zone-2 marker).
    for block_id in tier0_ids:
        assert block_id in spans, f"tier0 block {block_id!r} dropped from render"
        assert zone1_start < spans[block_id][0] < zone2_start, (
            f"tier0 block {block_id!r} did not land in the Zone-1 region"
        )

    # Affordance parity, reverse: no tier0 block id leaks into the Zone-2 span,
    # and every reference block id sits after the Zone-2 marker.
    for block_id in reference_ids:
        assert block_id in spans, f"reference block {block_id!r} dropped from render"
        assert spans[block_id][0] > zone2_start, (
            f"reference block {block_id!r} did not land in the Zone-2 region"
        )
    for block_id in tier0_ids:
        assert spans[block_id][0] < zone2_start, (
            f"tier0 block {block_id!r} leaked into the Zone-2 region"
        )

    # The render order reflects the partition: Zone-1 marker, tier0 blocks,
    # Zone-2 marker, reference blocks. Source order is preserved within a tier.
    assert result.regions_added == [
        ZONE_TIER0_REGION_ID,
        "t0-a",
        "t0-b",
        ZONE_REFERENCE_REGION_ID,
        "ref-a",
        "ref-b",
    ]


def test_render_agents_md_zero_tier0_emits_no_empty_zone1_region(tmp_path: Path) -> None:
    """A profile with zero tier0 blocks emits no Zone-1 region; no tier0 leaks into Zone 2.

    The negative path: when every targeted block is reference-tier, the
    renderer must not stamp an empty Zone-1 boundary marker. The reference
    blocks still render behind the Zone-2 marker, and no ``tier0`` region id
    appears anywhere in the output.
    """
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("ref-a", "## Ref A", tier="reference"),
            _block("ref-b", "## Ref B", tier="reference"),
        ]
    )

    result, _ = render_agents_md(composed, target, Manifest(version=1, generated={}))

    text = target.read_text(encoding="utf-8")
    spans = _region_spans(text)
    # No Zone-1 region at all — not even an empty one.
    assert ZONE_TIER0_REGION_ID not in spans
    assert ZONE_TIER0_REGION_ID not in result.regions_added
    # Zone 2 exists and holds both reference blocks behind its marker.
    assert ZONE_REFERENCE_REGION_ID in spans
    zone2_start = spans[ZONE_REFERENCE_REGION_ID][0]
    assert spans["ref-a"][0] > zone2_start
    assert spans["ref-b"][0] > zone2_start
    assert result.regions_added == [ZONE_REFERENCE_REGION_ID, "ref-a", "ref-b"]


def test_render_agents_md_empty_compose_no_regions(tmp_path: Path) -> None:
    """A composed profile with zero AGENTS.md blocks: file is created empty / unchanged.

    The renderer always emits a POSIX-compliant trailing ``\\n`` so
    ``end-of-file-fixer`` is a no-op; with zero blocks the file therefore
    contains exactly one newline byte (still effectively empty content).
    """
    target = tmp_path / "AGENTS.md"
    composed = _make_composed([])

    result, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "\n"
    assert result.regions_added == []
    assert result.regions_updated == []
    assert result.regions_unchanged == []
    assert manifest.generated == {}


def test_lint_entity_title_clean_title_has_no_violations() -> None:
    """A bounded imperative noun-phrase with no trailing period is clean."""
    assert lint_entity_title("Add bounded title to entities") == []


def test_lint_entity_title_at_cap_is_clean() -> None:
    """A title of exactly the cap length is accepted (off-by-one boundary)."""
    title = "x" * ENTITY_TITLE_MAX
    assert len(title) == 72
    assert lint_entity_title(title) == []


def test_lint_entity_title_over_cap_is_flagged() -> None:
    """A title one char over the cap is flagged as over-cap (off-by-one boundary)."""
    title = "x" * (ENTITY_TITLE_MAX + 1)
    violations = lint_entity_title(title)
    assert len(violations) == 1
    assert "73 chars" in violations[0]
    assert f"{ENTITY_TITLE_MAX}-char cap" in violations[0]


def test_lint_entity_title_trailing_period_is_flagged() -> None:
    """A within-cap title ending in a period is flagged for the trailing period."""
    violations = lint_entity_title("Adds a bounded title.")
    assert len(violations) == 1
    assert "trailing period" in violations[0]


def test_lint_entity_title_trailing_period_after_whitespace_is_flagged() -> None:
    """Trailing whitespace after the period does not hide it from the check."""
    violations = lint_entity_title("Adds a bounded title.  ")
    assert len(violations) == 1
    assert "trailing period" in violations[0]


def test_lint_entity_title_both_violations_reported_in_order() -> None:
    """An over-cap title that also ends in a period yields both messages, cap first."""
    title = "x" * ENTITY_TITLE_MAX + "yz."
    violations = lint_entity_title(title)
    assert len(violations) == 2
    assert "char cap" in violations[0]
    assert "trailing period" in violations[1]


def test_lint_entity_title_empty_string_is_clean_for_style() -> None:
    """Empty title trips neither style rule (the model's min_length=1 owns that).

    ``lint_entity_title`` is a style backstop for the over-cap and
    trailing-period modes only; the non-empty invariant is enforced by the
    Pydantic ``min_length=1`` bound at the ingestion boundary, so the linter
    deliberately stays silent on the empty case rather than duplicating it.
    """
    assert lint_entity_title("") == []


@pytest.mark.parametrize(
    "title",
    [
        "Enforce sandbox deny-list at dispatch",
        "Add title+description to entities",
        "Surface entity description in detail renders",
    ],
)
def test_lint_entity_title_accepts_real_wave_titles(title: str) -> None:
    """Representative real wave titles from this phase pass the linter."""
    assert lint_entity_title(title) == []


# ---- reference placement ---------------------------------------------------


def _reference_block(region_id: str, body: str, summary: str) -> RenderBlock:
    """Build a reference-placed block whose expansion holds *body*."""
    return RenderBlock(
        id=region_id,
        target="AGENTS.md",
        body_template=body,
        placement="reference",
        summary=summary,
    )


def test_render_agents_md_moves_reference_block_body_to_its_own_file(tmp_path: Path) -> None:
    """A reference-placed block leaves one line in the root and a full sibling file."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [
            _block("kept", "## Kept\n\n- stays inline"),
            _reference_block("moved", "## Moved\n\n- long expansion", "Follow the moved rule."),
        ]
    )

    render_agents_md(composed, target, Manifest(version=1, generated={}))

    root_text = target.read_text(encoding="utf-8")
    assert "- stays inline" in root_text
    assert "- long expansion" not in root_text
    assert "`moved` — Follow the moved rule." in root_text
    assert "docs/rules/moved.md" in root_text

    expansion = reference_file_path(tmp_path, "moved").read_text(encoding="utf-8")
    assert "- long expansion" in expansion
    assert "Follow the moved rule." in expansion


def test_render_agents_md_keeps_a_managed_region_for_a_reference_block(tmp_path: Path) -> None:
    """The moved block keeps its region id, so drift + byte-cap naming still work."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    found = {r.id for r in regions.find_regions(target.read_text(encoding="utf-8"))}
    assert "moved" in found
    assert f"{target.as_posix()}::moved" in manifest.generated


def test_render_agents_md_wraps_the_expansion_in_a_managed_region(tmp_path: Path) -> None:
    """The expansion carries markers, so its body has a hash to check against."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )

    render_agents_md(composed, target, Manifest(version=1, generated={}))

    expansion = reference_file_path(tmp_path, "moved")
    parsed = regions.find_regions(expansion.read_text(encoding="utf-8"))
    assert [r.id for r in parsed] == ["moved"]
    assert parsed[0].declared_hash == regions.compute_hash(parsed[0].body)
    assert "## Moved" in parsed[0].body
    # The banner stays outside the region so the hash covers the rule text only.
    assert expansion.read_text(encoding="utf-8").startswith("<!-- Generated from the eawf")


def test_render_agents_md_registers_a_manifest_entry_for_the_expansion(tmp_path: Path) -> None:
    """The moved file gets its own manifest row keyed by ``<path>::<region_id>``."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    expansion = reference_file_path(tmp_path, "moved")
    entry = manifest.generated[f"{expansion.as_posix()}::moved"]
    assert entry.target == expansion.as_posix()
    assert entry.region_id == "moved"
    assert entry.version == "1.0"
    on_disk = regions.extract_region(expansion.read_text(encoding="utf-8"), "moved")
    assert on_disk is not None
    assert entry.hash == regions.compute_hash(on_disk.body)


def test_detect_drift_reports_ok_for_a_freshly_rendered_expansion(tmp_path: Path) -> None:
    """A just-written expansion hashes to its manifest row."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    reports = detect_drift(reference_file_path(tmp_path, "moved"), manifest)
    assert [(r.id, r.kind) for r in reports] == [("moved", "ok")]


def test_detect_drift_flags_a_hand_edited_expansion(tmp_path: Path) -> None:
    """Hand-editing a moved rule is now detectable, not silent."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    expansion = reference_file_path(tmp_path, "moved")
    tampered = expansion.read_text(encoding="utf-8").replace("body", "smuggled obligation")
    expansion.write_text(tampered, encoding="utf-8")

    reports = detect_drift(expansion, manifest)
    assert [(r.id, r.kind) for r in reports] == [("moved", "hand-edited")]


def test_detect_drift_flags_a_deleted_expansion(tmp_path: Path) -> None:
    """Deleting a moved rule reports ``missing`` rather than passing unnoticed."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    expansion = reference_file_path(tmp_path, "moved")
    expansion.unlink()

    reports = detect_drift(expansion, manifest)
    assert [(r.id, r.kind) for r in reports] == [("moved", "missing")]


def test_render_agents_md_drops_the_manifest_row_of_an_unmoved_block(tmp_path: Path) -> None:
    """A block that flips back to root placement leaves no phantom expansion row."""
    target = tmp_path / "AGENTS.md"
    reference_composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )
    _, manifest = render_agents_md(composed=reference_composed, target=target, manifest=Manifest())
    expansion_key = f"{reference_file_path(tmp_path, 'moved').as_posix()}::moved"
    assert expansion_key in manifest.generated

    root_composed = _make_composed([_block("moved", "## Moved\n\nbody")])
    _, after = render_agents_md(root_composed, target, manifest)

    assert expansion_key not in after.generated


def test_render_agents_md_reference_placement_is_byte_stable_across_renders(
    tmp_path: Path,
) -> None:
    """Re-rendering rewrites both halves identically (no hash-of-now drift)."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )
    expansion = reference_file_path(tmp_path, "moved")

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    first_root, first_expansion = target.read_bytes(), expansion.read_bytes()

    render_agents_md(composed, target, manifest)

    assert target.read_bytes() == first_root
    assert expansion.read_bytes() == first_expansion


def test_render_agents_md_reclaims_an_expansion_that_stopped_being_moved(
    tmp_path: Path,
) -> None:
    """A block flipping back to root placement takes its expansion file with it."""
    target = tmp_path / "AGENTS.md"
    _, manifest = render_agents_md(
        _make_composed([_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]),
        target,
        Manifest(version=1, generated={}),
    )
    expansion = reference_file_path(tmp_path, "moved")
    assert expansion.exists()

    render_agents_md(_make_composed([_block("moved", "## Moved\n\nbody")]), target, manifest)

    assert not expansion.exists()


def test_render_agents_md_reclaims_the_expansion_of_a_dropped_block(tmp_path: Path) -> None:
    """Dropping the block entirely (profile disabled) reclaims its expansion too."""
    target = tmp_path / "AGENTS.md"
    _, manifest = render_agents_md(
        _make_composed(
            [
                _reference_block("kept", "## Kept\n\nbody", "Follow the kept rule."),
                _reference_block("dropped", "## Dropped\n\nbody", "Follow the dropped rule."),
            ]
        ),
        target,
        Manifest(version=1, generated={}),
    )

    render_agents_md(
        _make_composed([_reference_block("kept", "## Kept\n\nbody", "Follow the kept rule.")]),
        target,
        manifest,
    )

    assert reference_file_path(tmp_path, "kept").exists()
    assert not reference_file_path(tmp_path, "dropped").exists()


def test_render_agents_md_reclaim_spares_a_hand_written_file(tmp_path: Path) -> None:
    """The sweep only deletes files carrying the generated banner."""
    target = tmp_path / "AGENTS.md"
    handwritten = reference_file_path(tmp_path, "house-style")
    handwritten.parent.mkdir(parents=True, exist_ok=True)
    handwritten.write_text("# House style\n\nHand-written, not ours.\n", encoding="utf-8")

    render_agents_md(
        _make_composed([_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]),
        target,
        Manifest(version=1, generated={}),
    )

    assert handwritten.read_text(encoding="utf-8") == "# House style\n\nHand-written, not ours.\n"


def test_render_agents_md_refuses_to_overwrite_a_foreign_file(tmp_path: Path) -> None:
    """An expansion never clobbers a same-named file the renderer did not write."""
    target = tmp_path / "AGENTS.md"
    squatter = reference_file_path(tmp_path, "moved")
    squatter.parent.mkdir(parents=True, exist_ok=True)
    squatter.write_text("# Pre-existing\n\nSomeone else's page.\n", encoding="utf-8")
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nbody", "Follow the moved rule.")]
    )

    with pytest.raises(ReferenceCollisionError, match=r"moved\.md"):
        render_agents_md(composed, target, Manifest(version=1, generated={}))

    assert squatter.read_text(encoding="utf-8") == "# Pre-existing\n\nSomeone else's page.\n"


def test_render_agents_md_overwrites_its_own_expansion(tmp_path: Path) -> None:
    """A banner-carrying expansion is the renderer's own, so a re-render rewrites it."""
    target = tmp_path / "AGENTS.md"
    composed = _make_composed(
        [_reference_block("moved", "## Moved\n\nfirst body", "Follow the moved rule.")]
    )
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))

    updated = _make_composed(
        [_reference_block("moved", "## Moved\n\nsecond body", "Follow the moved rule.")]
    )
    render_agents_md(updated, target, manifest)

    assert "second body" in reference_file_path(tmp_path, "moved").read_text(encoding="utf-8")


def test_render_reference_line_rejects_a_root_placed_block() -> None:
    """A root-placed block has no summary, so asking for its line is a caller bug."""
    block = _block("kept", "## Kept")
    with pytest.raises(ValueError, match="not reference-placed"):
        render_reference_line(block)


def test_render_reference_line_rejects_a_summary_less_reference_block() -> None:
    """The two guard arms report distinct causes, not one catch-all message."""
    block = RenderBlock.model_construct(
        id="moved",
        target="AGENTS.md",
        body_template="## Moved",
        placement="reference",
        summary=None,
        version="1.0",
    )
    with pytest.raises(ValueError, match="has no summary"):
        render_reference_line(block)


def test_render_reference_document_rejects_a_root_placed_block() -> None:
    """Same fail-fast contract on the document builder."""
    block = _block("kept", "## Kept")
    with pytest.raises(ValueError, match="not reference-placed"):
        render_reference_document(block, "## Kept")


def test_render_reference_document_rejects_a_summary_less_reference_block() -> None:
    """The document builder splits the same two arms as the line builder."""
    block = RenderBlock.model_construct(
        id="moved",
        target="AGENTS.md",
        body_template="## Moved",
        placement="reference",
        summary=None,
        version="1.0",
    )
    with pytest.raises(ValueError, match="has no summary"):
        render_reference_document(block, "## Moved")


def test_reference_file_path_lands_under_the_rule_reference_dir(tmp_path: Path) -> None:
    """The expansion path is ``<root>/docs/rules/<id>.md``, POSIX-stable."""
    path = reference_file_path(tmp_path, "moved")
    assert path.parent == tmp_path / "docs" / "rules"
    assert path.name == "moved.md"
