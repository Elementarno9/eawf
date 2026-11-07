"""The version probe and the hook entry path load no state, spec or store module.

Every hook call and every ``eawf --version`` is a fresh process, so each
import on those paths is paid per call. Each probe runs in a fresh
interpreter: the pytest process has already imported most of the tree, so an
in-process ``sys.modules`` check would prove nothing.
"""

from __future__ import annotations

import json
import subprocess  # noqa: EAWF024 a fresh interpreter is the only honest sys.modules probe
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from eawf import __main__ as entry
from eawf import __version__
from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import HOOK_REGISTRY
from eawf.workflow.skills.publication import BUNDLE_TARGET_EPOCH
from tests._epoch2_helpers import lay_epoch2_tree

#: The model packages neither path may load before its daemon call.
FORBIDDEN_PREFIXES: tuple[str, ...] = ("eawf.kernel.state", "eawf.kernel.spec", "eawf.kernel.store")

#: Runs ``eawf --version`` through the console entry point, then lists ``sys.modules``.
_VERSION_PROBE = """
import json, sys
sys.argv = ["eawf", "--version"]
from eawf.__main__ import main
try:
    main()
except SystemExit:
    pass
print(json.dumps(sorted(sys.modules)))
"""

#: Runs ``eawf hook run <event>`` as the rendered hook wrapper calls it,
#: ``--target-epoch`` included, and writes ``sys.modules`` to the file named by
#: argv[2] at three points: when the runner dispatches the event to its first
#: handler, when a handler first opens a daemon client, and at process exit. The
#: stand-in daemon client answers every call with ``{}``.
_HOOK_PROBE = """
import atexit, io, json, sys, types

event, dump, epoch, transcript = sys.argv[1:5]
seen = {"dispatch": None, "daemon": None, "exit": None}

def snapshot(point):
    if seen[point] is None:
        seen[point] = sorted(sys.modules)

def write():
    snapshot("exit")
    with open(dump, "w", encoding="utf-8") as handle:
        json.dump(seen, handle)

atexit.register(write)

class DaemonClient:
    def __init__(self):
        snapshot("daemon")
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return None
    def call(self, method, params):
        return {}

stand_in = types.ModuleType("eawf.surfaces.cli._daemon_client")
stand_in.DaemonClient = DaemonClient
sys.modules["eawf.surfaces.cli._daemon_client"] = stand_in

from eawf.runtime.hooks.runner import HookRunner

run_event = HookRunner.run_event

def spied_run_event(self, hook_event):
    snapshot("dispatch")
    return run_event(self, hook_event)

HookRunner.run_event = spied_run_event
sys.stdin = io.StringIO(json.dumps({
    "session_id": "s1",
    "transcript_path": transcript,
    "agent_id": "a1",
    "agent_transcript_path": transcript,
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
    "tool_response": {"stdout": "ok"},
    "prompt": "hello",
}))
sys.argv = ["eawf", "hook", "run", event, "--runtime", "claude", "--target-epoch", epoch]
from eawf.__main__ import main
main()
"""

#: Every event a rendered wrapper runs: the Claude installer wires the
#: handler-backed ones, the Codex installer every one.
WRAPPER_EVENTS: tuple[str, ...] = tuple(spec.event_type.value for spec in HOOK_REGISTRY)

#: Events whose own handler loads the state models before any daemon call, each
#: with the reason. These are open defects, kept visible as strict xfails so a
#: fix turns them red until the entry is removed.
MODELS_BEFORE_DAEMON: dict[str, str] = {
    HookEventType.SESSION_START.value: (
        "the transcript index handler runs before the context boundary's daemon call "
        "and types its rows with eawf.kernel.state models"
    ),
    HookEventType.SESSION_END.value: (
        "the transcript index handler makes no daemon call and types its rows with "
        "eawf.kernel.state models"
    ),
    HookEventType.AGENT_END.value: (
        "the interim verdict seeder writes the report through the store and makes no daemon call"
    ),
    HookEventType.PERMISSION_REQUEST.value: (
        "the permission wait is read from the layered config, whose schema imports "
        "eawf.kernel.state, before the daemon call so a malformed wait never reaches it"
    ),
}


def _forbidden(modules: list[str]) -> list[str]:
    return [name for name in modules if name.startswith(FORBIDDEN_PREFIXES)]


@cache
def _probe_hook(event: str, root: Path) -> dict[str, Any]:
    """Run the hook probe for *event* in a fresh epoch-2 tree under *root*, once per event.

    Args:
        event: The hook event the wrapper runs.
        root: A directory the probe may lay its tree and transcript in.

    Returns:
        The ``sys.modules`` snapshots keyed ``dispatch``, ``daemon`` and
        ``exit``; a point the process never reached is ``None``.
    """
    repo = root / event
    lay_epoch2_tree(repo)
    transcript = root / f"{event}.jsonl"
    transcript.write_text('{"message": {"content": "hello"}}\n', encoding="utf-8")
    dump = root / f"{event}.json"
    epoch = str(BUNDLE_TARGET_EPOCH)
    proc = subprocess.run(
        [sys.executable, "-c", _HOOK_PROBE, event, str(dump), epoch, str(transcript)],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert dump.is_file(), f"the probe wrote no snapshot:\n{proc.stderr}"
    return dict(json.loads(dump.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def probe_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("hook-import-path")


def test_version_loads_no_model_module_and_no_pydantic(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _VERSION_PROBE],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    version_line, modules_line = proc.stdout.strip().splitlines()
    assert version_line.startswith(__version__)
    modules = json.loads(modules_line)
    assert _forbidden(modules) == []
    assert [name for name in modules if name.split(".")[0] in {"pydantic", "pydantic_core"}] == []


def test_wrapper_events_cover_every_handler_backed_event() -> None:
    from eawf.runtime.hooks.runner import registered_handler_event_types

    assert {event.value for event in registered_handler_event_types()} <= set(WRAPPER_EVENTS)
    assert set(MODELS_BEFORE_DAEMON) <= set(WRAPPER_EVENTS)


@pytest.mark.parametrize(
    "event", [event for event in WRAPPER_EVENTS if event != HookEventType.AGENT_END.value]
)
def test_hook_run_dispatches_without_model_modules(probe_root: Path, event: str) -> None:
    # The entry path, the epoch check and every handler registration have run
    # by the time the runner dispatches; agent_end is answered by the verdict
    # seeder before any runner exists.
    dispatched = _probe_hook(event, probe_root)["dispatch"]
    assert dispatched is not None, "the hook never reached its runner"
    assert _forbidden(dispatched) == []


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            event,
            marks=pytest.mark.xfail(reason=MODELS_BEFORE_DAEMON[event], strict=True),
        )
        if event in MODELS_BEFORE_DAEMON
        else event
        for event in WRAPPER_EVENTS
    ],
)
def test_hook_run_reaches_daemon_without_model_modules(probe_root: Path, event: str) -> None:
    # An event that never calls the daemon is held to the whole process.
    snapshots = _probe_hook(event, probe_root)
    modules = snapshots["daemon"] if snapshots["daemon"] is not None else snapshots["exit"]
    assert _forbidden(modules) == []


def test_skill_catalog_loads_only_for_a_skill_call(probe_root: Path) -> None:
    # A pre_tool_use for any tool but the skill tool leaves the catalog unloaded.
    assert "eawf.workflow.skills.catalog" not in _probe_hook("pre_tool_use", probe_root)["exit"]


@pytest.mark.parametrize(
    ("argv", "served"),
    [
        (["--version"], True),
        ([], False),
        (["--version", "--json"], False),
        (["--json", "--version"], False),
        (["version"], False),
        (["--versions"], False),
    ],
)
def test_serve_version_answers_only_the_bare_flag(
    argv: list[str], served: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    assert entry._serve_version(argv) is served
    out = capsys.readouterr().out
    assert bool(out.strip()) is served


def test_serve_version_prints_the_cli_banner(capsys: pytest.CaptureFixture[str]) -> None:
    from eawf.surfaces.cli._version_display import compose_display_version

    entry._serve_version(["--version"])
    assert capsys.readouterr().out == f"{compose_display_version()}\n"


def test_runner_registers_lifecycle_handlers_under_their_module_names() -> None:
    from eawf.runtime.hooks import lifecycle_capture, runner

    assert runner.SESSION_INDEX_HOOK == lifecycle_capture.SESSION_INDEX_HOOK
    assert runner.SESSION_END_HOOK == lifecycle_capture.SESSION_END_HOOK
    assert tuple(lifecycle_capture.HOST_SUBAGENT_METHODS) == runner.HOST_SUBAGENT_EVENTS
