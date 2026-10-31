"""The shipped rule sources and their rendered targets teach the native model.

The lifecycle is Track, Milestone, Batch, Task and Run. The rule modules and
the core and agent_driven profiles are rendered into a managed repository's
``AGENTS.md`` and ``docs/rules`` pages, so a rule that still says phase, iter
or wave, or routes planning through a retired skill such as ``/prep``, sends
an agent after an entity or a command that no longer exists at the moment it
tries to comply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.profiles import compose, load_profile
from eawf.platform.profiles.selection import resolve_enabled_profiles
from eawf.platform.rules import load_rule_source
from eawf.platform.rules.render import CARD_TARGET, plan_rule_projections
from eawf.surfaces.render.agents_md import render_agents_md
from eawf.surfaces.render.manifest import Manifest
from eawf.workflow.skills.census import retired_nouns

pytestmark = pytest.mark.usefixtures("isolated_host_homes")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM = _REPO_ROOT / "src" / "eawf" / "platform"
_PROFILES = _PLATFORM / "profiles" / "data"
_RULE_SOURCES = (
    *sorted((_PLATFORM / "rules" / "data" / "modules").glob("*.yaml")),
    _PROFILES / "core.yaml",
    _PROFILES / "agent_driven.yaml",
)
_DOCS_RULES = _REPO_ROOT / "docs" / "rules"
#: Planning invocations the native PlanRevision verbs replaced.
_RETIRED_PLANNING = ("/prep", "/roadmap", "roadmap propose")


def epoch1_mentions(path: Path, *, root: Path = _REPO_ROOT) -> list[str]:
    """Return one ``path:line: text`` entry per epoch-1 mention in *path*.

    Args:
        path: A UTF-8 text file under *root*.
        root: The directory the reported path is relative to.

    Returns:
        Every census noun, then every retired planning string, line by line;
        empty when the file teaches the native model only.
    """
    hits: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        found = [*retired_nouns(line), *(text for text in _RETIRED_PLANNING if text in line)]
        hits.extend(f"{path.relative_to(root).as_posix()}:{number}: {text}" for text in found)
    return hits


def test_rule_sources_name_no_epoch1_model() -> None:
    assert len(_RULE_SOURCES) > 2
    assert [hit for source in _RULE_SOURCES for hit in epoch1_mentions(source)] == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", []),
        ("Ship each Milestone through one pull request.", []),
        ("Close the phase, then run /prep.", ["phase", "/prep", "/prep"]),
        (
            "Use /roadmap propose for wave_close.",
            ["/roadmap", "wave_close", "/roadmap", "roadmap propose"],
        ),
        ("Spell P01-I02-W03 ids.", ["P01-I02-W03"]),
    ],
)
def test_epoch1_mentions_reds_on_epoch1_rule_text(
    tmp_path: Path, line: str, expected: list[str]
) -> None:
    source = tmp_path / "rule.yaml"
    source.write_text(f"{line}\n", encoding="utf-8")
    assert epoch1_mentions(source, root=tmp_path) == [f"rule.yaml:1: {text}" for text in expected]


def test_rendered_rule_targets_are_current_and_native(tmp_path: Path) -> None:
    card = dict(plan_rule_projections(_REPO_ROOT).outputs)[CARD_TARGET]
    assert (_REPO_ROOT / CARD_TARGET).read_text(encoding="utf-8") == card, "re-run eawf sync"
    load_rule_source(_REPO_ROOT)
    composed = compose([load_profile(name) for name in resolve_enabled_profiles(_REPO_ROOT)])
    render_agents_md(composed, tmp_path / "AGENTS.md", Manifest(version=1, generated={}))
    fresh = {page.name: page.read_bytes() for page in (tmp_path / "docs" / "rules").glob("*.md")}
    pages = sorted(_DOCS_RULES.glob("*.md"))
    assert [page.name for page in pages] == sorted(fresh)
    assert [page.name for page in pages if page.read_bytes() != fresh[page.name]] == []
    targets = (_REPO_ROOT / CARD_TARGET, _REPO_ROOT / ".ea" / "rules.yaml", *pages)
    assert [hit for target in targets for hit in epoch1_mentions(target)] == []
