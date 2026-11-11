"""The data-loss guard's session table through the real rendered hook wrapper.

Each row of ``tests/unit/runtime/sandbox/test_data_loss_reach_table.py`` is fed
to the rendered ``pre_tool_use`` wrapper as Claude Code feeds it, with a
stand-in ``uv`` that counts its starts and runs the current CLI with every
daemon call refused. In a session in a managed tree a destructive row prints
the host's deny; outside every managed tree no row prints anything and the
wrapper never starts eawf, whatever path the command names, except for a
payload whose cwd it cannot read for certain, which eawf then decides.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

import eawf
from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import render_hook_sh
from tests.unit.runtime.sandbox.test_data_loss_reach_table import (
    CASES,
    HANDED_OVER,
    OFF,
    SESSION,
    Row,
    build_layout,
    denied,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed"),
]

_SRC: Final = Path(eawf.__file__).resolve().parents[1]

# Runs the current CLI with every daemon call refused, so no daemon starts.
_LAUNCHER: Final = """
import sys
import eawf.runtime.hooks.runner as runner
import eawf.surfaces.cli._daemon_client as daemon_client

def _down(*_args, **_kwargs):
    raise ConnectionRefusedError("daemon down")

runner._default_daemon_client_factory = _down
daemon_client.DaemonClient = _down
sys.argv = ["eawf", *sys.argv[1:]]
from eawf.__main__ import main
main()
"""

# Stands in for uv: counts the start, drops ``run [...]`` and runs the rest
# of the line through the launcher.
_STUB_UV: Final = """#!/bin/bash
echo "started $*" >>"$EAWF_TEST_STARTS"
while [ $# -gt 0 ] && [ "$1" != "eawf" ]; do shift; done
shift
exec "$EAWF_TEST_PYTHON" "$EAWF_TEST_LAUNCHER" "$@"
"""


class _Harness:
    """The rendered wrapper, its stand-in runner and the trees it runs against."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.layout = build_layout(root / "trees")
        self.stubs = root / "stubs"
        self.stubs.mkdir()
        (self.stubs / "uv").write_text(_STUB_UV, encoding="utf-8")
        (self.stubs / "uv").chmod(0o755)
        (self.stubs / "eawf").write_text("#!/bin/bash\n", encoding="utf-8")
        (self.stubs / "eawf").chmod(0o755)
        (root / "launcher.py").write_text(_LAUNCHER, encoding="utf-8")
        self.script = root / "pre_tool_use.sh"
        self.script.write_text(
            render_hook_sh(HookEventType.PRE_TOOL_USE, runtime="claude"), encoding="utf-8"
        )

    def run(
        self, context: str, payload: str, starts: Path, directory: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Feed *payload* to the wrapper in *context*; count runner starts in *starts*.

        *directory* is the wrapper's own directory; the context's when ``None``.
        """
        _, project = self.layout.context(context)
        runtime = self.root / "xdg"
        runtime.mkdir(exist_ok=True)
        env = {
            **self.layout.env(),
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "TMPDIR": str(self.root),
            "XDG_RUNTIME_DIR": str(runtime),
            "PYTHONPATH": str(_SRC),
            "EAWF_TEST_PYTHON": sys.executable,
            "EAWF_TEST_LAUNCHER": str(self.root / "launcher.py"),
            "EAWF_TEST_STARTS": str(starts),
        }
        if project is not None:
            env["CLAUDE_PROJECT_DIR"] = str(project)
        return subprocess.run(
            ["bash", str(self.script)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=directory or self.layout.process_dir(context),
            check=False,
            timeout=60,
        )


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> _Harness:
    return _Harness(tmp_path_factory.mktemp("reach-wrapper"))


def _starts(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


@pytest.mark.parametrize(("context", "row"), CASES)
def test_reach_wrapper_table(harness: _Harness, tmp_path: Path, context: str, row: Row) -> None:
    starts = tmp_path / "starts"
    payload = harness.layout.payload(context, row.tool, row.tool_input)
    done = harness.run(context, payload, starts)
    assert done.returncode == 0, done.stderr
    if denied(context, row):
        decision = json.loads(done.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny", done.stdout
        return
    assert done.stdout == "", done.stdout
    if context in OFF and context not in HANDED_OVER:
        # The session is not in a managed tree: the wrapper stands down itself.
        assert _starts(starts) == 0, "the wrapper started eawf outside every managed tree"


@pytest.mark.parametrize(
    "cwd",
    ["$(touch {canary})", "`touch {canary}`", "x; touch {canary}", "{canary}\\nx"],
    ids=["substitution", "backticks", "separator", "escape"],
)
def test_reach_wrapper_never_evaluates_the_cwd(harness: _Harness, tmp_path: Path, cwd: str) -> None:
    canary = tmp_path / "canary"
    payload = json.dumps(
        {
            "session_id": SESSION,
            "cwd": cwd.format(canary=canary),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    starts = tmp_path / "starts"
    done = harness.run("unmanaged-git", payload, starts)
    assert (done.returncode, done.stdout) == (0, "")
    assert not canary.exists()
    assert _starts(starts) == 0


@pytest.mark.parametrize(
    ("shape", "denies"),
    [("no-cwd", True), ("cwd-late", True), ("cwd-nested-key-unmanaged", False)],
)
def test_reach_wrapper_keeps_w_in_a_managed_checkout_read_unsure(
    harness: _Harness, tmp_path: Path, shape: str, denies: bool
) -> None:
    """In a managed checkout an unsure reading still names it; the payload's cwd decides."""
    managed = harness.layout.managed
    if shape == "no-cwd":
        payload = json.dumps(
            {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "rm -rf .ea"}}
        )
    else:
        payload = harness.layout.payload(shape, "Bash", {"command": "rm -rf .ea"})
    starts = tmp_path / "starts"
    done = harness.run(
        shape if shape != "no-cwd" else "managed-root", payload, starts, directory=managed
    )
    assert done.returncode == 0, done.stderr
    assert starts.read_text().splitlines() == [
        f"started run --no-project eawf -w {managed} hook run pre_tool_use --runtime claude"
    ]
    if denies:
        assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert done.stdout == ""
