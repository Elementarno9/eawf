"""Golden-output regression tests for ``eawf.surfaces.render.agents_md``.

For each fixture combo, render to a temp path and assert byte-equality
with the committed fixture under ``tests/golden/agents_md/``.

A failure here means the renderer's output drifted — either the template,
the composer, or one of the source profile bodies changed. Regenerate with
``uv run eawf snapshot update --kind agents_md`` and commit the new bytes
alongside the change; never hand-edit a golden.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eawf.observability.doctor.checks import CODEX_PROJECT_DOC_BYTE_CAP
from eawf.platform.lint.eawf014_no_manual_wrap import check_source
from eawf.platform.profiles import compose, load_profile
from eawf.surfaces.render.agents_md import (
    measure_agents_md_byte_cap,
    reference_file_path,
    render_agents_md,
)
from eawf.surfaces.render.manifest import Manifest

_FIXTURE_DIR: Path = Path(__file__).parent / "agents_md"

#: Profiles this repo enables in ``.ea/config.yaml``. The byte-cap assertion
#: below measures exactly that set, so the test fails for the same reason the
#: doctor check would fail on the committed AGENTS.md.
_REPO_PROFILE_IDS: tuple[str, ...] = ("core", "python", "research", "agent_driven", "quality")

# The always-on tier-0 set tagged in ``core.yaml``. These are the
# irreversible "no-tooling-backstop" rules: a lapse cannot be caught by
# any lint or gate, so the agent must internalise them. Rules that DO
# have a tooling backstop (e.g. ``planned-scope-revisability`` via the
# PENDING-only guard, ``agent-report-contract`` via the typed report
# boundary) are ``reference``, not tier-0.
_EXPECTED_TIER0_BLOCK_IDS = {
    "non-negotiable-rules",
    "state-vs-specs",
    "worktree-discipline",
    "prep-plan-mode",
    "iter-phase-close-timing",
}

# Rules with an automated backstop that were reconciled off tier-0.
_TOOLING_BACKED_REFERENCE_BLOCK_IDS = {
    "planned-scope-revisability",
    "agent-report-contract",
}

# Reference-placed blocks contributed by the two profiles the reachability
# sweep used to skip (``agent_driven`` and ``quality``). Named explicitly so a
# profile silently dropping out of the composed set fails loudly instead of
# shrinking the sweep back to the core/python/research subset.
_LATE_PROFILE_REFERENCE_BLOCK_IDS = {
    "agent-driven-phase-equals-release",
    "agent-driven-large-phase-pr",
    "agent-driven-cadence-adr-pointer",
    "lean-wave-verification",
    "code-craft-dry",
    "code-craft-fail-fast",
    "code-craft-single-responsibility",
    "code-craft-explicit-over-implicit",
}


#: The committed fixture set. The assertion node and the refresh writer below
#: both read it, so a new combo cannot be asserted without also being writable.
_GOLDEN_COMBOS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("core",), "core_only.md"),
    (("core", "python", "research"), "core_python_research.md"),
)


@pytest.mark.golden
@pytest.mark.parametrize(
    ("profile_combo", "fixture_name"),
    _GOLDEN_COMBOS,
    ids=["core_only", "core_python_research"],
)
def test_render_agents_md_matches_golden(
    profile_combo: tuple[str, ...],
    fixture_name: str,
    tmp_path: Path,
) -> None:
    """Rendered AGENTS.md is byte-identical to the committed fixture."""
    composed = compose([load_profile(p) for p in profile_combo])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))

    actual_bytes = target.read_bytes()
    expected_bytes = (_FIXTURE_DIR / fixture_name).read_bytes()
    assert actual_bytes == expected_bytes, (
        f"AGENTS.md output drifted from golden fixture {fixture_name!r}. "
        "If intentional: uv run eawf snapshot update --kind agents_md"
    )


def test_refresh_agents_md_goldens(tmp_path: Path) -> None:
    """Rewrite the committed AGENTS.md fixtures under a refresh switch.

    This is the node ``eawf snapshot update --kind agents_md`` drives. Without
    a switch it skips, so a normal run never rewrites the tree; with
    ``EAWF_SNAPSHOT_OUT`` the bytes land in a tmp dir so a caller can verify a
    regeneration without touching the committed fixtures.
    """
    if not (os.environ.get("EAWF_REFRESH_GOLDEN") or os.environ.get("EAWF_SNAPSHOT_REGEN")):
        pytest.skip("set EAWF_REFRESH_GOLDEN=1 (or EAWF_SNAPSHOT_REGEN=1) to refresh")
    out_dir = Path(os.environ.get("EAWF_SNAPSHOT_OUT") or _FIXTURE_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, (profile_combo, fixture_name) in enumerate(_GOLDEN_COMBOS):
        # Each combo renders into its own root: render_agents_md sweeps stale
        # docs/rules/ expansions beside the target, so a shared root would let
        # one combo's sweep delete the previous combo's output.
        root = tmp_path / f"combo{index}"
        root.mkdir()
        composed = compose([load_profile(p) for p in profile_combo])
        target = root / "AGENTS.md"
        render_agents_md(composed, target, Manifest(version=1, generated={}))
        (out_dir / fixture_name).write_bytes(target.read_bytes())


@pytest.mark.golden
@pytest.mark.parametrize(
    "profile_combo",
    [
        ("core",),
        ("core", "python", "research"),
    ],
    ids=["core_only", "core_python_research"],
)
def test_render_agents_md_two_renders_byte_stable(
    profile_combo: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Two consecutive renders of the same composed profile produce identical bytes.

    This is stronger than the golden check: it asserts the renderer itself is
    deterministic across calls (no hash-of-now sneaking into the output).
    """
    composed = compose([load_profile(p) for p in profile_combo])
    target = tmp_path / "AGENTS.md"

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    first = target.read_bytes()

    render_agents_md(composed, target, manifest)
    second = target.read_bytes()

    assert first == second


@pytest.mark.golden
@pytest.mark.parametrize(
    "profile_combo",
    [
        ("core",),
        ("core", "python", "research"),
    ],
    ids=["core_only", "core_python_research"],
)
def test_rendered_agents_md_is_eawf014_clean(
    profile_combo: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """The rendered AGENTS.md carries no manually wrapped paragraphs.

    The source profile bodies are authored one line per paragraph
    (P29-I07-W06), so the verbatim render must pass the EAWF014
    no-manual-wrap lint over the whole file. A regression here means a
    profile body re-introduced an intra-paragraph hard wrap.
    """
    composed = compose([load_profile(p) for p in profile_combo])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))

    violations = check_source(target.read_text(encoding="utf-8"), candidate_lines=None)

    assert violations == [], "\n".join(v.render() for v in violations)


def test_core_profile_carries_memory_hygiene_convention() -> None:
    """The memory-hygiene convention is sourced from the core profile (core.yaml).

    The convention (durable facts are remembered; status is queried via
    ``eawf status`` / ``eawf memory digest``, not memorized) lives in the
    profile source so a re-render reproduces it; a future edit that drops the
    block is caught here. The block is reference-tier so it stays off the
    tier-0 AGENTS.md token budget.
    """
    core = load_profile("core")
    block = next((b for b in core.render_blocks if b.id == "memory-hygiene"), None)
    assert block is not None, "core.yaml must declare the memory-hygiene render block"
    assert block.target == "AGENTS.md"
    assert block.tier == "reference"
    assert "eawf status" in block.body_template
    assert "eawf memory digest" in block.body_template
    assert "derivable" in block.body_template


def test_memory_hygiene_lands_in_rendered_reference_file(tmp_path: Path) -> None:
    """The memory-hygiene expansion renders in full and the re-render is stable.

    The block is reference-placed, so its full body lives in
    ``docs/rules/memory-hygiene.md`` while AGENTS.md keeps the one line that
    names the obligation and links the expansion. Both halves are asserted so a
    regression that drops either the pointer or the body is caught.
    """
    composed = compose([load_profile("core")])
    target = tmp_path / "AGENTS.md"
    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    first = target.read_text(encoding="utf-8")
    expansion = reference_file_path(tmp_path, "memory-hygiene")

    assert "docs/rules/memory-hygiene.md" in first
    assert "### Memory hygiene: remember durable facts, query status" not in first
    assert "### Memory hygiene: remember durable facts, query status" in expansion.read_text(
        encoding="utf-8"
    )
    # Idempotent: a second render produces byte-identical output.
    render_agents_md(composed, target, manifest)
    assert target.read_text(encoding="utf-8") == first


def test_every_reference_placed_block_is_reachable_and_complete(tmp_path: Path) -> None:
    """No reference-placed block loses content: it moves, and the root links it.

    This is the invariant that makes the byte-cap split safe. For every block
    the composed profile marks ``placement: reference``, the root must carry the
    block id plus the path to the expansion, and the expansion must contain the
    block's whole body text.

    The sweep composes the repo's full enabled set, not a core/python/research
    subset: the ``agent_driven`` and ``quality`` blocks moved in the same split
    and went unasserted while the profile list was written out by hand here.
    """
    composed = compose([load_profile(p) for p in _REPO_PROFILE_IDS])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))
    root_text = target.read_text(encoding="utf-8")

    moved = [b for b in composed.render_blocks if b.target == "AGENTS.md" and b.is_reference_placed]
    assert moved, "the core profile must declare at least one reference-placed block"
    assert {b.id for b in moved} >= _LATE_PROFILE_REFERENCE_BLOCK_IDS, (
        "the agent_driven + quality reference blocks must be in the swept set"
    )
    for block in moved:
        assert f"`{block.id}`" in root_text
        assert f"docs/rules/{block.id}.md" in root_text
        expansion = reference_file_path(tmp_path, block.id).read_text(encoding="utf-8")
        # A structured block's triad is split by sub-headings in the render, so
        # each authored part is checked on its own rather than as one span.
        authored = (block.body_template, block.rationale, block.mechanism, block.verification)
        for part in authored:
            if part:
                assert part.strip() in expansion, f"{block.id!r} lost content on the move"


def test_rendered_agents_md_fits_the_project_doc_byte_cap(tmp_path: Path) -> None:
    """The repo's own profile set renders inside the consumer's byte cap.

    A consumer silently truncates the project doc at
    :data:`~eawf.observability.doctor.checks.CODEX_PROJECT_DOC_BYTE_CAP` bytes,
    so a render past it loses its guidance tail without any error. The split
    into ``docs/rules/`` exists to hold this line; the assertion fails the
    moment new prose pushes the always-loaded file back over.
    """
    composed = compose([load_profile(p) for p in _REPO_PROFILE_IDS])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))

    report = measure_agents_md_byte_cap(
        target.read_text(encoding="utf-8"),
        cap=CODEX_PROJECT_DOC_BYTE_CAP,
    )

    assert not report.over_cap, (
        f"AGENTS.md renders {report.total_bytes}B against a {report.cap}B cap; "
        f"move an elaborating block to placement=reference to bring it back under"
    )
    assert report.dropped_block_ids == []


def test_core_profile_tags_expected_tier0_blocks() -> None:
    """The always-on tier-0 set is tagged on the core profile blocks.

    Only the load-bearing always-on blocks opt into ``tier0``; every
    other block stays at the ``reference`` default so the AGENTS.md
    budget gate accounts for the always-on layer alone.
    """
    core = load_profile("core")
    tier0_ids = {b.id for b in core.render_blocks if b.tier == "tier0"}

    assert tier0_ids == _EXPECTED_TIER0_BLOCK_IDS
    # Hook-enforced / duplicated reference blocks stay off tier-0, as do
    # the rules whose discipline has an automated backstop.
    reference_ids = {b.id for b in core.render_blocks if b.tier == "reference"}
    assert reference_ids >= {"commit-prefix", "secrets-hygiene", "markdown-no-manual-wrap"}
    assert reference_ids >= _TOOLING_BACKED_REFERENCE_BLOCK_IDS
    # The two reconciled blocks are no longer tier-0.
    assert tier0_ids.isdisjoint(_TOOLING_BACKED_REFERENCE_BLOCK_IDS)
