"""Display-layer helpers for the ``eawf --version`` surface.

This module composes the human-readable version presentation that
:mod:`eawf._version` deliberately leaves out (the bare ``__version__``
string is the only fact stored there). Everything here is **display-only
by construction** -- no consumer outside the version-display surface
reads these helpers, so a wrong answer degrades a banner rather than any
lifecycle decision.

The install-type probe (:func:`is_editable_install`) reports whether the
running ``eawf`` came from a PEP 660 editable install (``pip install -e``
/ ``uv`` source checkout) or a regular wheel. The signal lets the banner
annotate a dev checkout (``0.5.4 (editable)``) without changing any
behaviour.

The version composer (:func:`compose_display_version`) layers a PEP 440
local segment (``+dev.g<shortsha>`` plus ``.dirty`` for a dirty tree)
onto the stored base when running from an editable checkout, so the
banner distinguishes a dev build from the clean wheel version. The wheel
path returns the bare base unchanged.
"""

from __future__ import annotations

import json
import logging
import subprocess
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path

from eawf import __version__

logger = logging.getLogger(__name__)

_DISTRIBUTION_NAME = "eawf"
_DIRECT_URL_RESOURCE = "direct_url.json"
_GIT_MARKER = ".git"
_GIT_TIMEOUT_S = 5.0


def _package_import_root() -> Path:
    """Return the directory the ``eawf`` package was imported from.

    This is the parent of ``eawf/__init__.py`` (i.e. ``.../src/eawf`` in a
    source checkout). The :func:`is_editable_install` ``.git`` fallback
    walks ancestors of this path, so tests monkeypatch this function to
    point the search at a controlled directory.

    Returns:
        The absolute path of the ``eawf`` package directory.
    """
    import eawf

    return Path(eawf.__file__).resolve().parent


def _has_git_ancestor(start: Path) -> bool:
    """Return whether *start* or any ancestor carries a ``.git`` marker.

    A worktree checkout stores ``.git`` as a *file* (a gitdir pointer)
    rather than a directory, so presence is tested with
    :meth:`pathlib.Path.exists` rather than ``is_dir`` to cover both the
    primary-clone and worktree layouts.

    Args:
        start: The directory to begin the upward search from.

    Returns:
        ``True`` when a ``.git`` entry exists at *start* or above it.
    """
    return any((directory / _GIT_MARKER).exists() for directory in (start, *start.parents))


def is_editable_install() -> bool:
    """Return whether the running ``eawf`` is a PEP 660 editable install.

    Resolution order:

    1. Read the installed distribution's ``direct_url.json`` and return
       its ``dir_info.editable`` flag when present (PEP 610 / PEP 660).
       An editable wheel carries ``{"dir_info": {"editable": true}}``; a
       regular wheel either omits the resource or carries an absent /
       falsey flag.
    2. Fall back to ``.git`` presence at (or above) the package import
       root when the distribution is not installed
       (:class:`importlib.metadata.PackageNotFoundError`), ``direct_url.json``
       is absent, the JSON does not parse, or the ``dir_info.editable``
       key is missing.

    Returns:
        ``True`` for an editable install (or an un-packaged source
        checkout with a ``.git`` marker), ``False`` for a non-editable
        wheel with no ``.git``.
    """
    try:
        raw = Distribution.from_name(_DISTRIBUTION_NAME).read_text(_DIRECT_URL_RESOURCE)
    except PackageNotFoundError:
        raw = None

    if raw is not None:
        try:
            direct_url = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(f"is_editable_install dist={_DISTRIBUTION_NAME!r} reason=unparseable")
            direct_url = {}
        dir_info = direct_url.get("dir_info")
        if isinstance(dir_info, dict) and "editable" in dir_info:
            return bool(dir_info["editable"])

    return _has_git_ancestor(_package_import_root())


def _git_output(args: list[str]) -> str | None:
    """Run a read-only ``git`` command and return its stripped stdout.

    The command runs against the package import root so the probe reads
    the checkout the running ``eawf`` was imported from, not the operator
    cwd. Any git failure (non-zero exit, missing executable, timeout) is
    swallowed and reported as ``None`` so the version banner degrades to
    the bare base rather than raising.

    Args:
        args: The ``git`` argument vector excluding the leading ``git``.

    Returns:
        The stripped stdout on success, or ``None`` on any failure.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_package_import_root(),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"_git_output args={args!r} reason={exc!r}")
        return None
    if result.returncode != 0:
        logger.debug(f"_git_output args={args!r} rc={result.returncode}")
        return None
    return result.stdout.strip()


def compose_display_version(base: str = __version__) -> str:
    """Compose the display version, layering a dev local segment when editable.

    On an editable checkout (per :func:`is_editable_install`) the returned
    string appends a PEP 440 local segment ``+dev.g<shortsha>`` -- the
    12-char abbreviated ``HEAD`` -- plus a trailing ``.dirty`` when the
    working tree has uncommitted changes (``git status --porcelain`` is
    non-empty). On the wheel path (non-editable) the bare *base* is
    returned unchanged.

    Every git probe is fail-soft: a missing short SHA (detached / broken
    checkout) returns *base* unchanged, so a degraded git environment
    never breaks the banner.

    Args:
        base: The stored base version string (defaults to ``__version__``).

    Returns:
        The base version, optionally suffixed with the dev local segment.
    """
    if not is_editable_install():
        return base

    short_sha = _git_output(["rev-parse", "--short=12", "HEAD"])
    if not short_sha:
        return base

    local = f"dev.g{short_sha}"
    if _git_output(["status", "--porcelain"]):
        local = f"{local}.dirty"
    return f"{base}+{local}"
