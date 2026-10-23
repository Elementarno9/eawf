"""The release-process rule text names the Milestone release path only.

The core profile renders this rule into every managed repo. The release
cadence setting is retired, ``eawf release pipeline`` takes ``--milestone``
as its only subject and the phase release workflow is deleted, so a rule that
still names a cadence, ``--phase`` or that workflow sends each reader to a
setting nothing reads or a command that refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_PROFILE = _REPO_ROOT / "src" / "eawf" / "platform" / "profiles" / "data" / "core.yaml"
_RENDERED_RELEASE_RULE = _REPO_ROOT / "docs" / "rules" / "release-process.md"
_RENDERED_SHIP_RULE = _REPO_ROOT / "docs" / "rules" / "ship-process.md"

#: Spellings of the retired release path, each with the problem it reports.
_RETIRED_RELEASE_TEXT: tuple[tuple[str, str], ...] = (
    ("vcs.conventions.release.cadence", "names the retired release cadence setting"),
    ("per-phase", "names the per-phase cadence"),
    ("per_phase", "names the per-phase cadence"),
    ("--phase", "names the retired --phase subject"),
    ("phase-release.yaml", "names the deleted phase release workflow"),
)

#: The pre-correction rule text, trimmed to the lines the check reads.
_OLD_RULE_TEXT = (
    "Releases are opt-in per repo via ``vcs.conventions.release.cadence``. "
    "The two supported cadences:\n"
    "\n"
    "- ``manual`` — the default for every repo.\n"
    "- ``per-phase`` — opt-in; each phase PR closes with a release-readiness "
    "pre-flight gate.\n"
    "\n"
    "Post-merge, ``.github/workflows/phase-release.yaml`` reads the annotation. A repo "
    "that releases through an eawf release train runs "
    "``eawf release pipeline <version> --phase P<NN> --publish``.\n"
)


def _core_rule_bodies() -> dict[str, str]:
    profile = yaml.safe_load(_CORE_PROFILE.read_text(encoding="utf-8"))
    return {
        block["id"]: block["body_template"]
        for block in profile["render_blocks"]
        if "body_template" in block
    }


def release_rule_mismatches(body: str) -> list[str]:
    """Return every way a release rule *body* strays from the Milestone release path.

    Args:
        body: The rule body, as authored in the profile or as rendered.

    Returns:
        One message per retired spelling present and per native verb
        missing, deduplicated in that order; empty when the body agrees.
    """
    problems = [problem for text, problem in _RETIRED_RELEASE_TEXT if text in body]
    if "``eawf release preflight <version>``" not in body:
        problems.append("does not name the eawf release preflight sweep")
    if "--milestone <MLS-NNNN>" not in body:
        problems.append("does not name the --milestone subject")
    return list(dict.fromkeys(problems))


def test_release_rule_text_names_the_milestone_release_path() -> None:
    bodies = _core_rule_bodies()
    assert release_rule_mismatches(bodies["release-process"]) == []
    rendered_release = _RENDERED_RELEASE_RULE.read_text(encoding="utf-8")
    assert release_rule_mismatches(rendered_release) == [], "re-render docs/rules"
    for rendered in (bodies["ship-process"], _RENDERED_SHIP_RULE.read_text(encoding="utf-8")):
        assert [text for text, _ in _RETIRED_RELEASE_TEXT if text in rendered] == []
        assert "--milestone <MLS-NNNN>" in rendered


def test_release_rule_check_reds_on_the_phase_rule_text() -> None:
    assert release_rule_mismatches(_OLD_RULE_TEXT) == [
        "names the retired release cadence setting",
        "names the per-phase cadence",
        "names the retired --phase subject",
        "names the deleted phase release workflow",
        "does not name the eawf release preflight sweep",
        "does not name the --milestone subject",
    ]


def test_release_rule_check_passes_an_empty_body_only_with_findings() -> None:
    assert release_rule_mismatches("") == [
        "does not name the eawf release preflight sweep",
        "does not name the --milestone subject",
    ]
