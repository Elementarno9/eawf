"""Tests for the opt-in ``quality`` code-craft profile (P27-I03-W02).

The ``quality`` profile ships the code-craft rules as structured RenderBlock
triads (the P27-I03-W01 schema). The contracts under test:

- The profile body loads and validates against the closed ``ProfileBody``
  schema (``extra="forbid"``) via the layered loader.
- Every render block is the *structured* triad shape, and each carries a
  non-empty ``verification`` line — that is the load-bearing success
  criterion for this wave.
- Composing the profile and rendering it into AGENTS.md emits the rules with
  a ``### Verification`` sub-heading whose body is the block's verification
  text (so the verification line survives the full compose -> render path).
- The profile is opt-in: it is not part of the built-in default enabled set.

The XOR / partial-triad *error paths* on :class:`RenderBlock` are exercised
exhaustively in ``test_profile_body_v2.py``; this module asserts the shipped
body satisfies the structured-shape contract end to end.
"""

from __future__ import annotations

from pathlib import Path

from eawf.platform.profiles import compose, load_profile
from eawf.platform.profiles.models import ProfileBody
from eawf.surfaces.render.agents_md import render_agents_md
from eawf.surfaces.render.manifest import Manifest

_PROFILE_ID = "quality"


def test_quality_profile_loads_and_validates() -> None:
    """``load_profile("quality")`` returns a validated body with render blocks."""
    body = load_profile(_PROFILE_ID)
    assert isinstance(body, ProfileBody)
    assert body.name == _PROFILE_ID
    assert body.version == "1.0"
    # The code-craft profile carries no state extensions or instrument needs.
    assert body.state_extensions.fields_required == []
    assert body.instrument_requirements == []
    # It ships at least one code-craft rule as a render block.
    assert len(body.render_blocks) >= 1


def test_quality_profile_blocks_target_agents_md() -> None:
    """Every code-craft block renders into AGENTS.md (the managed-rules file)."""
    body = load_profile(_PROFILE_ID)
    assert all(block.target == "AGENTS.md" for block in body.render_blocks)


def test_quality_profile_every_block_is_structured_with_verification() -> None:
    """Each block is the structured triad and carries a non-empty verification.

    This is the wave's load-bearing criterion: a code-craft rule that does not
    carry a verification line cannot be checked by a reviewer, only asserted.
    """
    body = load_profile(_PROFILE_ID)
    for block in body.render_blocks:
        assert block.is_structured, f"block {block.id!r} is not the structured triad"
        assert block.rationale is not None and block.rationale.strip(), block.id
        assert block.mechanism is not None and block.mechanism.strip(), block.id
        assert block.verification is not None and block.verification.strip(), block.id
        # Structured blocks must leave the prose body empty (XOR contract).
        assert block.body_template == ""


def test_quality_profile_block_ids_are_unique() -> None:
    """Render-block ids are the composition merge key, so they must be unique."""
    body = load_profile(_PROFILE_ID)
    ids = [block.id for block in body.render_blocks]
    assert len(ids) == len(set(ids))


def test_quality_profile_composes_with_core() -> None:
    """Composing ``core + quality`` preserves every code-craft block by id."""
    composed = compose([load_profile("core"), load_profile(_PROFILE_ID)])
    quality_ids = {block.id for block in load_profile(_PROFILE_ID).render_blocks}
    composed_ids = {block.id for block in composed.render_blocks}
    assert quality_ids.issubset(composed_ids)
    assert _PROFILE_ID in composed.provenance["render_blocks"]


def test_quality_profile_renders_verification_subheadings(tmp_path: Path) -> None:
    """Rendering the composed profile emits one ``### Verification`` per block.

    Asserts the verification line survives the full compose -> render path: the
    structured-triad renderer (``AGENTS.md.j2``) emits a fixed
    Rationale / Mechanism / Verification layout, and each block's verification
    body appears verbatim in the rendered AGENTS.md.
    """
    body = load_profile(_PROFILE_ID)
    composed = compose([body])
    target = tmp_path / "AGENTS.md"

    render_agents_md(composed, target, Manifest(version=1, generated={}))
    rendered = target.read_text(encoding="utf-8")

    block_count = len(body.render_blocks)
    assert rendered.count("### Verification") == block_count
    assert rendered.count("### Rationale") == block_count
    assert rendered.count("### Mechanism") == block_count
    # Each block's verification body text lands in the rendered file.
    for block in body.render_blocks:
        assert block.verification is not None
        first_line = block.verification.strip().splitlines()[0]
        assert first_line in rendered, f"verification for {block.id!r} missing from render"


def test_quality_profile_not_in_builtin_default() -> None:
    """The profile is opt-in: the built-in default enables only ``core``."""
    from eawf.kernel.config.defaults import built_in_defaults

    enabled = built_in_defaults().get("profiles", {}).get("enabled", [])
    assert _PROFILE_ID not in enabled
