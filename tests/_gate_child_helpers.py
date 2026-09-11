"""Install a test double inside the child interpreter a close gate runs in.

The close scorer executes every deterministic gate in a CHILD interpreter
pinned to a throwaway sandbox
(:func:`eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process` on
the advisory branch,
:func:`eawf.runtime.daemon.gate_execution.run_gate_out_of_process` on the
durable one). A gate is routinely a whole test suite, and a suite that
exercises eawf's own RPCs would otherwise drive the LIVE runtime directory
and ledger, so the process boundary is load-bearing production behaviour.

It is also opaque to ``monkeypatch``: a double installed in the pytest
process is simply not present in the child, so a close-gate test that
fabricates a gate outcome by patching a probe silently scores the REAL
probe instead. This module carries the double across that boundary the way
CPython already offers -- a ``sitecustomize`` module on the child's
``PYTHONPATH`` -- and keeps the spawn, the sandbox and the check itself
completely real.

The double is installed LAZILY, through a ``sys.meta_path`` finder that
patches the target module the moment the check imports it. Importing the
target at interpreter-startup time instead would drag the eawf package tree
into ``site`` initialisation, which re-enters the very module ``python -m``
is about to execute.

Every call the double receives is appended to a JSON-lines log the parent
reads back, so a test asserts BOTH the fabricated outcome and the arguments
the production path actually handed the gate -- and a double that never ran
is visible as an empty log rather than as a silently-passing test.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

#: Directory name planted under the caller's workdir to hold the startup
#: module. Named for what it is so a leftover tree is self-describing.
_PLUGIN_DIRNAME = "gate-child-double"

#: Body of the generated startup module. The header assigns ``_TARGET`` /
#: ``_ATTRIBUTE`` / ``_RETURNS`` / ``_CALL_LOG`` above it.
_STARTUP_BODY = '''

def _plain(value):
    """Render one argument as JSON-safe data, preserving what a test asserts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _double(*args, **kwargs):
    """Record the call, then answer with the fabricated outcome."""
    record = {
        "args": [_plain(item) for item in args],
        "kwargs": {key: _plain(value) for key, value in kwargs.items()},
    }
    with open(_CALL_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")
    return _RETURNS


class _PatchingLoader(Loader):
    """Delegate to the real loader, then swap the attribute on the result."""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module):
        self._inner.exec_module(module)
        setattr(module, _ATTRIBUTE, _double)


class _PatchingFinder(MetaPathFinder):
    """Return a patching loader for the one target module, nothing else."""

    def __init__(self):
        self._resolving = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname != _TARGET or self._resolving:
            return None
        self._resolving = True
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            self._resolving = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PatchingLoader(spec.loader)
        return spec


sys.meta_path.insert(0, _PatchingFinder())
'''


@dataclass(frozen=True)
class GateChildDouble:
    """Handle on a double installed into the gate child's interpreter.

    Attributes:
        call_log: JSON-lines file the child-side double appends one record
            to per call.
        plugin_dir: Directory prepended to the child's ``PYTHONPATH``.
    """

    call_log: Path
    plugin_dir: Path

    def calls(self) -> list[dict[str, Any]]:
        """Return every call the child-side double recorded, in order.

        Returns:
            One ``{"args": [...], "kwargs": {...}}`` mapping per call, with
            every value rendered as JSON-safe data (a ``Path`` as its string,
            a tuple as a list). Empty when the double never ran -- which is
            what a test asserts against to prove the injection took.
        """
        if not self.call_log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.call_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def install_gate_child_double(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workdir: Path,
    target_module: str,
    attribute: str,
    returns: object,
) -> GateChildDouble:
    """Replace ``target_module.attribute`` inside every gate child of this test.

    Args:
        monkeypatch: Used to prepend the generated plugin directory to
            ``PYTHONPATH``, which the gate runners pass to the child
            verbatim. Undone with the rest of the fixture teardown.
        workdir: Directory the plugin and its call log are planted under --
            normally the test's ``tmp_path``.
        target_module: Fully qualified module the double is installed on
            (e.g. ``eawf.workflow.audit_dsl.kinds.affordance_parity``).
        attribute: Module attribute the double replaces.
        returns: What the double answers with. Must be JSON-serialisable,
            because it crosses the process boundary as data.

    Returns:
        The :class:`GateChildDouble` handle whose ``calls()`` replays what
        the production path handed the gate.

    Raises:
        FileExistsError: A double is already installed under *workdir*. Only
            one ``sitecustomize`` is importable per interpreter, so a second
            install would shadow the first instead of adding to it.
        TypeError: *returns* is not JSON-serialisable.
    """
    plugin_dir = workdir / _PLUGIN_DIRNAME
    startup_path = plugin_dir / "sitecustomize.py"
    if startup_path.exists():
        raise FileExistsError(f"a gate-child double is already installed at {str(startup_path)!r}")
    call_log = plugin_dir / "calls.jsonl"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    header = "\n".join(
        [
            '"""Gate-child startup hook planted by the eawf test suite."""',
            "",
            "import importlib.util",
            "import json",
            "import sys",
            "from importlib.abc import Loader, MetaPathFinder",
            "",
            f"_TARGET = {target_module!r}",
            f"_ATTRIBUTE = {attribute!r}",
            f"_RETURNS = json.loads({json.dumps(returns)!r})",
            f"_CALL_LOG = {str(call_log)!r}",
        ]
    )
    startup_path.write_text(header + _STARTUP_BODY, encoding="utf-8")
    inherited = os.environ.get("PYTHONPATH", "")
    entries = [str(plugin_dir), *(part for part in [inherited] if part)]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(entries))
    return GateChildDouble(call_log=call_log, plugin_dir=plugin_dir)


__all__ = [
    "GateChildDouble",
    "install_gate_child_double",
]
