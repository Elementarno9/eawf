"""Gate: every spawn site in ``src/`` carries the win32 no-console kwargs.

A console-subsystem child spawned by a parent that owns no console (the daemon,
or a hook fired by a desktop editor) allocates its own console, which the
operator sees as a terminal window flashing on screen. ``CREATE_NO_WINDOW``
suppresses that, and both shared helpers set it, so the invariant this gate
pins is that a spawn site goes through one of the helpers rather than calling
``subprocess`` bare.

The exemptions are the three sites that must inherit the operator's terminal:
the help pager, and the release tag and push commands whose progress the
operator watches.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC: Path = Path(__file__).resolve().parents[2] / "src" / "eawf"

#: Spawn helpers that set ``CREATE_NO_WINDOW`` on win32, plus the explicit
#: ``creationflags`` a site may pass when it needs its own flag set.
GUARD_MARKERS: tuple[str, ...] = (
    "detached_subprocess_kwargs",
    "no_window_kwargs",
    "creationflags",
)

#: Sites that inherit the operator's console on purpose. A console-less child
#: here would lose the terminal the operator is reading.
INHERITS_CONSOLE: frozenset[tuple[str, str]] = frozenset(
    {
        ("surfaces/cli/commands/help.py", "less"),
        ("surfaces/cli/commands/release_tag.py", "tag_cmd"),
        ("surfaces/cli/commands/release_tag.py", "push_cmd"),
    }
)

_SPAWN_NAMES: frozenset[str] = frozenset({"run", "Popen", "check_output", "call", "check_call"})
_ASYNC_SPAWN_NAMES: frozenset[str] = frozenset(
    {"create_subprocess_exec", "create_subprocess_shell"}
)


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    """Return every ``subprocess`` spawn call in *tree*."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _SPAWN_NAMES and name not in _ASYNC_SPAWN_NAMES:
            continue
        owner = ""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
        if owner == "subprocess" or name == "Popen" or name in _ASYNC_SPAWN_NAMES:
            calls.append(node)
    return calls


def _exempt(rel: str, segment: str) -> bool:
    """Return whether *segment* is one of the console-inheriting sites."""
    return any(path == rel and token in segment for path, token in INHERITS_CONSOLE)


def find_unguarded_spawns(root: Path) -> list[str]:
    """Return ``path:lineno`` for every spawn site without the no-console kwargs."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "subprocess" not in source and "create_subprocess" not in source:
            continue
        rel = path.relative_to(root).as_posix()
        for call in _spawn_calls(ast.parse(source)):
            segment = ast.get_source_segment(source, call) or ""
            if any(marker in segment for marker in GUARD_MARKERS):
                continue
            if _exempt(rel, segment):
                continue
            offenders.append(f"{rel}:{call.lineno}")
    return offenders


def test_every_spawn_site_suppresses_the_console_window() -> None:
    """No ``src/`` spawn site may reach the OS without the no-console kwargs."""
    offenders = find_unguarded_spawns(SRC)
    assert offenders == [], (
        "spawn sites missing detached_subprocess_kwargs() / no_window_kwargs(); "
        f"each flashes a console window when the parent owns none: {offenders}"
    )


def test_gate_reds_on_a_bare_spawn(tmp_path: Path) -> None:
    """The gate fires on the defect it exists to catch."""
    module = tmp_path / "offender.py"
    module.write_text(
        "import subprocess\n\nsubprocess.run(['git', 'status'], capture_output=True)\n",
        encoding="utf-8",
    )

    assert find_unguarded_spawns(tmp_path) == ["offender.py:3"]


def test_gate_passes_a_guarded_spawn(tmp_path: Path) -> None:
    """A site that splats the shared helper is clean."""
    module = tmp_path / "clean.py"
    module.write_text(
        "import subprocess\n\n"
        "from eawf.platform.subprocess_detach import detached_subprocess_kwargs\n\n"
        "subprocess.run(['git', 'status'], **detached_subprocess_kwargs())\n",
        encoding="utf-8",
    )

    assert find_unguarded_spawns(tmp_path) == []
