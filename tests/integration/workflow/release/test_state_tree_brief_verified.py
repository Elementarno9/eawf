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

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[4]
_BRIEF_DIR = _REPO_ROOT / ".ea" / "artifacts" / "research"
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

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


@pytest.fixture
def staged_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Lay out a throwaway repo root the artifact registry can be written to."""
    ea = tmp_path / ".ea"
    (ea / "store").mkdir(parents=True)
    shutil.copy(_EMPTY_STATE, ea / "state.json")
    monkeypatch.setenv("EA_STATE", str(ea / "state.json"))
    return tmp_path


def _register(staged_repo: Path, brief: Path, *, sha256: str) -> str:
    """Copy *brief* into *staged_repo* and register it; return the relpath."""
    relpath = f".ea/artifacts/research/{brief.name}"
    target = staged_repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(brief, target)
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-state-tree-brief",
            "--kind",
            "research_brief",
            "--uri",
            f"repo:{relpath}",
            "--sha256",
            sha256,
        ],
    )
    assert result.exit_code == 0, result.stdout
    return relpath


def test_brief_passes_the_artifact_chassis(brief: Path) -> None:
    """CR-02: ``eawf artifact validate`` accepts the committed brief body."""
    result = runner.invoke(app, ["--json", "artifact", "validate", str(brief)])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"ok": True, "errors": []}


def test_brief_carries_every_chassis_section(brief: Path) -> None:
    """CR-02: the four chassis sections and a clean scrub line are present."""
    text = brief.read_text(encoding="utf-8")
    missing = [section for section in _CHASSIS_SECTIONS if section not in text]
    assert not missing, f"{brief.name} lost chassis sections: {missing}"
    assert "- status: clean" in text


def test_artifact_verify_matches_the_pinned_digest(staged_repo: Path, brief: Path) -> None:
    """CR-02: ``eawf artifact verify`` recomputes the brief and matches."""
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    _register(staged_repo, brief, sha256=digest)
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-state-tree-brief"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["mismatches"] == 0
    assert payload["missing"] == 0
    assert payload["results"][0]["status"] == "ok"
    assert payload["results"][0]["computed_sha256"] == digest


def test_artifact_verify_reds_on_a_stale_digest(staged_repo: Path, brief: Path) -> None:
    """CR-02 negative control: a drifted body fails the same command.

    Without this, a verify that reported ``ok`` unconditionally would keep
    the positive test green through the exact regression it guards.
    """
    _register(staged_repo, brief, sha256="0" * 64)
    result = runner.invoke(app, ["--json", "artifact", "verify", "ART-state-tree-brief"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["mismatches"] == 1
    assert payload["results"][0]["status"] == "mismatch"


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
