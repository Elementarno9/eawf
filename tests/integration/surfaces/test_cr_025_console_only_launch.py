"""CR-025: ``eawf ui`` never constructs the epoch-1 application, on any launch path.

Each test runs one launch in a fresh interpreter and reads back which modules it
loaded, so an import anywhere on the path -- the launcher, the console, a package
``__init__`` -- is caught, not only a direct call. The paths are the interactive TTY,
``--plain``, ``--no-input`` and a stdout that is not a TTY, each over an epoch-2 tree
and over an epoch-1 one. An interactive run hands its app to a stub instead of an event
loop, so nothing here takes a terminal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.surfaces.tui.console.plain import OFFLINE_SNAPSHOT

#: The epoch-1 app and the epoch-1 headless status emitter the launcher once fell back to.
_EPOCH1_MODULES = ("eawf.surfaces.tui.app", "eawf.surfaces.tui.chassis.offline")

# Runs one launch with stdout's TTY answer forced, then writes what it loaded to argv[4].
_DRIVER = """
import json, sys

tty, no_input, plain = (flag == "1" for flag in sys.argv[1:4])
report = sys.argv[4]
real = sys.stdout


class _Stdout:
    def isatty(self):
        return tty

    def write(self, text):
        return real.write(text)

    def flush(self):
        real.flush()


sys.stdout = _Stdout()
import eawf.surfaces.tui.launch as launch

opened = []
launch._run_console = lambda app, seam: opened.append(type(app).__module__) or 0
rc = launch.launch_tui(workspace=None, no_input=no_input, plain=plain)
sys.stdout = real
with open(report, "w") as out:
    json.dump({"rc": rc, "opened": opened, "loaded": sorted(sys.modules)}, out)
"""

_PATHS = [
    pytest.param(True, False, False, id="tty"),
    pytest.param(True, False, True, id="plain"),
    pytest.param(True, True, False, id="no-input"),
    pytest.param(False, False, False, id="non-tty"),
]


def _activate_epoch2(ea: Path) -> None:
    """Declare and activate the tree at ``ea``, so it resolves to epoch 2."""
    (ea / GENERATIONS_DIRNAME).mkdir(parents=True)
    (ea / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "CR-025"})
    )
    (ea / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "epoch": 2,
                "generation_id": "gen-" + "ab" * 8,
                "manifest_digest": "cd" * 32,
                "generation_digest": "cd" * 32,
                "written_at": "2026-09-24T00:00:00Z",
            }
        )
    )


def _launch(
    tmp_path: Path, *, tty: bool, no_input: bool, plain: bool
) -> tuple[dict[str, object], subprocess.CompletedProcess[str]]:
    """Launch ``eawf ui`` over ``tmp_path``'s tree in a fresh interpreter.

    Returns:
        The launch's report (exit code, apps opened, modules loaded) and the process.
    """
    report = tmp_path / "report.json"
    flags = [str(int(flag)) for flag in (tty, no_input, plain)]
    env = {
        "EA_STATE": str(tmp_path / ".ea" / "state.json"),
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin:/bin",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, *flags, str(report)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert report.is_file(), proc.stderr
    return json.loads(report.read_text()), proc


@pytest.mark.parametrize(("tty", "no_input", "plain"), _PATHS)
def test_cr_025_an_epoch2_tree_never_loads_the_epoch1_app(
    tmp_path: Path, *, tty: bool, no_input: bool, plain: bool
) -> None:
    _activate_epoch2(tmp_path / ".ea")
    result, proc = _launch(tmp_path, tty=tty, no_input=no_input, plain=plain)
    assert [m for m in _EPOCH1_MODULES if m in result["loaded"]] == []
    assert result["rc"] == 0
    if tty and not (no_input or plain):
        assert result["opened"] == ["eawf.surfaces.tui.console.app"]
    else:
        # the console's own frame in plain mode: ASCII, under the offline snapshot
        assert result["opened"] == []
        assert OFFLINE_SNAPSHOT in proc.stdout
        assert proc.stdout.isascii()


@pytest.mark.parametrize(("tty", "no_input", "plain"), _PATHS)
def test_cr_025_an_epoch1_tree_never_loads_the_epoch1_app(
    tmp_path: Path, *, tty: bool, no_input: bool, plain: bool
) -> None:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "state.json").write_text("{}")
    result, proc = _launch(tmp_path, tty=tty, no_input=no_input, plain=plain)
    assert [m for m in _EPOCH1_MODULES if m in result["loaded"]] == []
    # migration-required is terminal: the console hands over its commands and exits 4
    assert result["rc"] == 4
    assert "eawf migrate epoch2 --plan" in proc.stderr
    interactive = tty and not (no_input or plain)
    assert result["opened"] == (["eawf.surfaces.tui.console.app"] if interactive else [])
