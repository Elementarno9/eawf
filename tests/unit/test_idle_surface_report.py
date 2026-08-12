"""Tests for ``tools/idle_surface_report.py``.

The ceiling test is a ratchet on the repo's real idle surface: it bounds
growth without demanding the whole backlog be cleared first.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOL_PATH = _REPO_ROOT / "tools" / "idle_surface_report.py"

#: Measured idle count for ``src/eawf`` when the reporter landed. Lower it as
#: surfaces are wired or removed; a rise means new shipped-but-uncalled code.
IDLE_CEILING = 525


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("idle_surface_report", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["idle_surface_report"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool() -> Any:
    return _load_tool()


def test_flags_a_function_with_no_caller(tool: Any, tmp_path: Path) -> None:
    """A public function nothing else names is reported."""
    (tmp_path / "lonely.py").write_text("def never_called() -> int:\n    return 1\n", "utf-8")
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["never_called"]


def test_does_not_flag_a_called_function(tool: Any, tmp_path: Path) -> None:
    """A function named by a sibling module is not idle."""
    (tmp_path / "defines.py").write_text("def used() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "calls.py").write_text("from defines import used\n\nused()\n", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_does_not_flag_private_functions(tool: Any, tmp_path: Path) -> None:
    """Underscore-prefixed helpers are module-internal by convention."""
    (tmp_path / "helper.py").write_text("def _internal() -> int:\n    return 1\n", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_does_not_flag_framework_decorated_handlers(tool: Any, tmp_path: Path) -> None:
    """A Typer handler's call site is the decorator, not our code."""
    source = "\n".join(
        [
            "import typer",
            "",
            "app = typer.Typer()",
            "",
            '@app.command("run")',
            "def run() -> None:",
            "    return None",
            "",
        ]
    )
    (tmp_path / "cli.py").write_text(source, "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_flags_the_known_unwired_renderer(tool: Any) -> None:
    """The real defect: ``render_intent_line`` is written but never called.

    It renders a project's problem / desired-outcome pair, which is why the
    rendered AGENTS.md carries no project description at all.
    """
    idle = {name for name, _ in tool.find_idle_functions(_REPO_ROOT / "src" / "eawf")}
    assert "render_intent_line" in idle


def test_repo_idle_surface_stays_within_ceiling(tool: Any) -> None:
    """Idle surface does not grow. Lower :data:`IDLE_CEILING` when it shrinks."""
    idle = tool.find_idle_functions(_REPO_ROOT / "src" / "eawf")
    assert len(idle) <= IDLE_CEILING, (
        f"{len(idle)} idle public functions exceeds the {IDLE_CEILING} ceiling; "
        "wire the new surface to a caller or drop it"
    )
