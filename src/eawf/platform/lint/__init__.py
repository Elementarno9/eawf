"""Custom static-analysis rules for the eawf codebase.

Rules in this package are plain Python checks (not registered ruff
plugins — the ``[tool.ruff.lint] select`` list carries no ``EAWF``
prefix). Each rule module exposes a small, importable, unit-testable
surface. The rules are *registered* through the ``[tool.eawf.lint]``
table in ``pyproject.toml``: ``enabled`` is the rule allow-list and each
rule's tunables (e.g. EAWF010's ``max-loc`` and ``exclude``) live in a
sub-table. :func:`load_lint_config` reads that table so a pre-commit
dispatcher and the rule modules share one source of truth.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Default per-module line budget, mirrored from ``eawf.platform.lint.eawf010`` so
# a missing ``[tool.eawf.lint.eawf010] max-loc`` key still yields the
# canonical cap without importing the rule module at config-load time.
DEFAULT_MAX_LOC = 700


@dataclass(frozen=True)
class Eawf010Config:
    """Resolved EAWF010 (module-length cap) configuration.

    Attributes:
        max_loc: Per-module physical line budget.
        exclude: Repo-relative module paths exempt from the cap (e.g.
            pre-existing oversized files awaiting a split).
    """

    max_loc: int = DEFAULT_MAX_LOC
    exclude: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class LintConfig:
    """Resolved ``[tool.eawf.lint]`` configuration.

    Attributes:
        enabled: Rule codes the dispatcher should run, in declared order.
        eawf010: Resolved EAWF010 sub-config.
    """

    enabled: tuple[str, ...]
    eawf010: Eawf010Config


def load_lint_config(pyproject_path: Path) -> LintConfig:
    """Load ``[tool.eawf.lint]`` from a ``pyproject.toml`` file.

    Args:
        pyproject_path: Path to the ``pyproject.toml`` to read.

    Returns:
        A :class:`LintConfig`. A pyproject without a ``[tool.eawf.lint]``
        table yields an empty ``enabled`` tuple and a default-cap
        EAWF010 sub-config (no exclusions), so callers degrade cleanly
        rather than raising.

    Raises:
        FileNotFoundError: if ``pyproject_path`` does not exist.
        tomllib.TOMLDecodeError: if the file is not valid TOML.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("eawf", {}).get("lint", {})
    enabled = tuple(table.get("enabled", []))
    raw_010 = table.get("eawf010", {})
    eawf010 = Eawf010Config(
        max_loc=int(raw_010.get("max-loc", DEFAULT_MAX_LOC)),
        exclude=frozenset(raw_010.get("exclude", [])),
    )
    return LintConfig(enabled=enabled, eawf010=eawf010)
