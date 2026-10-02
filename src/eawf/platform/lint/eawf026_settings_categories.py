"""EAWF026 — every configuration catalog section is filed under exactly one settings category.

The Settings route's rail lists the orientation categories and, under each, the catalog
sections it holds. A section the category table does not file is a section the rail cannot
reach: its keys exist and are configurable, and no operator can navigate to them. The
table is therefore held to the catalog by a lint rather than by a document, so a section
added to the catalog fails here until it is placed.

The lint reads the leaf catalog's sections and
:data:`eawf.kernel.projection.settings.SETTINGS_CATEGORIES`, and reds on a section the
table files under no category, a section filed twice, a section filed although the
catalog has none, and a category outside the closed orientation set.

The production call-site is ``eawf hook eawf026-settings-categories``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.projection.settings import SETTINGS_CATEGORIES

RULE_CODE = "EAWF026"

#: The closed set of orientation categories the settings rail may list.
ORIENTATION_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"execution", "identity", "interface", "quality", "system"}
)


def catalog_sections() -> frozenset[str]:
    """Return every section the leaf catalog declares a key under."""
    return frozenset(entry.domain for entry in LEAF_KEY_REGISTRY.values())


def category_assignment_defects(sections: Iterable[str]) -> tuple[str, ...]:
    """Return every way the category table fails to file ``sections`` exactly once.

    Args:
        sections: The catalog's sections.

    Returns:
        One message per category outside :data:`ORIENTATION_CATEGORIES`, per section
        the table files under no category, per section it files twice, and per section
        it files although the catalog has no such section; empty when the assignment is
        total.
    """
    defects = [
        f"category {name!r} is not an orientation category"
        for name in sorted({name for name, _members in SETTINGS_CATEGORIES})
        if name not in ORIENTATION_CATEGORIES
    ]
    filed = [section for _name, members in SETTINGS_CATEGORIES for section in members]
    wanted = set(sections)
    defects += [f"section {s!r} is filed under no category" for s in sorted(wanted - set(filed))]
    defects += [
        f"section {s!r} is filed twice" for s in sorted({s for s in filed if filed.count(s) > 1})
    ]
    defects += [
        f"category table files {s!r}, which the catalog lacks" for s in sorted(set(filed) - wanted)
    ]
    return tuple(defects)
