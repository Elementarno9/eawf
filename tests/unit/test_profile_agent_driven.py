"""Tests for the opt-in ``agent_driven`` profile (P27-I03-W03).

The ``agent_driven`` profile encodes two deliberate *divergences* from the
small-CL / trunk-based defaults that the bundled ``core`` profile renders,
plus a pointer to the decisions that ratified them:

- **phase = release** — every closed phase ships as at least a minor release;
  the phase-close commit bumps the package version and the merge tags it.
- **large phase-PR** — one PR per phase (≈ +10k lines is acceptable); do not
  split a phase into many small PRs; per-wave bisectable commits give the
  granularity inside the PR.
- **ADR pointer** — names Decision D10 (one-PR-per-phase cadence) + D07
  (rebase-merge) and the canonical ``docs/architecture/workflow.md`` reference
  so the divergence is auditable and reversible.

The contracts under test mirror the W02 ``quality`` profile suite:

- The body loads + validates against the closed ``ProfileBody`` schema.
- Every render block is the structured triad shape and carries a non-empty
  ``verification`` line (the load-bearing success criterion).
- Composing + rendering emits the divergence rules into AGENTS.md.
- The profile is opt-in (not in the built-in default enabled set).
- The default ``core``-only composition still renders the small-CL PR-cadence
  prose and does NOT pull in the agent-driven divergence blocks.

The XOR / partial-triad error paths on :class:`RenderBlock` are exercised in
``test_profile_body_v2.py``; this module asserts the shipped body satisfies
the structured-shape contract end to end.
"""

from __future__ import annotations

from pathlib import Path

from eawf.profiles import compose, load_profile
from eawf.profiles.models import ProfileBody
from eawf.render.agents_md import render_agents_md
from eawf.render.manifest import Manifest

_PROFILE_ID = "agent_driven"

#: Render-block ids the agent_driven profile contributes (divergence rules).
_DIVERGENCE_BLOCK_IDS = {
    "agent-driven-phase-equals-release",
    "agent-driven-large-phase-pr",
    "agent-driven-cadence-adr-pointer",
}


def test_agent_driven_profile_loads_and_validates() -> None:
    """``load_profile("agent_driven")`` returns a validated body with blocks."""
    body = load_profile(_PROFILE_ID)
    assert isinstance(body, ProfileBody)
    assert body.name == _PROFILE_ID
    assert body.version == "1.0"
    # The cadence profile carries no state extensions or instrument needs.
    assert body.state_extensions.fields_required == []
    assert body.instrument_requirements == []
    # It ships the divergence rules as render blocks.
    assert {block.id for block in body.render_blocks} == _DIVERGENCE_BLOCK_IDS


def test_agent_driven_profile_blocks_target_agents_md() -> None:
    """Every divergence block renders into AGENTS.md (the managed-rules file)."""
    body = load_profile(_PROFILE_ID)
    assert all(block.target == "AGENTS.md" for block in body.render_blocks)


def test_agent_driven_profile_every_block_is_structured_with_verification() -> None:
    """Each block is the structured triad and carries a non-empty verification.

    This is the wave's load-bearing criterion: a divergence rule that does not
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


def test_agent_driven_profile_block_ids_are_unique() -> None:
    """Render-block ids are the composition merge key, so they must be unique."""
    body = load_profile(_PROFILE_ID)
    ids = [block.id for block in body.render_blocks]
    assert len(ids) == len(set(ids))


def test_agent_driven_profile_renders_divergence_rules(tmp_path: Path) -> None:
    """Rendering the composed profile emits the phase=release + large-PR rules.

    Asserts the divergence content survives the full compose -> render path:
    the rendered AGENTS.md carries the phase=release semantics, the
    one-PR-per-phase / large-PR rule, and the ADR pointer (Decision ids +
    workflow doc).
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
    # phase = release: version bump on phase close.
    assert "at least a minor release" in rendered
    # large phase-PR: one PR per phase, do not split.
    assert "one PR per phase" in rendered
    assert "+10k lines" in rendered
    # ADR pointer: the ratifying decisions + canonical workflow reference.
    assert "D10" in rendered
    assert "D07" in rendered
    assert "docs/architecture/workflow.md" in rendered


def test_agent_driven_profile_composes_with_core() -> None:
    """Composing ``core + agent_driven`` preserves every divergence block."""
    composed = compose([load_profile("core"), load_profile(_PROFILE_ID)])
    composed_ids = {block.id for block in composed.render_blocks}
    assert _DIVERGENCE_BLOCK_IDS.issubset(composed_ids)
    assert _PROFILE_ID in composed.provenance["render_blocks"]


def test_agent_driven_profile_not_in_builtin_default() -> None:
    """The profile is opt-in: the built-in default enables only ``core``."""
    from eawf.config.defaults import built_in_defaults

    enabled = built_in_defaults().get("profiles", {}).get("enabled", [])
    assert _PROFILE_ID not in enabled
    assert enabled == ["core"]


def test_default_core_renders_small_cl_cadence_without_divergence(tmp_path: Path) -> None:
    """The default ``core``-only render keeps small-CL cadence, no divergence.

    The bundled default enables just ``core``; its ``branch-naming`` block
    frames PR cadence for human-team review ("PRs: one per phase (typical)").
    This render must NOT contain the agent-driven divergence blocks — those are
    opt-in. Asserts the two profiles render distinct surfaces.
    """
    composed = compose([load_profile("core")])
    target = tmp_path / "AGENTS.md"

    render_agents_md(composed, target, Manifest(version=1, generated={}))
    rendered = target.read_text(encoding="utf-8")

    # core's small-CL human-cadence framing survives.
    assert "PRs: one per phase (typical)" in rendered
    # The agent-driven divergence rules are absent from the default render.
    assert "at least a minor release" not in rendered
    assert "+10k lines" not in rendered
    core_ids = {block.id for block in compose([load_profile("core")]).render_blocks}
    assert _DIVERGENCE_BLOCK_IDS.isdisjoint(core_ids)
