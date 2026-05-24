"""Concrete ``1.0`` -> ``1.1`` migration step.

The v1.1 schema delta tightens every entity ``title`` to ``max_length=72``
and adds an optional ``description`` (``max_length=500``), and renames two
fields so the breadcrumb-safe ``title`` is the single dominant name:
``Decision.summary`` -> ``Decision.title`` and ``Hypothesis.text`` ->
``Hypothesis.title``. Because v1.1 also tightens ``title`` to
``min_length=1``, an empty / whitespace-only v1.0 ``title`` is replaced
with a placeholder (the row ``id`` when present, else ``"(untitled)"``)
so the migrated row stays model-valid.

The transform operates on the **raw** state dict and never re-validates the
input against the full :class:`eawf.kernel.state.models.State` model — the v1.0
input still carries the dropped ``summary`` / ``text`` keys and over-cap
titles, neither of which the current model accepts. For every entity row
whose ``title`` exceeds 72 chars, the FULL title is copied into
``description`` (no loss) and ``title`` is truncated to <= 72. The
truncation prefers the last natural clause boundary that fits the budget
(``. ``, ``; ``, ``: ``, ``, ``, `` — ``, `` -- ``), falling back to the
last word boundary, then to a hard character cut. Because the v1.1
``description`` is itself capped at 500 chars, a pathological title longer
than that is stored truncated-to-500 in ``description`` so the migrated
state still re-loads; every realistic title (the longest live rows run a
few hundred chars) fits well inside 500 and is preserved with no loss.

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

from eawf.kernel.migrations._base import _register

logger = logging.getLogger(__name__)

#: Maximum length of the breadcrumb-safe ``title`` after migration.
_TITLE_MAX = 72

#: Maximum length of the ``description`` field in the v1.1 model. The full
#: original title is preserved here on truncation; a title longer than this
#: (no live row reaches it) is itself capped so the migrated state stays
#: model-valid rather than bricking on a >500-char ``description``.
_DESCRIPTION_MAX = 500

#: Clause / sentence separators, longest first so a multi-char separator
#: (`` -- ``) is matched before its single-char prefix would be. The
#: truncator breaks ``title`` right before the latest separator that keeps
#: the kept prefix within :data:`_TITLE_MAX`, so the separator itself is
#: dropped and the title ends on a whole clause.
_CLAUSE_SEPARATORS = (" -- ", " — ", ". ", "; ", ": ", ", ")

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


def _last_clause_boundary(title: str) -> int:
    """Return the kept-prefix length of the latest in-budget clause break.

    Scans every separator in :data:`_CLAUSE_SEPARATORS` for its latest
    occurrence whose start index is at or before :data:`_TITLE_MAX`, so the
    text kept before it (``title[:start]``) stays within budget and the
    separator + its trailing space are discarded. Returns ``0`` when no
    separator yields a non-empty in-budget prefix, signalling the caller to
    fall back to a word boundary.
    """
    best = 0
    for separator in _CLAUSE_SEPARATORS:
        start = title.rfind(separator, 0, _TITLE_MAX + 1)
        if start <= 0:
            continue
        if title[:start].rstrip():
            best = max(best, start)
    return best


def _truncate_title(title: str) -> str:
    """Return *title* shortened to <= :data:`_TITLE_MAX` chars.

    Prefers, within the budget, the last clause / sentence boundary so the
    truncated title ends on a whole clause; falls back to the last
    whitespace boundary so it at least ends on a whole word; falls back to a
    hard character cut when neither boundary exists within the cap.
    """
    if len(title) <= _TITLE_MAX:
        return title
    clause = _last_clause_boundary(title)
    if clause:
        return title[:clause].rstrip()
    head = title[:_TITLE_MAX]
    cut = head.rstrip()
    boundary = cut.rfind(" ")
    if boundary > 0:
        return cut[:boundary].rstrip()
    return head.rstrip()


def _empty_title_placeholder(row: dict[str, Any]) -> str:
    """Return a ``min_length=1`` stand-in for an empty/whitespace-only title.

    Prefers the row's own ``id`` (a stable, human-meaningful handle that
    already satisfies the v1.1 ``min_length=1`` floor); falls back to the
    literal ``"(untitled)"`` when the row carries no usable ``id``.
    """
    row_id = row.get("id")
    if isinstance(row_id, str) and row_id.strip():
        return row_id
    return "(untitled)"


def _migrate_title_row(row: dict[str, Any]) -> None:
    """Cap ``row['title']`` to <= 72, preserving the full text in description.

    No-op when the row carries no ``title`` key. An empty / whitespace-only
    ``title`` is replaced with a ``min_length=1`` placeholder
    (:func:`_empty_title_placeholder`) so the migrated row satisfies the
    v1.1 model floor — the lean ``check_post`` invariant reads only
    ``schema_version`` and would not otherwise catch it. An in-cap
    non-empty title is left untouched. When the title is over-cap, the FULL
    original title is copied into ``description`` (only when ``description``
    is not already populated) and ``title`` is truncated in place. The
    copied ``description`` is itself bounded to :data:`_DESCRIPTION_MAX` so
    a title longer than the description cap (no live row reaches it) cannot
    brick model re-load.
    """
    title = row.get("title")
    if not isinstance(title, str):
        return
    if not title.strip():
        row["title"] = _empty_title_placeholder(row)
        return
    if len(title) <= _TITLE_MAX:
        return
    if not row.get("description"):
        row["description"] = title[:_DESCRIPTION_MAX]
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


#: Registered into :data:`eawf.kernel.migrations._base.DEFAULT_REGISTRY` on import.
STEP = _register(MigrationV10ToV11())


__all__ = ["STEP", "MigrationV10ToV11"]
