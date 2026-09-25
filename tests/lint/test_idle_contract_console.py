"""The console-app construction-reachability rule, and proof it reds on a real defect.

``tools/idle_contract_console.py`` discovers every Textual ``App`` subclass
under ``src/eawf/surfaces/tui/`` by AST scan (not a hardcoded class name) and
asserts each has a production construction call reachable from the
``pyproject.toml`` console-script entry point. This is the generalization of
the exact gap that let the console (``ConsoleApp``) ship for a whole phase
with no launcher: a hand-registered, one-off check would have caught only
that one class, not a future screen shaped the same way.

Three groups: the real tree (both ``ConsoleApp`` and ``EaApp`` are wired),
a minimal synthetic fixture tree proving the rule fires on the exact seeded
defect and clears once fixed, and the ``tools/idle_contract_gate.py``
wrapper (``check_console_app_construction_wired``) that turns the finding
into a typed ``GateResult``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.idle_contract_console import (
    TUI_SURFACE_ROOT,
    find_app_subclasses,
    find_unconstructed_console_apps,
)
from tools.idle_contract_gate import GateFailure, check_console_app_construction_wired

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PYPROJECT_WITH_ENTRY = """
[project.scripts]
eawf = "eawf.surfaces.cli.app:main"
""".lstrip()

_PYPROJECT_WITHOUT_ENTRY = """
[project.scripts]
eawfd = "eawf.runtime.daemon.main:main"
""".lstrip()

_CLI_MODULE = """
def main():
    from eawf.surfaces.tui.launch import launch_tui

    return launch_tui()
""".lstrip()

_APP_MODULE = """
from textual.app import App


class ConsoleApp(App[None]):
    pass
""".lstrip()

_LAUNCHER_WITH_CONSTRUCTION = """
from eawf.surfaces.tui.console.app import ConsoleApp


def launch_tui():
    return ConsoleApp()
""".lstrip()

_LAUNCHER_WITHOUT_CONSTRUCTION = """
from eawf.surfaces.tui.console.app import ConsoleApp


def launch_tui():
    return None
""".lstrip()


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_tree(
    tmp_path: Path,
    *,
    pyproject: str = _PYPROJECT_WITH_ENTRY,
    launcher: str | None = _LAUNCHER_WITH_CONSTRUCTION,
) -> Path:
    """Seed a minimal fixture tree with the shape the real console launch chain has.

    ``pyproject.toml`` -> ``eawf.surfaces.cli.app:main`` -> (lazy import)
    ``eawf.surfaces.tui.launch`` -> (lazy import) ``eawf.surfaces.tui.console.app``.
    """
    _write(tmp_path, "pyproject.toml", pyproject)
    _write(tmp_path, "src/eawf/surfaces/cli/app.py", _CLI_MODULE)
    _write(tmp_path, "src/eawf/surfaces/tui/console/app.py", _APP_MODULE)
    if launcher is not None:
        _write(tmp_path, "src/eawf/surfaces/tui/launch.py", launcher)
    return tmp_path


# --------------------------------------------------------------------------- #
# The real tree: every discovered App subclass is wired.
# --------------------------------------------------------------------------- #


def test_real_tree_has_every_app_subclass_constructed() -> None:
    assert find_unconstructed_console_apps() == []


def test_real_tree_discovers_both_console_and_epoch1_apps() -> None:
    apps = find_app_subclasses(_REPO_ROOT / TUI_SURFACE_ROOT)
    assert {"ConsoleApp", "EaApp"} <= set(apps)


# --------------------------------------------------------------------------- #
# Generic discovery: AST-based, not a hardcoded class name.
# --------------------------------------------------------------------------- #


def test_discovers_a_differently_named_app_subclass(tmp_path: Path) -> None:
    """A brand-new screen class is covered the moment it is defined -- no edit needed."""
    _write(
        tmp_path,
        "src/eawf/surfaces/tui/console/future.py",
        "from textual.app import App\n\n\nclass FutureScreenApp(App[None]):\n    pass\n",
    )
    apps = find_app_subclasses(tmp_path)
    assert "FutureScreenApp" in apps


def test_ignores_a_class_that_does_not_subclass_app(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/eawf/surfaces/tui/widgets/helper.py",
        "class Helper:\n    pass\n",
    )
    assert find_app_subclasses(tmp_path) == {}


# --------------------------------------------------------------------------- #
# Gate-fire proof: reds on the seeded defect, passes once the launcher wires it.
# --------------------------------------------------------------------------- #


def test_fixture_tree_passes_when_the_launcher_constructs_the_app(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, launcher=_LAUNCHER_WITH_CONSTRUCTION)
    assert find_unconstructed_console_apps(repo_root=root) == []


def test_fixture_tree_reds_when_the_launcher_construction_is_removed(tmp_path: Path) -> None:
    """The exact W30 defect: ConsoleApp is imported but never constructed."""
    root = _seed_tree(tmp_path, launcher=_LAUNCHER_WITHOUT_CONSTRUCTION)
    assert find_unconstructed_console_apps(repo_root=root) == ["ConsoleApp"]


def test_fixture_tree_reds_when_the_launcher_module_is_missing_entirely(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, launcher=None)
    assert find_unconstructed_console_apps(repo_root=root) == ["ConsoleApp"]


# --------------------------------------------------------------------------- #
# Boundary: no App subclass at all is not a finding.
# --------------------------------------------------------------------------- #


def test_tree_with_no_app_subclass_returns_empty(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", _PYPROJECT_WITH_ENTRY)
    _write(tmp_path, "src/eawf/surfaces/tui/README.md", "no app subclasses here\n")
    assert find_unconstructed_console_apps(repo_root=tmp_path) == []


# --------------------------------------------------------------------------- #
# Error path: the console-script entry point itself is gone.
# --------------------------------------------------------------------------- #


def test_missing_console_script_entry_marks_every_app_unreachable(tmp_path: Path) -> None:
    root = _seed_tree(tmp_path, pyproject=_PYPROJECT_WITHOUT_ENTRY)
    assert find_unconstructed_console_apps(repo_root=root) == ["ConsoleApp"]


def test_missing_pyproject_marks_every_app_unreachable(tmp_path: Path) -> None:
    _write(tmp_path, "src/eawf/surfaces/tui/console/app.py", _APP_MODULE)
    assert find_unconstructed_console_apps(repo_root=tmp_path) == ["ConsoleApp"]


# --------------------------------------------------------------------------- #
# The idle-contract-gate wrapper.
# --------------------------------------------------------------------------- #


def test_gate_wrapper_passes_on_the_real_tree() -> None:
    result = check_console_app_construction_wired()
    assert result.passed is True
    assert result.failure is None


def test_gate_wrapper_reds_when_an_app_has_no_construction_site() -> None:
    stub = SimpleNamespace(find_unconstructed_console_apps=lambda: ["FutureScreenApp"])
    result = check_console_app_construction_wired(module=stub)
    assert result.passed is False
    assert result.failure is GateFailure.CONSOLE_APP_CONSTRUCTION_IDLE
    assert "FutureScreenApp" in result.message


@pytest.mark.parametrize("unconstructed", [[], ["ConsoleApp", "FutureScreenApp"]])
def test_gate_wrapper_matches_the_underlying_finding(unconstructed: list[str]) -> None:
    stub = SimpleNamespace(find_unconstructed_console_apps=lambda: unconstructed)
    result = check_console_app_construction_wired(module=stub)
    assert result.passed is (not unconstructed)
