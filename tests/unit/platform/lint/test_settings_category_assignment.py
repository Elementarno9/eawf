"""EAWF026 holds the settings category table to the configuration catalog.

Requirement row proved here, by id:

- ``LINT-042``: every catalog section is assigned to exactly one of the six orientation
  categories, and the lint reds when a section present in the catalog is unassigned,
  assigned twice, or assigned to a category outside the six.

The fire-proof case is the defect that motivated the rule: the ``agents`` section reached
the catalog before the category table filed it, which left its keys configurable and
unreachable from the rail.
"""

from __future__ import annotations

import pytest

from eawf.kernel.projection.settings import SETTINGS_CATEGORIES
from eawf.platform.lint import eawf026_settings_categories as eawf026
from eawf.platform.lint.eawf026_settings_categories import (
    ORIENTATION_CATEGORIES,
    catalog_sections,
    category_assignment_defects,
)


def _table_without(section: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the shipped table with ``section`` filed nowhere."""
    return tuple(
        (name, tuple(s for s in members if s != section)) for name, members in SETTINGS_CATEGORIES
    )


def test_lint_042_the_shipped_table_files_every_catalog_section_exactly_once() -> None:
    """The lint over the real catalog and table is clean."""
    assert category_assignment_defects(catalog_sections()) == ()


def test_lint_042_the_table_uses_exactly_the_six_orientation_categories() -> None:
    """The six the rail lists are the closed set, each used once."""
    names = [name for name, _members in SETTINGS_CATEGORIES]
    assert sorted(names) == sorted(ORIENTATION_CATEGORIES)
    assert len(ORIENTATION_CATEGORIES) == 6


def test_lint_042_a_section_added_to_the_catalog_unassigned_reds() -> None:
    """The fixture adds a section the table does not file, and the lint names it."""
    sections = catalog_sections() | {"newsection"}
    assert category_assignment_defects(sections) == (
        "section 'newsection' is filed under no category",
    )


def test_lint_042_the_agents_section_left_unplaced_reds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real defect: ``agents`` in the catalog before the table filed it."""
    monkeypatch.setattr(eawf026, "SETTINGS_CATEGORIES", _table_without("agents"))
    assert eawf026.category_assignment_defects(eawf026.catalog_sections()) == (
        "section 'agents' is filed under no category",
    )


def test_lint_042_a_section_assigned_twice_reds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A section under two categories reaches the rail twice and is refused."""
    doubled = (*SETTINGS_CATEGORIES[:-1], ("system", (*SETTINGS_CATEGORIES[-1][1], "agents")))
    monkeypatch.setattr(eawf026, "SETTINGS_CATEGORIES", doubled)
    assert category_assignment_defects(catalog_sections()) == ("section 'agents' is filed twice",)


def test_lint_042_a_category_outside_the_six_reds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seventh heading is a category the rail does not declare."""
    renamed = (("operations", SETTINGS_CATEGORIES[0][1]), *SETTINGS_CATEGORIES[1:])
    monkeypatch.setattr(eawf026, "SETTINGS_CATEGORIES", renamed)
    assert category_assignment_defects(catalog_sections()) == (
        "category 'operations' is not an orientation category",
    )


def test_lint_042_a_section_the_catalog_lacks_reds() -> None:
    """The table filing a section no key lives under is a phantom rail entry."""
    assert category_assignment_defects(catalog_sections() - {"vcs"}) == (
        "category table files 'vcs', which the catalog lacks",
    )


def test_lint_042_an_empty_catalog_reports_every_filed_section_as_phantom() -> None:
    """The empty boundary: with no catalog sections, every filed section is phantom."""
    defects = category_assignment_defects(())
    filed = {s for _name, members in SETTINGS_CATEGORIES for s in members}
    assert len(defects) == len(filed)
    assert all(d.endswith("which the catalog lacks") for d in defects)


def test_lint_042_a_single_section_catalog_reports_the_rest_as_phantom() -> None:
    """The single boundary: one real section passes and every other filed one is phantom."""
    defects = category_assignment_defects({"vcs"})
    assert "category table files 'vcs', which the catalog lacks" not in defects
    assert "section 'vcs' is filed under no category" not in defects
