"""The release-process rule text agrees with the release cadence config.

The core profile renders this rule into every managed repo, so a cadence
spelling the config rejects, a wrong default, or a setting no config model
defines sends each reader to a configuration that fails validation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
import yaml

from eawf.kernel.config.schema import ReleaseCadence, VcsReleaseConventionsConfig

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_PROFILE = _REPO_ROOT / "src" / "eawf" / "platform" / "profiles" / "data" / "core.yaml"
_RENDERED_RELEASE_RULE = _REPO_ROOT / "docs" / "rules" / "release-process.md"
_RENDERED_SHIP_RULE = _REPO_ROOT / "docs" / "rules" / "ship-process.md"

_CADENCE_LIST_LEAD = "supported cadences:"
_CADENCE_BULLET = re.compile(r"^- ``([^`]+)``(.*)$")
_MISSPELLED_CADENCE = "``per_phase``"

#: The pre-correction rule text, trimmed to the lines the check reads.
_OLD_RULE_TEXT = (
    "Releases are opt-in per repo via ``vcs.conventions.release.cadence``. "
    "The two supported cadences:\n"
    "\n"
    "- ``per_phase`` — agent-driven profile default; each phase PR closes with a "
    "release-readiness pre-flight gate and a post-merge auto-tag.\n"
    "- ``manual`` — managed-repo default; releases ride a separate operator-driven "
    "tag flow.\n"
    "\n"
    "Per-phase release pre-flight (gates ``eawf phase close``) requires:\n"
    "\n"
    "- ``__version__`` (``src/<pkg>/_version.py`` or the configured ``version_source``) "
    "advanced from the prior release.\n"
)


def _core_rule_bodies() -> dict[str, str]:
    profile = yaml.safe_load(_CORE_PROFILE.read_text(encoding="utf-8"))
    return {
        block["id"]: block["body_template"]
        for block in profile["render_blocks"]
        if "body_template" in block
    }


def _cadence_bullets(body: str) -> dict[str, str]:
    """Return the cadence bullets under the cadence list lead, by cadence name.

    Only the contiguous bullet run right after the lead counts, so the
    pre-flight checklist further down never reads as a cadence.
    """
    _, _, tail = body.partition(_CADENCE_LIST_LEAD)
    bullets: dict[str, str] = {}
    for line in tail.strip().splitlines():
        match = _CADENCE_BULLET.match(line)
        if match is None:
            break
        bullets[match.group(1)] = match.group(2)
    return bullets


def cadence_rule_mismatches(body: str) -> list[str]:
    """Return every way a release-process rule *body* disagrees with the config.

    Args:
        body: The rule body, as authored in the profile or as rendered.

    Returns:
        One message per disagreement; empty when the body agrees.
    """
    problems: list[str] = []
    bullets = _cadence_bullets(body)
    allowed = set(get_args(ReleaseCadence))
    if set(bullets) != allowed:
        problems.append(f"cadence bullets {sorted(bullets)} != config literals {sorted(allowed)}")
    default = VcsReleaseConventionsConfig().cadence
    if "default" not in bullets.get(default, ""):
        problems.append(f"the {default!r} bullet does not name it the default")
    problems.extend(
        f"the {name!r} bullet claims to be a default"
        for name, rest in bullets.items()
        if name != default and "default" in rest
    )
    if "version_source" in body:
        problems.append("names version_source, which no config model defines")
    if "``release-preflight``" not in body:
        problems.append("does not name the release-preflight check phase close reads")
    return problems


def test_release_rule_text_matches_the_cadence_config() -> None:
    bodies = _core_rule_bodies()
    assert cadence_rule_mismatches(bodies["release-process"]) == []
    rendered_release = _RENDERED_RELEASE_RULE.read_text(encoding="utf-8")
    assert cadence_rule_mismatches(rendered_release) == [], "re-render docs/rules with eawf sync"
    misspelled = sorted(
        block_id for block_id, body in bodies.items() if _MISSPELLED_CADENCE in body
    )
    assert misspelled == [], f"core rule bodies spell the cadence per_phase: {misspelled}"
    for rendered in (_RENDERED_RELEASE_RULE, _RENDERED_SHIP_RULE):
        assert _MISSPELLED_CADENCE not in rendered.read_text(encoding="utf-8"), rendered.name


def test_cadence_check_reds_on_the_old_rule_text() -> None:
    problems = cadence_rule_mismatches(_OLD_RULE_TEXT)
    assert any("per_phase" in problem for problem in problems), problems
    assert any("claims to be a default" in problem for problem in problems), problems
    assert any("version_source" in problem for problem in problems), problems
    assert any("release-preflight" in problem for problem in problems), problems
