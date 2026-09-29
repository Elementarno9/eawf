"""The flake quarantine: a known-flaky test leaves every blocking lane.

A flaky test in a blocking lane teaches that red is negotiable, and
deleting it loses the regression it guards. Quarantine is the third way:
the test is deselected from every blocking run and runs instead in a
lane that reports how often it failed and never fails the build.

Entry is not free. Each entry names the backlog row that owns the fix and
the date the fix is due, so a quarantine is a scheduled repair rather
than a place a test goes to be forgotten. The registry is a committed
JSON file validated once here; ``tests/conftest.py`` asks this module
which collected tests it holds.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

#: The shape of a native Task key, the id a backlog row carries.
BACKLOG_KEY_PATTERN: Final[str] = r"^[A-Z][A-Z0-9_-]{1,15}-\d{4}$"


class QuarantineEntry(BaseModel):
    """One quarantined test.

    Attributes:
        test_id: The pytest node id; a parametrized node's cases are all
            covered by the id without its ``[...]`` suffix.
        backlog_ref: The backlog row that owns the fix.
        fix_by: The date the fix is due.
        reason: How the test flakes, in one sentence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    test_id: Annotated[str, Field(min_length=1, pattern=r"::")]
    backlog_ref: Annotated[str, Field(pattern=BACKLOG_KEY_PATTERN)]
    fix_by: date
    reason: Annotated[str, Field(min_length=1)]


class QuarantineRegistry(BaseModel):
    """Every quarantined test, one entry each."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[QuarantineEntry, ...] = ()

    @model_validator(mode="after")
    def _one_entry_per_test(self) -> Self:
        """Refuse a test quarantined twice.

        Raises:
            ValueError: Two entries name the same test.
        """
        seen: set[str] = set()
        for entry in self.entries:
            if entry.test_id in seen:
                raise ValueError(f"test {entry.test_id!r} is quarantined twice")
            seen.add(entry.test_id)
        return self

    def holds(self, node_id: str) -> bool:
        """Return whether *node_id* is quarantined.

        Args:
            node_id: A collected item's pytest node id.

        Returns:
            ``True`` when an entry names the node, or names the
            parametrized function the node is one case of.
        """
        base = node_id.split("[", 1)[0]
        return any(entry.test_id in (node_id, base) for entry in self.entries)


def load_quarantine(path: Path) -> QuarantineRegistry:
    """Return the committed registry at *path*, validated.

    Raises:
        FileNotFoundError: *path* does not exist.
        pydantic.ValidationError: An entry lacks its backlog row or its
            fix date, or a test is quarantined twice.
    """
    return QuarantineRegistry.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class LaneReport:
    """What one run of the quarantine lane observed.

    Attributes:
        outcomes: Whether each quarantined test failed, by node id.
    """

    outcomes: Mapping[str, bool]

    @property
    def flake_rate(self) -> float:
        """Return the share of quarantined runs that failed, 0.0 when none ran."""
        return sum(self.outcomes.values()) / len(self.outcomes) if self.outcomes else 0.0

    def render(self) -> list[str]:
        """Return the lane's report lines, failures first."""
        lines = [
            f"quarantine {'FAILED' if failed else 'passed'}: {node_id}"
            for node_id, failed in sorted(self.outcomes.items(), key=lambda row: (not row[1], row))
        ]
        lines.append(
            f"quarantine lane: {sum(self.outcomes.values())} of {len(self.outcomes)} "
            f"quarantined run(s) failed (flake rate {self.flake_rate:.0%}); "
            "the lane reports and never blocks"
        )
        return lines


def partition(node_ids: Iterable[str], registry: QuarantineRegistry) -> tuple[list[str], list[str]]:
    """Split collected node ids into the blocking set and the quarantined set.

    Args:
        node_ids: The collected items' node ids, in collection order.
        registry: The committed quarantine registry.

    Returns:
        ``(blocking, quarantined)``, each in collection order.
    """
    blocking: list[str] = []
    quarantined: list[str] = []
    for node_id in node_ids:
        (quarantined if registry.holds(node_id) else blocking).append(node_id)
    logger.debug(f"partition blocking={len(blocking)} quarantined={len(quarantined)}")
    return blocking, quarantined


__all__ = [
    "BACKLOG_KEY_PATTERN",
    "LaneReport",
    "QuarantineEntry",
    "QuarantineRegistry",
    "load_quarantine",
    "partition",
]
