"""The commit rules describe batched pushes and a squashed state tail.

Every push restarts the full CI matrix, so the trailing run of ``state:``
commits is squashed before each push. The squash rewrites commit hashes, so wave
pins are repinned after, and work that surfaces after the phase-close commit
becomes a wave of the next PLANNED or ACTIVE phase because the commit lint
rejects a bare subject while one exists. The commit-granularity and
commit-prefix rules state that contract, their ``docs/rules/`` pages match a
fresh render, and the AGENTS.md card rendered from ``.ea/rules.yaml`` stays
under the byte cap a Codex consumer truncates at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.observability.doctor.checks import CODEX_PROJECT_DOC_BYTE_CAP
from eawf.platform.profiles import compose, load_profile
from eawf.platform.profiles.models import RenderBlock
from eawf.platform.rules.render import CARD_TARGET, plan_rule_projections
from eawf.surfaces.render.agents_md import reference_file_path, render_agents_md
from eawf.surfaces.render.manifest import Manifest

pytestmark = pytest.mark.usefixtures("isolated_host_homes")

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Profiles this repo enables in ``.ea/config.yaml``; the committed AGENTS.md
#: and ``docs/rules/`` pages are rendered from exactly this set.
_REPO_PROFILE_IDS: tuple[str, ...] = ("core", "python", "research", "agent_driven", "quality")

#: The rule pages this contract touches, plus the one it must leave alone.
_RULE_PAGE_IDS: tuple[str, ...] = ("commit-granularity", "commit-prefix", "lean-wave-verification")

#: The commit-rule rewrite must not move the lean-wave-verification rule: the local
#: full-tree gauntlet a phase ship still runs is defined there.
_LEAN_WAVE_VERIFICATION_VERSION = "1.3"


def _core_block(block_id: str) -> RenderBlock:
    return next(b for b in load_profile("core").render_blocks if b.id == block_id)


def _rendered_repo_root(tmp_path: Path) -> Path:
    composed = compose([load_profile(p) for p in _REPO_PROFILE_IDS])
    render_agents_md(composed, tmp_path / "AGENTS.md", Manifest(version=1, generated={}))
    return tmp_path


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
    """The commit-rule rewrite leaves lean-wave-verification where it was."""
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


def test_agents_md_matches_fresh_render_under_byte_cap() -> None:
    """The committed AGENTS.md is the current rule-graph card and fits the Codex cap."""
    plan = plan_rule_projections(_REPO_ROOT)
    rendered = next(p.text for p in plan.projections if p.record.target == CARD_TARGET)
    committed = (_REPO_ROOT / CARD_TARGET).read_bytes()

    assert committed == rendered.encode("utf-8"), (
        "AGENTS.md drifted from .ea/rules.yaml; re-run eawf sync"
    )
    assert len(committed) < CODEX_PROJECT_DOC_BYTE_CAP
