"""The mkdocs nav lists every generated rule expansion.

``mkdocs.yml`` declares an explicit nav, so a page absent from it is
unreachable in a built site even though the file ships. The rule expansions
under ``docs/rules/`` are generated (one per profile render block declaring
``placement: reference``), so the set grows whenever a rule moves out of the
always-loaded ``AGENTS.md`` -- exactly the moment a hand-maintained nav rots.
This test pins the two together in both directions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_MKDOCS_YML: Path = _REPO_ROOT / "mkdocs.yml"
_RULES_DIR: Path = _REPO_ROOT / "docs" / "rules"


def _nav_targets(node: Any) -> list[str]:
    """Return every docs-relative page path reachable from a nav *node*.

    The nav is a nested list of either bare page paths or single-key mappings
    (``{title: page-or-subsection}``), so the walk recurses through both shapes
    and collects the leaf strings.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [target for item in node for target in _nav_targets(item)]
    if isinstance(node, dict):
        return [target for value in node.values() for target in _nav_targets(value)]
    return []


def _load_nav_targets() -> list[str]:
    """Return every page path in ``mkdocs.yml``'s nav, in declaration order."""
    body = yaml.safe_load(_MKDOCS_YML.read_text(encoding="utf-8"))
    return _nav_targets(body["nav"])


def test_every_generated_rule_page_is_in_the_nav() -> None:
    """A rule expansion that ships must be reachable in the built site."""
    on_disk = {f"rules/{path.name}" for path in _RULES_DIR.glob("*.md")}
    assert on_disk, "docs/rules/ must hold the generated rule expansions"
    missing = sorted(on_disk - set(_load_nav_targets()))
    assert missing == [], (
        f"mkdocs.yml nav omits {len(missing)} rule page(s): {missing}. "
        "Add each under the Rules section so it is reachable in the built site."
    )


def test_the_nav_names_no_absent_rule_page() -> None:
    """A nav row pointing at a deleted expansion breaks a strict build."""
    navigated = {target for target in _load_nav_targets() if target.startswith("rules/")}
    stale = sorted(target for target in navigated if not (_REPO_ROOT / "docs" / target).is_file())
    assert stale == [], f"mkdocs.yml nav names {len(stale)} absent rule page(s): {stale}"


def test_the_nav_lists_each_rule_page_once() -> None:
    """A duplicated nav row renders the same page twice in the sidebar."""
    navigated = [target for target in _load_nav_targets() if target.startswith("rules/")]
    duplicates = sorted({target for target in navigated if navigated.count(target) > 1})
    assert duplicates == [], f"mkdocs.yml nav repeats {duplicates}"
