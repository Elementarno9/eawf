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

#: Measured idle count for ``src/eawf`` once the reporter counted call sites
#: instead of files mentioning a name. Pinned to the exact measurement, with no
#: headroom, so any newly shipped uncalled function reds this test rather than
#: being absorbed by slack. Lower it as surfaces are wired or removed.
#:
#: The previous 525 is not comparable: it was calibrated against the file-level
#: proxy, which reported 600 rows of which 469 had a caller in their own
#: defining file.
#:
#: Re-pinned down from 206 by dropping four functions nothing reached:
#: ``mode_key_rows`` (superseded by ``mode_key_rows_active``),
#: ``validate_envelope_path`` (its one would-be caller reads the file
#: itself), and the unused ``validate_or_raise`` / ``now_iso`` helpers.
#:
#: What still counts is epoch-2 substrate whose producers have not landed:
#: ``compact_terminal_task`` / ``recover_store_tree``
#: (``kernel/store/compaction.py``), ``append_correction``
#: (``kernel/store/ledger.py``), ``apply_transition``
#: (``workflow/lifecycle/epoch2.py``), ``ambiguity_label`` /
#: ``render_state_diagram`` (``kernel/state/epoch2/transitions.py``) and
#: ``validation_rule_payload`` (``kernel/migration/epoch2/validation.py``).
#: The staged importer, the recovery leg and the epoch-2 mutators are what
#: call them; lower this again as each producer lands.
#:
#: One row is a measurement artifact rather than idle surface:
#: ``run_census`` is driven by ``tools/ea_commit_census.py``, which this
#: reporter's ``src/eawf`` source root cannot see.
IDLE_CEILING = 204


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


def test_does_not_flag_a_function_called_in_its_own_module(tool: Any, tmp_path: Path) -> None:
    """A helper whose only caller shares its file is not idle.

    The file-level proxy this replaced saw one file mentioning the name and
    reported the helper as shipped-but-uncalled; 469 of its 600 rows were this
    false positive.
    """
    source = "\n".join(
        [
            "def helper() -> int:",
            "    return 1",
            "",
            "def entry() -> int:",
            "    return helper()",
            "",
        ]
    )
    (tmp_path / "module.py").write_text(source, "utf-8")
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["entry"]


def test_does_not_flag_a_qualified_call(tool: Any, tmp_path: Path) -> None:
    """``module.used()`` reaches the definition as surely as a bare call."""
    (tmp_path / "defines.py").write_text("def used() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "calls.py").write_text("import defines\n\ndefines.used()\n", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_does_not_flag_a_function_passed_as_a_callback(tool: Any, tmp_path: Path) -> None:
    """A function handed to a registry is called, just not at the name site."""
    (tmp_path / "defines.py").write_text("def probe() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "wires.py").write_text("from defines import probe\n\nPROBES = [probe]\n", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_flags_a_function_that_is_only_re_exported(tool: Any, tmp_path: Path) -> None:
    """Re-exporting is not calling: a package ``__init__`` hides no dead code."""
    (tmp_path / "defines.py").write_text("def orphan() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "__init__.py").write_text(
        'from defines import orphan\n\n__all__ = ["orphan"]\n', "utf-8"
    )
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["orphan"]


def test_flags_a_function_named_only_in_a_docstring(tool: Any, tmp_path: Path) -> None:
    """A cross-reference in prose is a mention, not a call site."""
    (tmp_path / "defines.py").write_text("def orphan() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "mentions.py").write_text('"""See orphan for the shape."""\n', "utf-8")
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["orphan"]


def test_flags_a_recursive_function_with_no_external_caller(tool: Any, tmp_path: Path) -> None:
    """Recursion is not evidence that anything reaches the function."""
    source = "def walk(n: int) -> int:\n    return 0 if n == 0 else walk(n - 1)\n"
    (tmp_path / "recurse.py").write_text(source, "utf-8")
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["walk"]


def test_does_not_flag_a_recursive_function_with_an_external_caller(
    tool: Any, tmp_path: Path
) -> None:
    """Self-references are subtracted, not the caller's reference too."""
    source = "\n".join(
        [
            "def walk(n: int) -> int:",
            "    return 0 if n == 0 else walk(n - 1)",
            "",
            "def entry() -> int:",
            "    return walk(3)",
            "",
        ]
    )
    (tmp_path / "recurse.py").write_text(source, "utf-8")
    assert [name for name, _ in tool.find_idle_functions(tmp_path)] == ["entry"]


def test_skips_a_name_defined_in_two_files(tool: Any, tmp_path: Path) -> None:
    """An ambiguous name is skipped: no reference is attributable to one def."""
    (tmp_path / "a.py").write_text("def twice() -> int:\n    return 1\n", "utf-8")
    (tmp_path / "b.py").write_text("def twice() -> int:\n    return 2\n", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_empty_source_root_reports_nothing(tool: Any, tmp_path: Path) -> None:
    """A tree with no modules is not an error; it has no idle surface."""
    assert tool.find_idle_functions(tmp_path) == []


def test_empty_module_reports_nothing(tool: Any, tmp_path: Path) -> None:
    """A file with no definitions contributes no row."""
    (tmp_path / "blank.py").write_text("", "utf-8")
    assert tool.find_idle_functions(tmp_path) == []


def test_unparsable_module_raises(tool: Any, tmp_path: Path) -> None:
    """A file the reporter cannot parse fails loudly rather than reading empty."""
    (tmp_path / "broken.py").write_text("def oops(\n", "utf-8")
    with pytest.raises(SyntaxError):
        tool.find_idle_functions(tmp_path)


def test_main_exits_one_when_the_ceiling_is_exceeded(tool: Any, tmp_path: Path) -> None:
    """The ratchet is the exit code, so a rise fails the gate."""
    (tmp_path / "lonely.py").write_text("def never_called() -> int:\n    return 1\n", "utf-8")
    argv = ["idle_surface_report.py", "--source-root", str(tmp_path), "--ceiling", "0"]
    assert tool.main(argv) == 1


def test_main_exits_zero_at_the_ceiling(tool: Any, tmp_path: Path) -> None:
    """A count equal to the ceiling passes; only a rise above it fails."""
    (tmp_path / "lonely.py").write_text("def never_called() -> int:\n    return 1\n", "utf-8")
    argv = ["idle_surface_report.py", "--source-root", str(tmp_path), "--ceiling", "1"]
    assert tool.main(argv) == 0


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
