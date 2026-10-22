"""The packaged Claude plugin hooks are laid out the way Claude Code loads them.

Claude Code reads a plugin's hook manifest from ``hooks/hooks.json``, runs
each wrapper it names as an executable, ends a session on ``SessionEnd``
(``Stop`` fires after every turn), and honours exit 2 from a ``PreToolUse``
hook as a deny. Each test pins one of those facts against the tree
``package_plugin`` writes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess  # noqa: EAWF024 -- the fail-closed guard is a shell script; its pinned criterion gate lives here
import tarfile
from pathlib import Path

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.runtimes.claude.hook_map import (
    PLUGIN_HOOK_REGISTRY,
    build_plugin_hooks_json,
)
from eawf.runtime.runtimes.claude.hooks_router import route_claude_payload
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.workflow.release.plugin_bundle import EXECUTABLE_MODE, build_bundle

pytestmark = pytest.mark.unit

_GUARD_REFUSAL = "Eä data-loss guard could not run (exit 3), so the call is refused"


def _package(target: Path) -> Path:
    package_plugin(target, include_hooks=True, extra_tools={})
    return target


def test_package_writes_the_manifest_under_hooks_dir(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")

    manifest = target / "hooks" / "hooks.json"
    assert manifest.is_file()
    assert not (target / "hooks.json").exists()
    body = json.loads(manifest.read_text(encoding="utf-8"))
    assert body == build_plugin_hooks_json()


def test_session_end_subscribes_session_end_not_stop() -> None:
    session_end = [s for s in PLUGIN_HOOK_REGISTRY if s.event_type is HookEventType.SESSION_END]
    assert [s.cc_event for s in session_end] == ["SessionEnd"]
    assert all(spec.cc_event != "Stop" for spec in PLUGIN_HOOK_REGISTRY)

    rendered = build_plugin_hooks_json()["hooks"]
    assert "SessionEnd" in rendered
    assert "Stop" not in rendered
    commands = [h["command"] for entry in rendered["SessionEnd"] for h in entry["hooks"]]
    assert commands == ["${CLAUDE_PLUGIN_ROOT}/hooks/session_end.sh"]

    routed = route_claude_payload({"hook_event_name": "SessionEnd", "session_id": "s-1"})
    assert routed is not None
    assert routed.event_type is HookEventType.SESSION_END
    assert route_claude_payload({"hook_event_name": "Stop", "session_id": "s-1"}) is None


def test_hook_wrappers_stay_executable_in_the_release_bundle(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    wrappers = sorted((target / "hooks").glob("*.sh"))
    assert wrappers, "the packaged tree carries no hook wrapper"
    for wrapper in wrappers:
        assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755, wrapper.name

    bundle = tmp_path / "plugin.tar.gz"
    build_bundle(target, bundle, epoch=0)
    with tarfile.open(bundle, "r:gz") as archive:
        modes = {
            member.name: member.mode
            for member in archive.getmembers()
            if member.name.startswith("hooks/") and member.name.endswith(".sh")
        }
    assert sorted(modes) == [f"hooks/{w.name}" for w in wrappers]
    assert set(modes.values()) == {EXECUTABLE_MODE}


def _run_guard(
    wrapper: Path, *, bin_dir: Path, home: Path, tool_name: str
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": {}}
    )
    return subprocess.run(
        ["bash", str(wrapper)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=home,
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(home)},
        check=False,
        timeout=30,
    )


def test_pre_tool_use_guard_fails_closed_on_a_version_mismatch(tmp_path: Path) -> None:
    target = _package(tmp_path / "plugin")
    wrapper = target / "hooks" / "pre_tool_use.sh"
    # The stub stands in for uv running ``eawf hook run pre_tool_use``, which
    # exits 3 when the installed CLI speaks another hook contract.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text("#!/bin/sh\ncat >/dev/null\nexit 3\n", encoding="utf-8")
    stub.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    judged = _run_guard(wrapper, bin_dir=bin_dir, home=home, tool_name="Bash")
    assert judged.returncode == 2, judged.stderr
    assert _GUARD_REFUSAL in judged.stderr

    unjudged = _run_guard(wrapper, bin_dir=bin_dir, home=home, tool_name="Read")
    assert unjudged.returncode == 0, unjudged.stderr
    assert _GUARD_REFUSAL not in unjudged.stderr
