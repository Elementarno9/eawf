"""The ship skill and the commit rules describe batched pushes and a squashed state tail.

Every push restarts the full CI matrix, so the ship skill pushes once per batch
of review fixes and squashes the trailing run of ``state:`` commits before each
push. The squash rewrites commit hashes, so the skill repins wave pins after, and
work that surfaces after the phase-close commit becomes a wave of the next
PLANNED or ACTIVE phase because the commit lint rejects a bare subject while one
exists. The commit-granularity and commit-prefix rules state the same contract,
their ``docs/rules/`` pages match a fresh render, and AGENTS.md stays under the
byte cap a Codex consumer truncates at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.observability.doctor.checks import CODEX_PROJECT_DOC_BYTE_CAP
from eawf.platform.profiles import compose, load_profile
from eawf.platform.profiles.models import RenderBlock
from eawf.surfaces.render.agents_md import reference_file_path, render_agents_md
from eawf.surfaces.render.manifest import Manifest
from eawf.surfaces.render.skills.registry import _SHIP_BODY, SKILL_REGISTRY
from eawf.surfaces.render.skills.render import render_skill_md_from_spec
from eawf.surfaces.render.unwrap import unwrap_markdown_paragraphs

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Profiles this repo enables in ``.ea/config.yaml``; the committed AGENTS.md
#: and ``docs/rules/`` pages are rendered from exactly this set.
_REPO_PROFILE_IDS: tuple[str, ...] = ("core", "python", "research", "agent_driven", "quality")

#: The rule pages this contract touches, plus the one it must leave alone.
_RULE_PAGE_IDS: tuple[str, ...] = ("commit-granularity", "commit-prefix", "lean-wave-verification")

#: The ship rewrite must not move the lean-wave-verification rule: the local
#: full-tree gauntlet the ship skill still runs is defined there.
_LEAN_WAVE_VERIFICATION_VERSION = "1.2"


def _step(body: str, number: int) -> str:
    """Return canonical-algorithm step *number* of an unwrapped skill body.

    Args:
        body: Skill markdown with one line per list item.
        number: The 1-based ordered-list index to fetch.

    Returns:
        The step's full line, marker included.

    Raises:
        KeyError: no line opens with ``<number>. ``, so an assertion against a
            renumbered or deleted step fails loudly instead of passing on "".
    """
    algorithm = body.split("## Canonical algorithm", 1)[1].split("\n## ", 1)[0]
    for line in algorithm.splitlines():
        if line.startswith(f"{number}. "):
            return line
    raise KeyError(f"no step {number} in the canonical algorithm")


def _ship_body() -> str:
    return unwrap_markdown_paragraphs(_SHIP_BODY)


def _rendered_ship_skill() -> str:
    spec = next(spec for spec in SKILL_REGISTRY if spec.skill_name == "ship")
    return render_skill_md_from_spec(spec)


def _core_block(block_id: str) -> RenderBlock:
    return next(b for b in load_profile("core").render_blocks if b.id == block_id)


def _rendered_repo_root(tmp_path: Path) -> Path:
    composed = compose([load_profile(p) for p in _REPO_PROFILE_IDS])
    render_agents_md(composed, tmp_path / "AGENTS.md", Manifest(version=1, generated={}))
    return tmp_path


def test_step_missing_number_raises_key_error() -> None:
    """The step extractor refuses a step the algorithm does not have."""
    with pytest.raises(KeyError, match="no step 99"):
        _step(_ship_body(), 99)


def test_ship_body_squashes_state_tail_before_push() -> None:
    """The push step squashes the trailing state commits first."""
    step = _step(_ship_body(), 4)

    assert "Squash the state tail, then push the long-running feature branch" in step
    assert "Before EVERY push" in step
    assert "trailing run of `state:` commits not yet pushed" in step
    assert "git reset --soft" in step
    assert "Never squash a commit the remote already has" in step


def test_ship_body_repins_wave_pins_after_squash() -> None:
    """A squash is followed by the verify-commits repair."""
    step = _step(_ship_body(), 4)

    assert "eawf wave verify-commits --repair" in step
    assert step.index("rewrites commit hashes") < step.index("eawf wave verify-commits --repair")


def test_ship_body_pushes_once_per_review_batch() -> None:
    """The review pass pushes per batch of fixes, never per fix."""
    step = _step(_ship_body(), 6)

    assert "push once per batch" in step
    assert "never one push per fix" in step
    assert "through step 4" in step
    assert "Implement, re-push" not in _ship_body()


def test_ship_body_routes_post_close_work_to_next_phase() -> None:
    """Work after the phase-close commit becomes a wave of the next phase."""
    step = _step(_ship_body(), 8)

    assert "after the phase-close commit" in step
    assert "next PLANNED or ACTIVE phase" in step
    assert "eawf roadmap revise <next-phase> --add-wave" in step
    assert "`Eawf-Wave` trailer" in step


def test_ship_body_keeps_close_as_the_final_pre_merge_commit() -> None:
    """The close step still precedes the post-close routing step."""
    body = _ship_body()

    assert "Bundle close in the final pre-merge commit" in _step(body, 7)
    assert "keeps this close subject" in _step(body, 7)
    with pytest.raises(KeyError):
        _step(body, 9)


def test_rendered_ship_skill_carries_batch_and_squash_steps() -> None:
    """The emitted SKILL.md carries the same steps as the registry body."""
    rendered = _rendered_ship_skill()

    for number in (4, 6, 8):
        assert _step(rendered, number) == _step(_ship_body(), number)
    assert "The trailing `state:` run is squashed" in rendered


def test_commit_granularity_allows_state_tail_squash_before_push() -> None:
    """commit-granularity lets the state tail collapse into one commit before a push."""
    block = _core_block("commit-granularity")

    assert block.mechanism is not None
    assert "**Squash the state tail before a push.**" in block.mechanism
    assert "trailing run of ``state:`` commits not yet pushed may be squashed into one" in (
        block.mechanism
    )
    assert "eawf wave verify-commits --repair" in block.mechanism
    assert "An add or claim of the wave may ride its commit" in block.mechanism
    assert "golden-only ``test:`` commit" in block.mechanism


def test_commit_prefix_out_of_phase_requires_no_planned_or_active_phase() -> None:
    """commit-prefix accepts a bare subject only while nothing is PLANNED or ACTIVE."""
    body = _core_block("commit-prefix").body_template

    assert "Accepted ONLY while no phase is PLANNED or ACTIVE" in body
    assert "Rejected while any phase is PLANNED or ACTIVE" in body
    assert "Accepted ONLY when ``state.current.phase_id`` is ``None``" not in body
    assert "reserved for the gap after phase close" not in body


def test_commit_prefix_bare_docs_carries_digest_companions() -> None:
    """A bare docs commit may carry the digest refresh only beside an artifact."""
    body = _core_block("commit-prefix").body_template

    assert "may also carry ``.ea/state.json`` and ``.secrets.baseline``" in body
    assert "Without an artifact, those two stay rejected" in body


def test_lean_wave_verification_stays_at_pinned_version() -> None:
    """The ship rewrite leaves lean-wave-verification where it was."""
    block = next(
        b for b in load_profile("agent_driven").render_blocks if b.id == "lean-wave-verification"
    )

    assert block.version == _LEAN_WAVE_VERIFICATION_VERSION


@pytest.mark.parametrize("block_id", _RULE_PAGE_IDS)
def test_rule_page_matches_fresh_render(block_id: str, tmp_path: Path) -> None:
    """Each committed rule page is byte-identical to a fresh render."""
    rendered = reference_file_path(_rendered_repo_root(tmp_path), block_id)
    committed = _REPO_ROOT / "docs" / "rules" / f"{block_id}.md"

    assert committed.read_bytes() == rendered.read_bytes(), (
        f"docs/rules/{block_id}.md drifted from its profile block; re-run eawf sync"
    )


def test_agents_md_matches_fresh_render_under_byte_cap(tmp_path: Path) -> None:
    """The committed AGENTS.md is the current render and fits the Codex cap."""
    rendered = (_rendered_repo_root(tmp_path) / "AGENTS.md").read_bytes()
    committed = (_REPO_ROOT / "AGENTS.md").read_bytes()

    assert committed == rendered, "AGENTS.md drifted from its profiles; re-run eawf sync"
    assert len(committed) < CODEX_PROJECT_DOC_BYTE_CAP
