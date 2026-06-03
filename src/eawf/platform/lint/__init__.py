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

# Default per-function cognitive-complexity budget, mirrored from
# ``eawf.platform.lint.eawf011`` so a missing
# ``[tool.eawf.lint.eawf011] max-complexity`` key still yields the
# canonical cap without importing the rule module at config-load time.
DEFAULT_MAX_COMPLEXITY = 15

# Default EAWF018 structure-smell caps, mirrored from
# ``eawf.platform.lint.eawf018_structure_smell`` so a missing
# ``[tool.eawf.lint.eawf018]`` sub-table still yields the spike-calibrated
# caps without importing the rule module at config-load time. These caps
# are also the *authority baseline*: a local pyproject may only tighten
# (lower) them, never loosen — :func:`load_lint_config` clamps each
# configured value to ``min(configured, default)``.
DEFAULT_MAX_PROSE_CHARS = 600
DEFAULT_MAX_BULLET_RUN = 12
DEFAULT_MAX_BULLET_CHARS = 500
DEFAULT_MAX_DOCSTRING_PARA_CHARS = 600


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
class Eawf011Config:
    """Resolved EAWF011 (cognitive-complexity gate) configuration.

    Attributes:
        max_complexity: Per-function cognitive-complexity budget.
        exclude: Repo-relative module paths exempt from the gate (e.g.
            pre-existing complex modules awaiting a refactor).
    """

    max_complexity: int = DEFAULT_MAX_COMPLEXITY
    exclude: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Eawf018Config:
    """Resolved EAWF018 (structure-smell advisory) configuration.

    The caps are the spike-calibrated baselines; a local pyproject may
    only tighten them (see :func:`load_lint_config`).

    Attributes:
        max_prose_chars: H1 per-prose-block character cap.
        max_bullet_run: H2 maximum consecutive-bullet-item count.
        max_bullet_chars: H3 per-bullet character cap.
        max_docstring_para_chars: Docstring leading-paragraph character cap.
    """

    max_prose_chars: int = DEFAULT_MAX_PROSE_CHARS
    max_bullet_run: int = DEFAULT_MAX_BULLET_RUN
    max_bullet_chars: int = DEFAULT_MAX_BULLET_CHARS
    max_docstring_para_chars: int = DEFAULT_MAX_DOCSTRING_PARA_CHARS


@dataclass(frozen=True)
class LintConfig:
    """Resolved ``[tool.eawf.lint]`` configuration.

    Attributes:
        enabled: Rule codes the dispatcher should run, in declared order.
        eawf010: Resolved EAWF010 sub-config.
        eawf011: Resolved EAWF011 sub-config.
        eawf018: Resolved EAWF018 sub-config.
    """

    enabled: tuple[str, ...]
    eawf010: Eawf010Config
    eawf011: Eawf011Config
    eawf018: Eawf018Config


def load_lint_config(pyproject_path: Path) -> LintConfig:
    """Load ``[tool.eawf.lint]`` from a ``pyproject.toml`` file.

    Args:
        pyproject_path: Path to the ``pyproject.toml`` to read.

    Returns:
        A :class:`LintConfig`. A pyproject without a ``[tool.eawf.lint]``
        table yields an empty ``enabled`` tuple and a default-cap
        EAWF010 sub-config (no exclusions), so callers degrade cleanly
        rather than raising. EAWF018 caps are clamped to the calibrated
        defaults: a local override below a default tightens, an override
        at or above a default is pinned to the default (tighten-only).

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
    raw_011 = table.get("eawf011", {})
    eawf011 = Eawf011Config(
        max_complexity=int(raw_011.get("max-complexity", DEFAULT_MAX_COMPLEXITY)),
        exclude=frozenset(raw_011.get("exclude", [])),
    )
    raw_018 = table.get("eawf018", {})
    eawf018 = Eawf018Config(
        max_prose_chars=min(
            int(raw_018.get("max-prose-chars", DEFAULT_MAX_PROSE_CHARS)),
            DEFAULT_MAX_PROSE_CHARS,
        ),
        max_bullet_run=min(
            int(raw_018.get("max-bullet-run", DEFAULT_MAX_BULLET_RUN)),
            DEFAULT_MAX_BULLET_RUN,
        ),
        max_bullet_chars=min(
            int(raw_018.get("max-bullet-chars", DEFAULT_MAX_BULLET_CHARS)),
            DEFAULT_MAX_BULLET_CHARS,
        ),
        max_docstring_para_chars=min(
            int(raw_018.get("max-docstring-para-chars", DEFAULT_MAX_DOCSTRING_PARA_CHARS)),
            DEFAULT_MAX_DOCSTRING_PARA_CHARS,
        ),
    )
    return LintConfig(enabled=enabled, eawf010=eawf010, eawf011=eawf011, eawf018=eawf018)
