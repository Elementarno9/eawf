"""The agent-writable folders of a repository's machine-local ``.ea/local`` tree.

Agents and skills write local files only under one of the :class:`LocalKind`
folders; ``eawf paths local`` prints a folder's absolute path so a skill page
names the command rather than a path. The ``runtime`` and ``cache`` folders of
the same tree belong to eawf itself, so no agent is handed a root inside them.

The module imports only the standard library because a hook process may load it.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

#: The directory of a repository that holds every eawf file.
EA_DIRNAME: Final = ".ea"

#: The machine-local, never committed directory inside :data:`EA_DIRNAME`.
LOCAL_DIRNAME: Final = "local"

#: The reflection verbs' collection inside the ``reports`` folder.
REFLECT_DIRNAME: Final = "reflect"

#: The mockup skill's folder inside the ``spikes`` folder.
MOCKUPS_DIRNAME: Final = "mockups"

#: The refusal of a kind eawf alone writes.
ONLY_EAWF_WRITES_REFUSAL: Final = "runtime and cache are written by eawf only"

#: The local folders eawf alone writes; an agent asking for one is refused.
_EAWF_ONLY_KINDS: Final = frozenset({"runtime", "cache"})

_SLUG_RE: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: The longest slug a dated file name takes; longer names stop being readable.
SLUG_MAX_LENGTH: Final = 64


class LocalKind(StrEnum):
    """The folders of ``.ea/local`` an agent or a skill may write under.

    Values:
        RESEARCH: briefs, audits, reviews, plans and issues.
        SPIKES: spikes, their ``mockups/`` and ``patches/``.
        RELEASE: release records, pipeline runs, facts and evidence.
        REPORTS: dated reports and the ``reflect/`` collection.
        ARCHIVE: dated folders of moved files with their manifest.
    """

    RESEARCH = "research"
    SPIKES = "spikes"
    RELEASE = "release"
    REPORTS = "reports"
    ARCHIVE = "archive"


class LocalKindError(ValueError):
    """A name is not a :class:`LocalKind` an agent may write under."""


def parse_local_kind(name: str) -> LocalKind:
    """Return the :class:`LocalKind` called *name*.

    Args:
        name: The kind's value, as typed on the command line.

    Returns:
        The kind.

    Raises:
        LocalKindError: *name* is ``runtime`` or ``cache``, with
            :data:`ONLY_EAWF_WRITES_REFUSAL`, or names no kind at all.
    """
    if name in _EAWF_ONLY_KINDS:
        raise LocalKindError(ONLY_EAWF_WRITES_REFUSAL)
    try:
        return LocalKind(name)
    except ValueError:
        known = ", ".join(kind.value for kind in LocalKind)
        raise LocalKindError(f"unknown local kind {name!r}; expected one of {known}") from None


def local_root(tree_root: Path, kind: LocalKind) -> Path:
    """Return the folder of *kind* inside a repository's ``.ea`` directory.

    Args:
        tree_root: The repository's ``.ea`` directory.
        kind: The folder.

    Returns:
        ``<tree_root>/local/<kind>``; nothing is created.
    """
    return tree_root / LOCAL_DIRNAME / kind.value


def dated_local_path(tree_root: Path, kind: LocalKind, slug: str, today: date) -> Path:
    """Return the dated markdown file *slug* names inside the folder of *kind*.

    Args:
        tree_root: The repository's ``.ea`` directory.
        kind: The folder.
        slug: Lower-case words joined by single hyphens, at most
            :data:`SLUG_MAX_LENGTH` characters.
        today: The date the file name opens with.

    Returns:
        ``<local_root>/<YYYY-MM-DD>-<slug>.md``; nothing is created.

    Raises:
        ValueError: *slug* is empty, too long or not lower-case hyphenated words.
    """
    if len(slug) > SLUG_MAX_LENGTH or _SLUG_RE.fullmatch(slug) is None:
        raise ValueError(
            f"slug {slug!r} must be lower-case letters and digits joined by single hyphens,"
            f" at most {SLUG_MAX_LENGTH} characters"
        )
    return local_root(tree_root, kind) / f"{today.isoformat()}-{slug}.md"


def local_scope(kind: LocalKind, *parts: str) -> str:
    """Return the repository-relative write scope of *kind*, or of a folder in it.

    Args:
        kind: The folder.
        parts: Sub-folder names below it, outermost first.

    Returns:
        A POSIX path such as ``.ea/local/spikes/mockups``.
    """
    return PurePosixPath(EA_DIRNAME, LOCAL_DIRNAME, kind.value, *parts).as_posix()


def split_local_scope(scope: str) -> tuple[LocalKind, str]:
    """Return the kind a :func:`local_scope` path lies in and the rest below it.

    Args:
        scope: A repository-relative scope :func:`local_scope` built.

    Returns:
        ``(kind, rest)``; *rest* is ``""`` for the kind's own folder.

    Raises:
        ValueError: *scope* lies outside every :class:`LocalKind` folder.
    """
    parts = PurePosixPath(scope).parts
    if len(parts) < 3 or parts[:2] != (EA_DIRNAME, LOCAL_DIRNAME):
        raise ValueError(f"scope {scope!r} is not inside {EA_DIRNAME}/{LOCAL_DIRNAME}")
    try:
        kind = LocalKind(parts[2])
    except ValueError:
        raise ValueError(f"scope {scope!r} is not inside a local kind folder") from None
    return kind, "/".join(parts[3:])


__all__ = [
    "EA_DIRNAME",
    "LOCAL_DIRNAME",
    "MOCKUPS_DIRNAME",
    "ONLY_EAWF_WRITES_REFUSAL",
    "REFLECT_DIRNAME",
    "SLUG_MAX_LENGTH",
    "LocalKind",
    "LocalKindError",
    "dated_local_path",
    "local_root",
    "local_scope",
    "parse_local_kind",
    "split_local_scope",
]
