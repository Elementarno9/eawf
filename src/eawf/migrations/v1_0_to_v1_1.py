"""Concrete ``1.0`` -> ``1.1`` migration step.

The v1.1 schema delta tightens every entity ``title`` to ``max_length=72``
and adds an optional ``description`` (``max_length=500``), and renames two
fields so the breadcrumb-safe ``title`` is the single dominant name:
``Decision.summary`` -> ``Decision.title`` and ``Hypothesis.text`` ->
``Hypothesis.title``.

The transform operates on the **raw** state dict and never re-validates the
input against the full :class:`eawf.state.models.State` model — the v1.0
input still carries the dropped ``summary`` / ``text`` keys and over-cap
titles, neither of which the current model accepts. For every entity row
whose ``title`` exceeds 72 chars, the FULL title is copied into
``description`` (no loss) and ``title`` is truncated to <= 72 (at the last
word boundary that fits, falling back to a hard cut). A later wave refines
the truncation to a clause boundary and adds the real-state-fixture test;
this edge keeps the truncation correct but not clause-perfect.

The pre/post invariants Pydantic-load against lean fixture models defined
here (:class:`StateV10` / :class:`StateV11`) that read only the
``schema_version`` marker — enough to fail fast on a malformed input
(MIG-F4) or a bad transform (MIG-F5) without coupling to the full state
schema.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from eawf.migrations._base import _register

logger = logging.getLogger(__name__)

#: Maximum length of the breadcrumb-safe ``title`` after migration.
_TITLE_MAX = 72

#: State sub-dicts whose rows carry a bounded ``title`` in v1.1.
_TITLE_BEARING_KEYS = (
    "phases",
    "iters",
    "waves",
    "backlog",
    "incidents",
    "decisions",
    "hypotheses",
)


class StateV10(BaseModel):
    """Lean from-version invariant model — the v1.0 pre-condition.

    Reads only the ``schema_version`` marker; ``extra="ignore"`` lets the
    rest of the real state payload pass through unread so the pre-check
    stays a focused contract, not a full schema clone.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.0"]


class StateV11(BaseModel):
    """Lean to-version invariant model — the v1.1 post-condition."""

    model_config = ConfigDict(extra="ignore")
    schema_version: Literal["1.1"]


def _truncate_title(title: str) -> str:
    """Return *title* shortened to <= :data:`_TITLE_MAX` chars.

    Prefers the last whitespace boundary at or before the cap so the
    truncated title ends on a whole word; falls back to a hard character
    cut when no boundary exists within the first :data:`_TITLE_MAX` chars.
    """
    if len(title) <= _TITLE_MAX:
        return title
    head = title[:_TITLE_MAX]
    cut = head.rstrip()
    boundary = cut.rfind(" ")
    if boundary > 0:
        return cut[:boundary].rstrip()
    return head.rstrip()


def _migrate_title_row(row: dict[str, Any]) -> None:
    """Cap ``row['title']`` to <= 72, preserving the full text in description.

    No-op when the row carries no ``title`` or the title already fits. When
    the title is over-cap, the FULL original title is copied into
    ``description`` (only when ``description`` is not already populated) and
    ``title`` is truncated in place.
    """
    title = row.get("title")
    if not isinstance(title, str) or len(title) <= _TITLE_MAX:
        return
    if not row.get("description"):
        row["description"] = title
    row["title"] = _truncate_title(title)


def _rename_field(row: dict[str, Any], *, old: str, new: str) -> None:
    """Rename ``row[old]`` -> ``row[new]`` in place, carrying the value.

    No-op when *old* is absent. When both keys are present the existing
    *new* value is kept and *old* is dropped (defensive — a v1.0 row never
    carries the v1.1 key, but a re-run must stay idempotent).
    """
    if old not in row:
        return
    value = row.pop(old)
    row.setdefault(new, value)


class MigrationV10ToV11:
    """Migrate a ``state.json`` dict from schema ``1.0`` to ``1.1``."""

    from_version = "1.0"
    to_version = "1.1"

    def apply(self, state_dict: dict[str, Any]) -> dict[str, Any]:
        """Rename + cap title-bearing rows, then bump ``schema_version``.

        The transform renames ``Decision.summary`` -> ``title`` on every
        decision row and ``Hypothesis.text`` -> ``title`` on every
        hypothesis row, copies any over-cap ``title`` into ``description``
        and truncates it to <= 72, and finally rewrites ``schema_version``
        ``1.0`` -> ``1.1``.

        Args:
            state_dict: Raw v1.0 state dict.

        Returns:
            A deep copy at schema ``1.1`` — the input is not mutated.
        """
        migrated = copy.deepcopy(state_dict)

        for row in self._iter_rows(migrated, "decisions"):
            _rename_field(row, old="summary", new="title")
        for row in self._iter_rows(migrated, "hypotheses"):
            _rename_field(row, old="text", new="title")

        for key in _TITLE_BEARING_KEYS:
            for row in self._iter_rows(migrated, key):
                _migrate_title_row(row)

        migrated["schema_version"] = "1.1"
        logger.info(f"apply from={self.from_version} to={self.to_version}")
        return migrated

    @staticmethod
    def _iter_rows(state_dict: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """Return the dict-valued rows under ``state_dict[key]``.

        Returns an empty list when the sub-dict is absent or ``None`` (the
        v1.0 schema makes most entity collections optional). Non-dict rows
        are skipped so a malformed payload cannot crash the iteration.
        """
        section = state_dict.get(key)
        if not isinstance(section, dict):
            return []
        return [row for row in section.values() if isinstance(row, dict)]

    def check_pre(self, state_dict: dict[str, Any]) -> None:
        """Validate the input carries the v1.0 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.0
                payload (MIG-F4).
        """
        StateV10.model_validate(state_dict)

    def check_post(self, state_dict: dict[str, Any]) -> None:
        """Validate the output carries the v1.1 ``schema_version`` marker.

        Raises:
            pydantic.ValidationError: When *state_dict* is not a v1.1
                payload (MIG-F5).
        """
        StateV11.model_validate(state_dict)


#: Registered into :data:`eawf.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV10ToV11())


__all__ = ["STEP", "MigrationV10ToV11"]
