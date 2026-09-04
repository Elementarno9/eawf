"""The review / audit / polish skill bodies name their rule files.

Each of these three skills grades work against repo rules, and each used to
paraphrase the rule inline. A paraphrase drifts from the rule file it
summarises and gives the author nothing to check against, so the finding
becomes an argument about what the reviewer remembers. Naming the rule file
by path makes the standard fetchable: the skill body cites
``docs/rules/<slug>.md`` and the reader opens it.

The gate asserts three things: every one of the three bodies matches the
``docs/rules/`` pattern at least once; every path it names resolves to a
committed file (a dangling citation is worse than none); and the citation
survives into the rendered SKILL.md, not just the registry constant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.surfaces.render.skills.registry import SKILL_REGISTRY
from eawf.surfaces.render.skills.render import SkillSpec, render_skill_md_from_spec

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The skills whose whole job is grading work against a written rule.
RULE_CITING_SKILLS: tuple[str, ...] = ("review", "audit", "polish")

#: Matches a ``docs/rules/<slug>.md`` citation anywhere in a body, with or
#: without surrounding backticks.
_RULE_PATH = re.compile(r"docs/rules/[a-z0-9][a-z0-9-]*\.md")


def _spec(skill_name: str) -> SkillSpec:
    for spec in SKILL_REGISTRY:
        if spec.skill_name == skill_name:
            return spec
    raise KeyError(f"{skill_name} is not in SKILL_REGISTRY")


def _body(skill_name: str) -> str:
    return _spec(skill_name).body


@pytest.mark.parametrize("skill_name", RULE_CITING_SKILLS)
def test_skill_body_matches_the_docs_rules_pattern(skill_name: str) -> None:
    """The body carries at least one ``docs/rules/`` citation."""
    assert "docs/rules/" in _body(skill_name)


@pytest.mark.parametrize("skill_name", RULE_CITING_SKILLS)
def test_skill_body_names_at_least_two_rule_files(skill_name: str) -> None:
    """One citation is an accident; the body names the rules it enforces."""
    assert len(set(_RULE_PATH.findall(_body(skill_name)))) >= 2


@pytest.mark.parametrize("skill_name", RULE_CITING_SKILLS)
def test_every_cited_rule_file_exists(skill_name: str) -> None:
    """Each cited path resolves to a committed rule file."""
    cited = sorted(set(_RULE_PATH.findall(_body(skill_name))))

    missing = [path for path in cited if not (_REPO_ROOT / path).is_file()]

    assert missing == [], f"{skill_name} cites absent rule files: {missing}"


@pytest.mark.parametrize("skill_name", RULE_CITING_SKILLS)
def test_cited_rule_files_reach_the_rendered_skill_markdown(skill_name: str) -> None:
    """The citation survives the render, not just the registry constant."""
    spec = _spec(skill_name)
    rendered = render_skill_md_from_spec(spec)

    assert set(_RULE_PATH.findall(rendered)) == set(_RULE_PATH.findall(spec.body))


def test_unknown_skill_name_raises_key_error() -> None:
    """The body lookup fails loudly for a name that is not registered."""
    with pytest.raises(KeyError, match="not in SKILL_REGISTRY"):
        _body("no-such-skill")


def test_rule_path_pattern_rejects_a_bare_directory_reference() -> None:
    """``docs/rules/`` alone is not a citation; the pattern needs a file."""
    assert _RULE_PATH.findall("see docs/rules/ for the list") == []


def test_rule_path_pattern_matches_a_backticked_citation() -> None:
    """A citation wrapped in backticks still resolves to the bare path."""
    assert _RULE_PATH.findall("`docs/rules/comment-economy.md` applies") == [
        "docs/rules/comment-economy.md"
    ]
