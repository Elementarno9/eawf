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
    assert "pull requests target `dev`" in cadence
    assert "`{version}-dev.{n}` pre-release cut on `dev`" in cadence
    assert "fast-forwards `main` to a reviewed `dev` head" in cadence

    ship, _ = rules["lifecycle.ship-process"]
    assert "fast-forward the integration branch (main unless repository rules name another)" in ship
    assert "fast-forward main" not in ship

    pr_size, pr_size_source = rules["delivery.milestone-pr-size"]
    assert pr_size_source == _RULES_SOURCE
    assert "fast-forward `dev`" in pr_size

    pointer, pointer_source = rules["delivery.cadence-provenance"]
    assert pointer_source == _RULES_SOURCE
    assert "cite decision D101" in pointer
    assert "superseded D94" in pointer
    assert "superseding D101" in pointer
    assert "D92" not in pointer

    release, _ = rules["release.milestone-cadence"]
    assert "at least a minor release" in release
    assert "a Milestone of a declared multi-Milestone release" in release


def test_rules_text_procedures_cite_d101() -> None:
    pointer = (_REPO_ROOT / "docs/rules/agent-driven-cadence-adr-pointer.md").read_text("utf-8")
    assert "Decision D101, which superseded D94" in pointer
    assert "superseding D101 with a new Decision row" in pointer
    ship = (_REPO_ROOT / "docs/rules/ship-process.md").read_text("utf-8")
    assert "under decision D101" in ship
    assert "Fast-forward ``main`` to the reviewed" not in ship


#: Obligations whose packaged rule states the seal-commit regime.
_SEAL_RULES = ("vcs.commit-granularity", "vcs.commit-scope-carrier", "lifecycle.close-timing")

#: Phrases of the retired one-state-commit-per-push regime.
_RETIRED_STATE_PHRASES = (
    "one `state:` commit per push",
    "end each push with one",
    "trailing state commit",
    "each push ends with",
    "before a push, the branch is squashed",
    "no commit lands on the branch after it",
)


def _retired_state_phrases(text: str) -> list[str]:
    """Return each retired regime phrase *text* carries, case-insensitively."""
    lowered = text.lower()
    return [phrase for phrase in _RETIRED_STATE_PHRASES if phrase.lower() in lowered]


def test_seal_commit_packaged_rules(tmp_path: Path) -> None:
    rules = _instructions(tmp_path)

    for obligation in _SEAL_RULES:
        text, source = rules[obligation]
        assert not source.startswith(".ea/"), obligation
        assert "`state: seal`" in text, obligation
        assert "once per pull request" in text, obligation
        assert "when the pull request opens" in text, obligation
        assert "code-only" in text, obligation
        assert "the acceptance and release records are the only later `state:` commits" in text, (
            obligation
        )
        assert _retired_state_phrases(text) == [], obligation


def test_seal_commit_rule_revisions() -> None:
    import yaml

    modules = _REPO_ROOT / "src/eawf/platform/rules/data/modules"
    revisions = {
        rule["obligation_id"]: rule["revision"]
        for name in ("eawf.core.vcs.yaml", "eawf.core.lifecycle.yaml")
        for rule in yaml.safe_load((modules / name).read_text("utf-8"))["rules"]
    }
    assert {key: revisions[key] for key in _SEAL_RULES} == {
        "vcs.commit-granularity": 8,
        "vcs.commit-scope-carrier": 6,
        "lifecycle.close-timing": 3,
    }


def test_seal_commit_profile_blocks_and_rendered_text() -> None:
    import yaml

    core = yaml.safe_load(
        (_REPO_ROOT / "src/eawf/platform/profiles/data/core.yaml").read_text("utf-8")
    )
    blocks = {block["id"]: block for block in core["render_blocks"]}
    for block_id in ("commit-prefix", "ship-process", "commit-granularity"):
        block = blocks[block_id]
        text = "\n".join(
            str(block.get(field) or "")
            for field in ("summary", "body_template", "rationale", "mechanism", "verification")
        )
        assert "``state: seal``" in text, block_id
        assert "code-only" in text, block_id

    rendered = [
        _REPO_ROOT / "AGENTS.md",
        _REPO_ROOT / ".ea/rules.yaml",
        *sorted((_REPO_ROOT / "src/eawf/platform/profiles/data").glob("*.yaml")),
        *sorted((_REPO_ROOT / "src/eawf/platform/rules/data/modules").glob("*.yaml")),
        *sorted((_REPO_ROOT / "docs/rules").glob("*.md")),
        _REPO_ROOT / "docs/architecture/workflow.md",
    ]
    found = {
        str(path.relative_to(_REPO_ROOT)): _retired_state_phrases(path.read_text("utf-8"))
        for path in rendered
    }
    assert {path: phrases for path, phrases in found.items() if phrases} == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("Write the Trailing State Commit last.", ["trailing state commit"]),
        ("end each push with one `state:` commit", ["end each push with one"]),
        ("commit state once per pull request as one `state: seal` commit", []),
        ("Each push ends with one `state: <summary>` commit.", ["each push ends with"]),
        (
            "The acceptance is the last state commit; no commit lands on the branch after it.",
            ["no commit lands on the branch after it"],
        ),
    ],
    ids=["empty", "case-folded", "push-tail", "seal", "push-ends", "after-acceptance"],
)
def test_retired_state_phrases(text: str, expected: list[str]) -> None:
    assert _retired_state_phrases(text) == expected


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


def test_the_0_7_2_section_stays_in_place() -> None:
    text = _CHANGELOG.read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    assert headings[headings.index("0.7.2") + 1] == "0.7.1"
    assert headings.index(__version__) <= headings.index("0.7.2")
    dated = re.search(r"^## \[0\.7\.2\][^\n]*\n(.*?)(?=^## \[)", text, re.M | re.S)
    assert dated is not None
    section = dated.group(1)
    areas = (
        "Upgrade notes",
        ".ea layout",
        "Worktrees and state home",
        "Epoch fence",
        "Hooks and skills",
        "Status line",
        "Reflect",
        "CI",
        "Release",
        "Digests and secret scan",
        "Prose",
    )
    assert [area for area in areas if f"### {area}\n" not in section] == []
    phrases = (
        "No state migration is required from 0.7.1",
        "eawf plugin update",
        "eawf doctor hooks",
        "eawf local tidy",
    )
    assert [phrase for phrase in phrases if phrase not in section] == []
