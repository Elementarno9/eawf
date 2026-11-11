"""The repository's rules and release notes carry the dev-branch cadence.

Milestone pull requests target ``dev``, pre-releases are cut there and a
stable release fast-forwards ``main`` to a reviewed ``dev`` head. The rules
an agent reads are compiled from ``.ea/rules.yaml`` plus the builtin modules,
so the checks read the compiled graph rather than the raw file: a builtin
rule that still told agents to fast-forward ``main`` would contradict the
repository rule even with ``.ea/rules.yaml`` correct.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf import __version__
from eawf.platform.rules.compile import compile_rule_graph
from eawf.platform.rules.loader import load_rule_source
from eawf.platform.rules.modules import select_rule_modules
from eawf.platform.rules.render import builtin_rule_provider

pytestmark = [pytest.mark.unit, pytest.mark.repo_state]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"
_RULES_SOURCE = ".ea/rules.yaml"


def _instructions(home: Path) -> dict[str, tuple[str, str]]:
    """Return each effective rule's instruction and source by obligation."""
    selection = select_rule_modules(load_rule_source(_REPO_ROOT))
    graph = compile_rule_graph(
        _REPO_ROOT, builtin_rules=builtin_rule_provider(selection), home=home
    )
    return {
        rule.record.obligation_id: (rule.record.instruction, rule.record.source.locator)
        for rule in graph.rules
    }


def _section(text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section of a changelog."""
    match = re.search(rf"^## \[{re.escape(version)}\]\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    assert match is not None, f"CHANGELOG.md has no ## [{version}] section"
    return match.group(1)


def test_rules_text(tmp_path: Path) -> None:
    rules = _instructions(tmp_path)

    cadence, cadence_source = rules["release.milestone-cadence"]
    assert cadence_source == _RULES_SOURCE
    assert "pull request targets `dev`" in cadence
    assert "pre-release tags `{version}-dev.{n}` are cut" in cadence
    assert "fast-forwards `main` to a reviewed `dev` head" in cadence

    ship, _ = rules["lifecycle.ship-process"]
    assert "fast-forward the integration branch (main unless repository rules name another)" in ship
    assert "fast-forward main" not in ship

    pr_size, pr_size_source = rules["delivery.milestone-pr-size"]
    assert pr_size_source == _RULES_SOURCE
    assert "fast-forward `dev`" in pr_size

    pointer, pointer_source = rules["delivery.cadence-provenance"]
    assert pointer_source == _RULES_SOURCE
    assert "cite decision D94" in pointer
    assert "superseded D92" in pointer
    assert "superseding D94" in pointer


def test_rules_text_procedures_cite_d94() -> None:
    pointer = (_REPO_ROOT / "docs/rules/agent-driven-cadence-adr-pointer.md").read_text("utf-8")
    assert "Decision D94, which superseded D92" in pointer
    assert "superseding D94 with a new Decision row" in pointer
    ship = (_REPO_ROOT / "docs/rules/ship-process.md").read_text("utf-8")
    assert "under decision D94" in ship
    assert "Fast-forward ``main`` to the reviewed" not in ship


def test_version_and_changelog() -> None:
    # The current version is pinned by the latest release's own test; this one
    # keeps the 0.7.1 section in place below any later release.
    text = _CHANGELOG.read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    assert headings[headings.index("0.7.1") + 1] == "0.7.0"
    assert headings.index(__version__) <= headings.index("0.7.1")
    section = _section(text, "0.7.1")
    batch_outcomes = {
        "hooks": ("exit 126", "about 140 ms", "hook-errors:<n>", "`plugin_version`"),
        "subagent Runs": ("`[eawf task=KEY]`", "stalled-abandoned", "3 rounds"),
        "status line": ("light and a dark palette", "machine-local ledger"),
        "cadence": ("fast lane", "`{version}-dev.{n}`"),
        "reflect": ("offline HTML pages", "--scope project|workspace|user", "incrementally"),
    }
    missing = {
        batch: [phrase for phrase in phrases if phrase not in section]
        for batch, phrases in batch_outcomes.items()
    }
    assert {batch: gaps for batch, gaps in missing.items() if gaps} == {}


def test_version_and_changelog_section_absent_is_refused() -> None:
    with pytest.raises(AssertionError, match=r"no ## \[9\.9\.9\] section"):
        _section("# Changelog\n\n## [0.7.1]\n\n- x\n", "9.9.9")
