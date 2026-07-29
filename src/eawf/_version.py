"""Single source of truth for the package version.

Hatchling reads ``__version__`` from this module via the
``[tool.hatch.version]`` table in ``pyproject.toml`` (``dynamic =
["version"]``), and the package re-exports it from
:mod:`eawf` so ``eawf --version`` and ``importlib.metadata`` agree.

The version string is **PEP-440-compatible** so ``pip install
eawf==0.3.0a1`` resolves; the semver core (``MAJOR.MINOR.PATCH``)
plus an optional PEP-440 pre-release segment (``a`` / ``b`` / ``rc``
+ a number) is the only shape this module carries. The
human-readable build-metadata long form (``0.3.0-alpha.1+phase.PNN``)
is composed by the version-display surface, not stored here.

Bump this value with ``tools/version_bump.py`` rather than editing
by hand — the bumper keeps the semver / PEP-440 grammar consistent
and is the single rewrite path the release pipeline drives.
"""

from __future__ import annotations

__version__ = "0.6.5"
