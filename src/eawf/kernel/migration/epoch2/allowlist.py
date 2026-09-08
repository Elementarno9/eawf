"""The allowed-legacy-symbol allowlist, read by two rules at once.

The operational-surface census asks which epoch-1 symbols may still be
reachable after activation; the backlog obsolescence sweep asks which
epoch-1 entities and verbs the cutover deletes. Both answers come from
this one file, which is the only formulation of the deleted set that
cannot drift: prose in two places drifts, a shared file does not.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)

_COMMENT_PREFIX = "#"
_RENAME_ARROW = "->"

ALLOW_DIRECTIVE = "allow"
RENAME_DIRECTIVE = "rename"
DELETE_DIRECTIVE = "delete"

_DIRECTIVES = frozenset({ALLOW_DIRECTIVE, RENAME_DIRECTIVE, DELETE_DIRECTIVE})


class LegacySymbolAllowlist(StrictMigrationModel):
    """The parsed allowlist.

    Attributes:
        allowed_surfaces: Legacy surfaces that survive activation.
        renames: Epoch-1 term to epoch-2 term. A rename is not a
            deletion, so a row naming the old term still imports.
        deleted_terms: Epoch-1 entities and verbs with no epoch-2
            successor.
    """

    allowed_surfaces: tuple[Annotated[str, Field(min_length=1)], ...]
    renames: tuple[tuple[str, str], ...]
    deleted_terms: tuple[Annotated[str, Field(min_length=1)], ...]

    def renamed_terms(self) -> tuple[str, ...]:
        """Return the epoch-1 side of every rename, lowercased."""
        return tuple(old.lower() for old, _ in self.renames)

    def as_payload(self) -> dict[str, Any]:
        """Return the digestable form of the allowlist."""
        return {
            "allowed_surfaces": list(self.allowed_surfaces),
            "renames": [list(pair) for pair in self.renames],
            "deleted_terms": list(self.deleted_terms),
        }


def parse_legacy_symbol_allowlist(text: str) -> LegacySymbolAllowlist:
    """Parse the allowlist text into its three directive groups.

    Args:
        text: The full file contents.

    Returns:
        The parsed allowlist, with each group in file order.

    Raises:
        ValueError: When a non-comment line carries no ``:`` separator,
            names a directive outside ``allow`` / ``rename`` / ``delete``,
            carries an empty payload, or is a ``rename`` without the
            ``->`` arrow. The file is a contract between two rules, so a
            line nobody can interpret fails at the boundary rather than
            being skipped into a silently narrower deleted set.
    """
    allowed: list[str] = []
    renames: list[tuple[str, str]] = []
    deleted: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIX):
            continue
        directive, separator, payload = line.partition(":")
        directive = directive.strip().lower()
        payload = payload.strip()
        if not separator:
            raise ValueError(f"line {lineno}: expected '<directive>: <payload>', got {raw!r}")
        if directive not in _DIRECTIVES:
            raise ValueError(
                f"line {lineno}: unknown directive {directive!r}; "
                f"expected one of {', '.join(sorted(_DIRECTIVES))}"
            )
        if not payload:
            raise ValueError(f"line {lineno}: directive {directive!r} carries an empty payload")
        if directive == ALLOW_DIRECTIVE:
            allowed.append(payload)
        elif directive == DELETE_DIRECTIVE:
            deleted.append(payload)
        else:
            old, arrow, new = payload.partition(_RENAME_ARROW)
            if not arrow or not old.strip() or not new.strip():
                raise ValueError(
                    f"line {lineno}: rename needs "
                    f"'<epoch-1 term> -> <epoch-2 term>', got {payload!r}"
                )
            renames.append((old.strip(), new.strip()))

    return LegacySymbolAllowlist(
        allowed_surfaces=tuple(allowed),
        renames=tuple(renames),
        deleted_terms=tuple(deleted),
    )


def load_legacy_symbol_allowlist(path: Path) -> LegacySymbolAllowlist:
    """Read and parse the allowlist file at ``path``.

    Args:
        path: Location of the allowlist file.

    Returns:
        The parsed allowlist.

    Raises:
        FileNotFoundError: When ``path`` does not exist. The sweep has no
            defensible fallback: guessing a deleted set is the drift the
            allowlist exists to prevent.
        ValueError: When a line cannot be interpreted.
    """
    return parse_legacy_symbol_allowlist(path.read_text(encoding="utf-8"))
