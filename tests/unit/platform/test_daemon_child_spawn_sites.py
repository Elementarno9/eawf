"""Sweep gate: every daemon-reachable child spawn suppresses the console window.

Under ``pythonw`` on Windows the daemon has no console of its own, so each
console-subsystem child it forks makes the OS allocate and flash a fresh
console window. :func:`eawf.platform.subprocess_detach.no_window_kwargs`
suppresses that; this sweep is what keeps it wired.

The check is static (AST over each module's source) rather than dynamic,
because three of the eight sites are ``async`` vendor-adapter spawns whose
runtime kwargs are only observable through a live fork. A static sweep also
catches a NEW bare spawn added to one of these modules later, which a
per-site runtime assertion never would.

Unit tier: this file must not ``import subprocess`` (EAWF024), so it names
the spawn callees as dotted strings and never touches the stdlib module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import eawf

_SRC_ROOT = Path(eawf.__file__).resolve().parent.parent
"""Absolute path of the ``src/`` tree holding the installed ``eawf`` package."""

DAEMON_CHILD_SPAWN_SITES: tuple[str, ...] = (
    "eawf/runtime/runtimes/claude/adapter.py",
    "eawf/runtime/runtimes/codex/adapter.py",
    "eawf/runtime/runtimes/opencode/adapter.py",
    "eawf/runtime/runtimes/probes/sdk_baseline.py",
    "eawf/runtime/worktree/git.py",
    "eawf/kernel/spec/writer.py",
    "eawf/runtime/runtimes/claude/statusline_modules/git.py",
    "eawf/kernel/config/layered.py",
)
"""The eight src-relative modules that fork a child on a daemon-reachable path.

Order mirrors the audit: the three vendor adapters, the SDK baseline probe,
worktree git, the spec writer's ``git rm``, the statusline git module, and
layered config's branch detection.
"""

SPAWN_CALLEES: frozenset[str] = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_output",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
    }
)
"""Dotted callees that fork a child process and therefore need the kwargs."""

NO_WINDOW_HELPER = "no_window_kwargs"
"""Name of the sanctioned helper; only ``**no_window_kwargs()`` counts as wired."""


def _dotted_callee(call: ast.Call) -> str | None:
    """Return the dotted name of ``call``'s callee, or ``None`` if not a plain path.

    Handles ``name`` and ``a.b.c`` shapes. A callee computed from a
    subscript or another call (``factory()(x)``) yields ``None`` -- such a
    site cannot be recognised statically and is not a spawn we sweep.
    """
    parts: list[str] = []
    node: ast.expr = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _splats_no_window_kwargs(call: ast.Call) -> bool:
    """Return whether ``call`` splats ``**no_window_kwargs()``.

    A literal ``creationflags=`` kwarg does NOT count: the point of the
    helper is that the platform branch lives in exactly one place.
    """
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        value = keyword.value
        if not isinstance(value, ast.Call):
            continue
        callee = _dotted_callee(value)
        if callee is not None and callee.split(".")[-1] == NO_WINDOW_HELPER:
            return True
    return False


def spawn_calls(source: str) -> list[ast.Call]:
    """Return every child-spawning call in ``source``, in source order.

    Raises:
        SyntaxError: When ``source`` is not parseable Python.
    """
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted_callee(node) in SPAWN_CALLEES
    ]


def bare_spawn_linenos(source: str) -> list[int]:
    """Return the 1-based lines of spawns in ``source`` missing the kwargs."""
    return sorted(call.lineno for call in spawn_calls(source) if not _splats_no_window_kwargs(call))


def assert_no_bare_spawns(source: str, *, label: str) -> None:
    """Raise :class:`AssertionError` naming every bare spawn line in ``source``."""
    bare = bare_spawn_linenos(source)
    assert not bare, (
        f"{label}: child spawn(s) at line(s) {bare} do not splat "
        f"**{NO_WINDOW_HELPER}(); a pythonw daemon will flash a console window"
    )


@pytest.mark.parametrize("relative_path", DAEMON_CHILD_SPAWN_SITES)
def test_daemon_child_spawn_site_suppresses_console_window(relative_path: str) -> None:
    """Every spawn in each of the eight audited modules splats the no-window kwargs."""
    source = (_SRC_ROOT / relative_path).read_text(encoding="utf-8")

    assert spawn_calls(source), f"{relative_path}: sweep list is stale -- no spawn call found"
    assert_no_bare_spawns(source, label=relative_path)


def test_sweep_covers_exactly_the_eight_audited_modules() -> None:
    """The sweep list holds eight distinct, existing modules."""
    assert len(DAEMON_CHILD_SPAWN_SITES) == len(set(DAEMON_CHILD_SPAWN_SITES)) == 8
    missing = [path for path in DAEMON_CHILD_SPAWN_SITES if not (_SRC_ROOT / path).is_file()]
    assert missing == []


def test_bare_spawn_is_detected() -> None:
    """A spawn with no kwargs splat is reported at its own line."""
    source = "import subprocess\nsubprocess.run(['git', 'status'], check=False)\n"

    assert bare_spawn_linenos(source) == [2]


def test_assert_no_bare_spawns_raises_on_a_bare_spawn() -> None:
    """The sweep reds -- with the module label -- when a site spawns bare."""
    source = "import asyncio\nasyncio.create_subprocess_exec('git', 'status')\n"

    with pytest.raises(AssertionError, match=r"fake/module\.py.*\[2\]"):
        assert_no_bare_spawns(source, label="fake/module.py")


def test_wired_spawn_is_not_reported() -> None:
    """A spawn splatting the helper clears the sweep."""
    source = "import subprocess\nsubprocess.run(['git'], **no_window_kwargs())\n"

    assert bare_spawn_linenos(source) == []
    assert_no_bare_spawns(source, label="fake/module.py")


def test_literal_creationflags_kwarg_still_counts_as_bare() -> None:
    """Hand-rolling the flag bypasses the single platform branch, so it fails."""
    source = "import subprocess\nsubprocess.run(['git'], creationflags=0x08000000)\n"

    assert bare_spawn_linenos(source) == [2]


def test_unrelated_splat_still_counts_as_bare() -> None:
    """Splatting some other kwargs helper does not satisfy the sweep."""
    source = "import subprocess\nsubprocess.run(['git'], **detached_subprocess_kwargs())\n"

    assert bare_spawn_linenos(source) == [2]


def test_non_spawn_call_named_run_is_ignored() -> None:
    """A same-named method on an unrelated object is not a spawn."""
    source = "runner.run(['git'])\nself.run()\n"

    assert spawn_calls(source) == []
    assert bare_spawn_linenos(source) == []


def test_empty_source_has_no_spawns() -> None:
    """The empty module is vacuously clean."""
    assert spawn_calls("") == []
    assert bare_spawn_linenos("") == []


def test_dynamically_computed_callee_is_ignored() -> None:
    """A callee that is not a plain dotted path is unrecognisable, not a spawn."""
    source = "spawner()(['git'])\nregistry['run'](['git'])\n"

    assert spawn_calls(source) == []


def test_unparseable_source_raises_syntax_error() -> None:
    """A malformed module surfaces the parse failure rather than passing silently."""
    with pytest.raises(SyntaxError):
        bare_spawn_linenos("def broken(:\n")
