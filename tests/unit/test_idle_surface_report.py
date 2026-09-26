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
_SOURCE_ROOT = _REPO_ROOT / "src" / "eawf"

#: Trees that hold production callers but ship no surface of their own. A
#: ``tools/`` script is a real consumer, so the measurement counts its call
#: sites; without it a wired function reads as idle.
_CALLER_ROOTS = [_REPO_ROOT / "tools"]

#: Measured idle count for ``src/eawf``, counting call sites in ``src/`` and
#: in :data:`_CALLER_ROOTS`. Pinned to the exact measurement, with no
#: headroom, so any newly shipped uncalled function reds this test rather than
#: being absorbed by slack. Lower it as surfaces are wired or removed.
#:
#: The previous 525 is not comparable: it was calibrated against the file-level
#: proxy, which reported 600 rows of which 469 had a caller in their own
#: defining file.
#:
#: 204 is not comparable either, for two reasons. It predates counting
#: ``tools/`` call sites, which alone settles five rows (``run_census`` and
#: friends are driven by repo scripts this reporter's source root cannot see).
#: And it went stale: the measurement had already drifted to 233 while the
#: literal stayed at 204, so the ratchet was red rather than holding. The
#: surface behind that drift is UI and epoch-2 native substrate whose producers
#: are still being written; each lands with its own row, and this number comes
#: down with them.
#:
#: What still counts is epoch-2 substrate whose producers have not landed:
#: ``compact_terminal_task`` (``kernel/store/compaction.py``),
#: ``append_correction`` (``kernel/store/ledger.py``) and
#: ``ambiguity_label`` / ``statuses_of``
#: (``kernel/state/epoch2/transitions.py``). The staged importer and the
#: Task-specific compaction sugar are what call them; lower this again as
#: each producer lands. Two rows have left the list: ``apply_transition``
#: (``workflow/lifecycle/epoch2.py``) is called by the native mutation
#: transaction, and ``recover_store_tree``
#: (``kernel/store/compaction.py``) by the daemon's start-up pass over
#: every native tree.
#:
#: 221 dropped to 218: ``assemble_verify_request`` and
#: ``assemble_completion_request`` (``workflow/delivery/request_assembly.py``)
#: were test-only surfaces built for a request shape neither ``/verify``
#: nor the completion path ever adopted, so both were removed rather than
#: wired to a caller that would have had to invent the fields they need.
#: ``route_claim_to_rung`` (``workflow/evidence/rung2.py``) lost its only
#: caller when the idle rung-2-to-rung-3 chain it served was removed; it
#: was removed alongside it, taking its now-unused ``ClaimRung`` and
#: ``looks_numeric`` helpers with it.
IDLE_CEILING = 218


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
    argv = [
        "idle_surface_report.py",
        "--source-root",
        str(tmp_path),
        "--caller-root",
        str(tmp_path),
        "--ceiling",
        "0",
    ]
    assert tool.main(argv) == 1


def test_main_exits_zero_at_the_ceiling(tool: Any, tmp_path: Path) -> None:
    """A count equal to the ceiling passes; only a rise above it fails."""
    (tmp_path / "lonely.py").write_text("def never_called() -> int:\n    return 1\n", "utf-8")
    argv = [
        "idle_surface_report.py",
        "--source-root",
        str(tmp_path),
        "--caller-root",
        str(tmp_path),
        "--ceiling",
        "1",
    ]
    assert tool.main(argv) == 0


def test_main_counts_the_default_caller_root(tool: Any, tmp_path: Path) -> None:
    """With no flag the repo's ``tools/`` tree supplies call sites."""
    source = "def run_census() -> int:\n    return 1\n"
    (tmp_path / "census.py").write_text(source, "utf-8")
    argv = ["idle_surface_report.py", "--source-root", str(tmp_path), "--ceiling", "0"]

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


def test_a_caller_root_reference_settles_a_source_function(tool: Any, tmp_path: Path) -> None:
    """A repo script is a production caller, so its call site counts."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "census.py").write_text("def census() -> int:\n    return 1\n", "utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("from census import census\n\ncensus()\n", "utf-8")

    assert tool.find_idle_functions(source_root, caller_roots=[scripts]) == []
    assert [name for name, _ in tool.find_idle_functions(source_root)] == ["census"]


def test_a_caller_root_definition_is_not_reported(tool: Any, tmp_path: Path) -> None:
    """A script's own helper is not shipped surface, so it is never a row."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "empty.py").write_text("", "utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "helper.py").write_text("def script_only() -> int:\n    return 1\n", "utf-8")

    assert tool.find_idle_functions(source_root, caller_roots=[scripts]) == []


def test_an_empty_caller_root_changes_nothing(tool: Any, tmp_path: Path) -> None:
    """A root with no modules is not an error; it supplies no references."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "lonely.py").write_text("def orphan() -> int:\n    return 1\n", "utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    idle = tool.find_idle_functions(source_root, caller_roots=[scripts])

    assert [name for name, _ in idle] == ["orphan"]


def test_a_missing_caller_root_raises(tool: Any, tmp_path: Path) -> None:
    """A root that scans as empty would report wired functions as idle."""
    source_root = tmp_path / "src"
    source_root.mkdir()

    with pytest.raises(NotADirectoryError, match="caller root is not a directory"):
        tool.find_idle_functions(source_root, caller_roots=[tmp_path / "absent"])


def test_an_unparsable_caller_root_module_raises(tool: Any, tmp_path: Path) -> None:
    """A broken script must fail loudly rather than contribute no references."""
    source_root = tmp_path / "src"
    source_root.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "broken.py").write_text("def oops(\n", "utf-8")

    with pytest.raises(SyntaxError):
        tool.find_idle_functions(source_root, caller_roots=[scripts])


def test_repo_run_census_is_not_idle_once_tools_counts(tool: Any) -> None:
    """``run_census`` is driven by a ``tools/`` script, so it is wired."""
    idle = {name for name, _ in tool.find_idle_functions(_SOURCE_ROOT, caller_roots=_CALLER_ROOTS)}

    assert "run_census" not in idle


def test_flags_the_known_unwired_renderer(tool: Any) -> None:
    """The real defect: ``render_intent_line`` is written but never called.

    It renders a project's problem / desired-outcome pair, which is why the
    rendered AGENTS.md carries no project description at all.
    """
    idle = {name for name, _ in tool.find_idle_functions(_SOURCE_ROOT, caller_roots=_CALLER_ROOTS)}
    assert "render_intent_line" in idle


def test_repo_idle_surface_stays_within_ceiling(tool: Any) -> None:
    """Idle surface does not grow. Lower :data:`IDLE_CEILING` when it shrinks."""
    idle = tool.find_idle_functions(_SOURCE_ROOT, caller_roots=_CALLER_ROOTS)
    assert len(idle) <= IDLE_CEILING, (
        f"{len(idle)} idle public functions exceeds the {IDLE_CEILING} ceiling; "
        "wire the new surface to a caller or drop it"
    )
