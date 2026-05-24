"""Structural guard for the P27-I05 layered super-package regroup.

The W14 spike ratified a *codemod-all-refs* regroup (no transitional
shims): every former top-level member package was ``git mv``-d under one
of six layer packages and every import rewritten in place. This test is
the durable invariant the W21 shim-removal wave leaves behind — it fails
the moment a former member package reappears at ``src/eawf/`` top level
(an accidental shim, a stray re-add, or a new package that skips the
layer hierarchy).

The six layers, top-down: ``kernel`` -> ``workflow`` -> ``runtime`` ->
``surfaces`` -> ``observability`` -> ``platform``.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "eawf"

#: The six layer super-packages. Every importable top-level package under
#: ``src/eawf/`` must be one of these.
_LAYERS: frozenset[str] = frozenset(
    {"kernel", "workflow", "runtime", "surfaces", "observability", "platform"}
)

#: Non-package top-level entries that legitimately carry no ``__init__.py``
#: (resource trees, not import surfaces).
_DATA_DIRS: frozenset[str] = frozenset({"_data", "schemas"})

#: Former member packages that the regroup moved under a layer. Their
#: reappearance at top level is the exact regression this guard catches.
_REGROUPED_MEMBERS: frozenset[str] = frozenset(
    {
        # kernel
        "state",
        "store",
        "config",
        "validate",
        "spec",
        "migrations",
        # workflow
        "lifecycle",
        "evidence",
        "skills",
        "agents",
        "agent_report",
        "audit_dsl",
        "dispatch",
        "pr_review",
        "estimation",
        # runtime
        "daemon",
        "runtimes",
        "mcp",
        "sandbox",
        "session",
        "lock",
        "budget",
        "ci_loop",
        "worktree",
        "hooks",
        "vcs",
        # surfaces
        "cli",
        "tui",
        "render",
        # observability
        "telemetry",
        "logging",
        "doctor",
        "bench",
        "eval",
        # platform
        "profiles",
        "registry",
        "install",
        "templates",
        "artifacts",
        "memory",
        "scrub",
        "lint",
        "backup",
        "docs",
    }
)


def _top_level_packages() -> set[str]:
    """Return the names of every top-level package under ``src/eawf/``.

    A directory is a package when it carries an ``__init__.py``;
    ``__pycache__`` and the resource trees are excluded.
    """
    return {
        child.name
        for child in _SRC_ROOT.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def test_top_level_packages_are_exactly_the_six_layers() -> None:
    """``src/eawf/`` exposes the six layer packages and nothing else."""
    assert _top_level_packages() == set(_LAYERS)


@pytest.mark.parametrize("layer", sorted(_LAYERS))
def test_each_layer_is_importable(layer: str) -> None:
    """Every layer super-package imports cleanly via its layered path."""
    module = importlib.import_module(f"eawf.{layer}")
    assert module.__name__ == f"eawf.{layer}"


@pytest.mark.parametrize("member", sorted(_REGROUPED_MEMBERS))
def test_no_regrouped_member_lingers_at_top_level(member: str) -> None:
    """No former member package survives (no shim, no stray re-add) at top level.

    Guards against a regrouped member reappearing directly under
    ``src/eawf/`` — the codemod-all-refs regroup leaves zero shims, so a
    ``src/eawf/<member>/`` directory here means a regression.
    """
    assert not (_SRC_ROOT / member / "__init__.py").is_file()


def test_data_dirs_carry_no_init() -> None:
    """The resource trees stay non-packages (no ``__init__.py``)."""
    for data_dir in _DATA_DIRS:
        path = _SRC_ROOT / data_dir
        if path.is_dir():
            assert not (path / "__init__.py").is_file()
