"""Import guard for the statusline cold path.

Claude Code spawns ``eawf cc statusline`` on every redraw, so anything the
statusline import drags in is paid per redraw. The check runs in a fresh
interpreter because the test process has long since imported the daemon.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_FORBIDDEN = (
    # The run-control models are needed only once a segment has found the
    # session's Run, so the budget segment imports them when it gets there.
    "eawf.kernel.runtime.control",
    "eawf.kernel.runtime.semantic",
    "eawf.runtime.daemon.admission",
    "eawf.runtime.runtimes.claude.plugin_install",
    "eawf.runtime.runtimes.claude.plugin_package",
)


def _loaded_after_import(module: str) -> set[str]:
    """Return the forbidden modules present in ``sys.modules`` after importing *module*."""
    probe = (
        "import sys\n"
        f"import {module}\n"
        f"print('\\n'.join(name for name in {_FORBIDDEN!r} if name in sys.modules))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    return {line for line in proc.stdout.splitlines() if line}


@pytest.mark.parametrize(
    "module",
    ["eawf.runtime.runtimes.claude", "eawf.runtime.runtimes.claude.statusline"],
)
def test_statusline_import_leaves_admission_and_installer_unloaded(module: str) -> None:
    assert _loaded_after_import(module) == set()


def test_guard_detects_a_module_that_does_load_admission() -> None:
    # The installer does import admission; this proves the probe can fail.
    assert "eawf.runtime.daemon.admission" in _loaded_after_import(
        "eawf.runtime.runtimes.claude.plugin_install"
    )


def test_guard_raises_on_a_missing_module() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _loaded_after_import("eawf.runtime.runtimes.claude.no_such_module")


def test_package_init_exports_nothing() -> None:
    import eawf.runtime.runtimes.claude as package

    assert not hasattr(package, "package_plugin")
    assert getattr(package, "__all__", []) == []
