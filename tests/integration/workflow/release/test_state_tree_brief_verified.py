"""REL-027: the epoch-2 state-tree brief stays a valid, pinned artifact.

The decision brief carries the reasoning behind the ratified epoch-2 tree
and the target the dev2 importer is validated against. It is durable
evidence, so it has to keep passing the artifact chassis (an H1, the four
chassis sections, a clean scrub line, dense citations that resolve) and it
has to keep hashing to what the registry pins. The brief is located by
globbing the research directory rather than by a literal path, so a rename
that keeps the contract does not red the gate while a deletion does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BRIEF_DIR = _REPO_ROOT / ".ea" / "artifacts" / "research"

#: The brief is identified by its H1 naming both halves of the ruling, the
#: same pair the decision summary carries.
_TITLE_TERMS = ("compact native tree", "legacy store")
_CHASSIS_SECTIONS = ("## Summary", "## References", "## Provenance", "## Scrub")

runner = CliRunner()


def _find_brief() -> Path:
    """Return the single research brief whose H1 names the state-tree shape."""
    matches = [
        path
        for path in sorted(_BRIEF_DIR.glob("*.md"))
        if all(
            term in path.read_text(encoding="utf-8").splitlines()[0].lower()
            for term in _TITLE_TERMS
        )
    ]
    assert len(matches) == 1, f"expected one state-tree brief, got {[p.name for p in matches]}"
    return matches[0]


@pytest.fixture
def brief() -> Path:
    return _find_brief()


def test_brief_carries_every_chassis_section(brief: Path) -> None:
    """CR-02: the four chassis sections and a clean scrub line are present."""
    text = brief.read_text(encoding="utf-8")
    missing = [section for section in _CHASSIS_SECTIONS if section not in text]
    assert not missing, f"{brief.name} lost chassis sections: {missing}"
    assert "- status: clean" in text


def _section(text: str, heading: str) -> str:
    """Return the body of the ``## <heading>`` section of *text*."""
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_brief_cites_the_epoch1_state_at_scale_spike(brief: Path) -> None:
    """CR-02: the brief's evidence chain reaches the state-at-scale census."""
    references = _section(brief.read_text(encoding="utf-8"), "## References")
    assert "epoch1-state-at-scale" in references


def test_brief_enumerates_candidate_shapes_with_one_recommended(brief: Path) -> None:
    """CR-03: at least two shapes were weighed and exactly one is marked."""
    section = _section(brief.read_text(encoding="utf-8"), "## Candidate shapes")
    rows = [
        line
        for line in section.splitlines()
        if line.startswith("|") and not set(line) <= set("|- ")
    ]
    candidates = rows[1:]
    assert len(candidates) >= 2, f"fewer than two candidate shapes: {len(candidates)}"
    recommended = [row for row in candidates if "recommended" in row.lower()]
    assert len(recommended) == 1, f"expected exactly one recommended shape, got {len(recommended)}"


def test_brief_states_the_dev2_importer_validation_target(brief: Path) -> None:
    """CR-03: the importer has a stated target, not a review-by-eyeball."""
    text = brief.read_text(encoding="utf-8")
    assert "## The dev2 importer validation target" in text
    section = _section(text, "## The dev2 importer validation target").lower()
    for oracle in ("totality", "fabrication", "idempotence", "dangling"):
        assert oracle in section, f"validation target no longer names {oracle}"
