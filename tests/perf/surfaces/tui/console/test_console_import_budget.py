"""The console opens without the daemon's server side, and paints before it connects.

``eawf`` with no command opens the console, so everything ``eawf.surfaces.tui.console.app``
imports is paid before the first frame. The console names its daemon reads and parses
their answers through :mod:`eawf.runtime.daemon.read_contract`; the handler modules that
serve those reads carry ledgers, the delivery spine and the evaluation stack, and none of
that belongs on the console's import path. The checks import the app in a fresh
interpreter, because the test process has already imported most of the tree.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App

from eawf.runtime.daemon import read_contract
from eawf.surfaces.tui import launch

if TYPE_CHECKING:
    from types import ModuleType

#: The packages whose modules the console must not pay for: every read handler module,
#: and the evaluation stack the trust route reads only once it is opened.
FORBIDDEN_PACKAGES: tuple[str, ...] = ("eawf.runtime.daemon.methods", "eawf.observability.eval")

#: The handler modules that serve the reads the contract names.
_SERVING_MODULES: tuple[str, ...] = (
    "campaign_read",
    "console_records",
    "dispatch_queue",
    "pause",
    "permission",
    "question",
    "run",
    "run_content",
    "run_liveness",
    "spend",
    "state_subscribe",
)


def _modules_after_import(module: str) -> frozenset[str]:
    """Return ``sys.modules`` keys after importing *module* in a fresh interpreter.

    Raises:
        AssertionError: The import failed in the child; its stderr is surfaced.
    """
    code = f"import {module}, sys; print(chr(10).join(sys.modules))"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"import {module} failed in subprocess:\n{proc.stderr}"
    return frozenset(proc.stdout.splitlines())


def _leaks(loaded: frozenset[str]) -> list[str]:
    """Return the forbidden modules in *loaded*."""
    return sorted(
        name
        for name in loaded
        if any(name.startswith(f"{package}.") for package in FORBIDDEN_PACKAGES)
        or name == "eawf.observability.eval"
    )


def test_console_app_import_loads_no_daemon_handler_or_eval_module() -> None:
    """Importing the console app pulls in no read handler module and no eval stack."""
    leaked = _leaks(_modules_after_import("eawf.surfaces.tui.console.app"))
    assert not leaked, (
        f"import eawf.surfaces.tui.console.app pulled server-side module(s): {leaked}. "
        "Import the verb or answer from eawf.runtime.daemon.read_contract instead."
    )


def test_read_contract_imports_no_daemon_handler_or_eval_module() -> None:
    """The contract itself stays clear of the modules it exists to keep off the path."""
    assert not _leaks(_modules_after_import("eawf.runtime.daemon.read_contract"))


def test_leak_check_flags_every_handler_module() -> None:
    """The match names each handler module, the eval package and its children, nothing else."""
    loaded = frozenset(
        {
            "eawf.runtime.daemon.methods",
            "eawf.runtime.daemon.methods.state_subscribe",
            "eawf.runtime.daemon.methods.projection",
            "eawf.observability.eval",
            "eawf.observability.eval.jury",
            "eawf.observability.evaluation_unrelated",
        }
    )
    assert _leaks(loaded) == [
        "eawf.observability.eval",
        "eawf.observability.eval.jury",
        "eawf.runtime.daemon.methods.projection",
        "eawf.runtime.daemon.methods.state_subscribe",
    ]
    assert _leaks(frozenset()) == []


@pytest.mark.parametrize("module_name", _SERVING_MODULES)
def test_serving_module_shares_each_contract_name(module_name: str) -> None:
    """A handler module that exposes a contract name exposes the contract's own object."""
    import importlib

    module: ModuleType = importlib.import_module(f"eawf.runtime.daemon.methods.{module_name}")
    shared = [name for name in read_contract.__all__ if hasattr(module, name)]
    assert shared, f"{module_name} serves no read the contract names"
    assert [
        name for name in shared if getattr(module, name) is not getattr(read_contract, name)
    ] == []


def test_contract_answer_rejects_an_unknown_field() -> None:
    """An answer carrying a field the contract does not name is refused, not dropped."""
    from pydantic import ValidationError

    assert read_contract.PausesAnswer.model_validate({}).pauses == ()
    with pytest.raises(ValidationError):
        read_contract.PausesAnswer.model_validate({"pauses": [], "extra": 1})
    with pytest.raises(ValidationError):
        read_contract.TargetResolution.model_validate({"key": "TSK-0001", "ending": "gone"})


class _FirstFrameApp(App[None]):
    """A console stand-in that notes when its first frame is on screen."""

    def __init__(self) -> None:
        super().__init__()
        self.painted = False

    def on_ready(self) -> None:
        self.painted = True

    async def run_async(self, **kwargs: Any) -> None:
        await super().run_async(headless=True, **kwargs)


class _RecordingSeam:
    """A seam stand-in that records when it is connected and whether a frame preceded it."""

    def __init__(self, app: _FirstFrameApp) -> None:
        self.app = app
        self.events: list[str] = []

    async def connect(self) -> None:
        self.events.append("connect after paint" if self.app.painted else "connect before paint")
        self.app.exit()

    async def disconnect(self) -> None:
        self.events.append("disconnect")


def test_console_connects_its_seam_only_after_the_first_frame() -> None:
    """The first frame never waits on the transport's state read or socket probe."""
    app = _FirstFrameApp()
    seam = _RecordingSeam(app)
    assert launch._run_console(app, seam) == 0  # type: ignore[arg-type]
    assert seam.events == ["connect after paint", "disconnect"]


def test_console_without_a_seam_runs_and_connects_nothing() -> None:
    """The pre-session entry layer has no seam, so nothing is connected or disconnected."""
    app = _FirstFrameApp()
    app.call_after_refresh(app.exit)
    assert launch._run_console(app, None) == 0  # type: ignore[arg-type]
    assert app.painted
