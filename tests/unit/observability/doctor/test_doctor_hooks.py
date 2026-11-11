"""``eawf doctor hooks`` runs a plugin bundle's hooks across the contract matrix.

The bundles are checked through the CLI the operator runs, against a stub
bundle with known defects, the current render and the 0.7.1 wrappers
copied verbatim under ``tests/fixtures/hooks/claude-0.7.1``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 exit status and output are the contract

from eawf.observability.doctor import checks
from eawf.observability.doctor.checks_hooks import (
    HOOK_HEALTH_CHECK,
    CellFailure,
    HooksReport,
    bundle_findings,
    check_hook_health,
    installed_bundles,
    launcher_version,
)
from eawf.observability.doctor.hook_matrix import (
    CONTEXTS,
    MANAGED_CONTEXTS,
    MatrixEvent,
    StaleDaemon,
    allowed_shape_error,
    matrix_events,
    payloads_for,
)
from eawf.runtime.hooks.health import HEALTH_RECORD, HOOK_RAISED, HookFailure, record_failures
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.catalog import render_skill_classes_tsv

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")

_FIXTURE_071 = Path(__file__).resolve().parents[3] / "fixtures" / "hooks" / "claude-0.7.1"
_COMMAND = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/eawf-hook.sh" '

# Exits 2 on a pre-tool call, prints a non-JSON line on a prompt, writes to
# stderr at session end and binds a socket in the runtime directory when a
# subagent stops; everything else passes.
_BIND = (
    "import os, socket; "
    'socket.socket(socket.AF_UNIX).bind(os.environ["XDG_RUNTIME_DIR"] + "/x.sock")'
)
_BROKEN_WRAPPER = f"""#!/bin/bash
cat >/dev/null
case "$1" in
    pre_tool_use) exit 2 ;;
    user_prompt_submit) echo "not json" ;;
    session_end) echo "boom" >&2 ;;
    subagent_stop) "{sys.executable}" -c '{_BIND}' 2>/dev/null ;;
esac
exit 0
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home without a plugin registry, and a working directory outside git."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return home


def _manifest(command: Any, *, timeout: int | None = 10) -> dict[str, Any]:
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event in matrix_events():
        entry: dict[str, Any] = {"type": "command", "command": command(event.value)}
        if timeout is not None:
            entry["timeout"] = timeout
        hooks[event.cc_event] = [{"matcher": "", "hooks": [entry]}]
    return {"hooks": hooks}


def _plugin_json(root: Path, version: str | None) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "eawf", "version": version}), encoding="utf-8"
    )


def _stub_bundle(root: Path, wrapper: str) -> Path:
    """A bundle in the 0.7.2 layout whose wrapper is *wrapper*."""
    (root / "hooks").mkdir(parents=True)
    (root / "skills" / "research").mkdir(parents=True)
    (root / "hooks" / "eawf-hook.sh").write_text(wrapper, encoding="utf-8")
    (root / "hooks" / "hooks.json").write_text(
        json.dumps(_manifest(lambda event: f"{_COMMAND}{event}")), encoding="utf-8"
    )
    (root / "hooks" / "skill-classes.tsv").write_text(render_skill_classes_tsv(), "utf-8")
    launched = launcher_version(os.environ)
    _plugin_json(root, None if launched is None else launched.partition("+")[0])
    return root


def _bundle_071(root: Path) -> Path:
    """The 0.7.1 bundle: its ten verbatim wrappers under the manifest 0.7.1 rendered."""
    shutil.copytree(_FIXTURE_071, root / "hooks")
    (root / "hooks" / "hooks.json").write_text(
        json.dumps(
            _manifest(
                lambda event: f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/{event}.sh"', timeout=None
            )
        ),
        encoding="utf-8",
    )
    _plugin_json(root, "0.7.1")
    return root


def _doctor(*bundles: Path) -> Any:
    args = ["doctor", "hooks"]
    for bundle in bundles:
        args += ["--bundle", str(bundle)]
    return CliRunner().invoke(app, args)


def test_failing_cell_line(tmp_path: Path, home: Path) -> None:
    bundle = _stub_bundle(tmp_path / "broken", _BROKEN_WRAPPER)

    result = _doctor(bundle)

    assert result.exit_code == 1, result.output
    lines = {line.strip() for line in result.stdout.splitlines()}
    assert "no-git pre_tool_use bash: exit expected 0 got 2" in lines
    assert "managed-root pre_tool_use daemon-down: exit expected 0 got 2" in lines
    assert (
        "unmanaged-git user_prompt_submit plan: stdout expected empty or one "
        "UserPromptSubmit document got not one JSON document: not json"
    ) in lines
    assert "unmanaged-git session_end exit: stderr expected empty got boom" in lines
    # A managed hook may name what it did on stderr.
    assert not any(line.startswith("managed-root session_end exit: stderr") for line in lines)
    assert "no-git subagent_stop agent: daemon expected no socket got x.sock" in lines
    assert not any("session_start" in line for line in lines)
    # A guard that answers nothing in a managed context is dead, not passing.
    for context in sorted(MANAGED_CONTEXTS):
        for label in ("enter-worktree", "agent-isolation"):
            assert f"{context} pre_tool_use {label}: guard expected deny got no decision" in lines
    assert not any(line.startswith("no-git pre_tool_use enter-worktree: guard") for line in lines)


def test_doctor_hooks_on_both_bundles(tmp_path: Path, home: Path) -> None:
    current = tmp_path / "current"
    package_plugin(current, include_marketplace=False, include_readme=False, extra_tools={})
    old = _bundle_071(tmp_path / "old")

    passing = _doctor(current)
    failing = _doctor(old)

    assert passing.exit_code == 0, passing.output
    assert "282 cells, ok" in passing.stdout
    assert failing.exit_code == 1, failing.output
    lines = {line.strip() for line in failing.stdout.splitlines()}
    assert "timeout PreToolUse: expected a timeout got none" in lines
    blocked = sorted(line for line in lines if line.startswith("runner-missing pre_tool_use bash"))
    assert blocked == ["runner-missing pre_tool_use bash: exit expected 0 got 2"], blocked


def test_bundle_checks(tmp_path: Path, home: Path) -> None:
    def findings(root: Path, project: Path | None = None) -> list[str]:
        return bundle_findings(root, project=project, home=home, env=os.environ)

    good = _stub_bundle(tmp_path / "good", "#!/bin/bash\nexit 0\n")
    assert findings(good) == []

    no_timeout = _stub_bundle(tmp_path / "no-timeout", "")
    manifest = _manifest(lambda event: f"{_COMMAND}{event}")
    del manifest["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
    (no_timeout / "hooks" / "hooks.json").write_text(json.dumps(manifest), "utf-8")
    assert findings(no_timeout) == ["timeout PreToolUse: expected a timeout got none"]

    old_form = _stub_bundle(tmp_path / "old-form", "")
    manifest = _manifest(lambda event: f"{_COMMAND}{event}")
    manifest["hooks"]["SessionEnd"][0]["hooks"][0]["command"] = "hooks/session_end.sh"
    (old_form / "hooks" / "hooks.json").write_text(json.dumps(manifest), "utf-8")
    assert findings(old_form) == [
        "command SessionEnd: expected bash ... eawf-hook.sh <event> got hooks/session_end.sh"
    ]

    twice = _stub_bundle(tmp_path / "twice", "")
    manifest = _manifest(lambda event: f"{_COMMAND}{event}")
    manifest["hooks"]["SessionStart"].append(manifest["hooks"]["SessionStart"][0])
    (twice / "hooks" / "hooks.json").write_text(json.dumps(manifest), "utf-8")
    assert findings(twice) == ["subscription session_start: expected 1 entry got 2"]

    project = tmp_path / "project"
    (project / ".claude" / "skills" / "research").mkdir(parents=True)
    (project / ".claude" / "skills" / "research" / "SKILL.md").write_text("x", "utf-8")
    assert findings(good, project) == [
        "duplicate skill research: expected only the plugin's got a copy "
        "under the project .claude/skills"
    ]

    classes = _stub_bundle(tmp_path / "classes", "")
    (classes / "hooks" / "skill-classes.tsv").write_text("research\trefused\n", "utf-8")
    assert findings(classes) == ["skill-classes.tsv: expected the catalogue got a different table"]

    stale = _stub_bundle(tmp_path / "stale", "")
    _plugin_json(stale, "0.0.1")
    (finding,) = findings(stale)
    assert finding.startswith("version: expected the launcher's eawf ")
    assert finding.endswith(" got bundle 0.0.1")


def test_bundle_checks_on_an_unreadable_manifest(tmp_path: Path, home: Path) -> None:
    root = _stub_bundle(tmp_path / "b", "")
    (root / "hooks" / "hooks.json").write_text('{"hooks": {}, "extra": 1}', "utf-8")

    found = bundle_findings(root, project=None, home=home, env=os.environ)

    assert found[0].startswith("manifest: expected an eawf hooks.json got ")


def test_doctor_hooks_without_an_installed_bundle_passes(home: Path) -> None:
    result = _doctor()

    assert result.exit_code == 0, result.output
    assert "no eawf plugin bundle installed; nothing to check" in result.stdout


def test_doctor_hooks_reports_json(tmp_path: Path, home: Path) -> None:
    result = CliRunner().invoke(app, ["doctor", "hooks", "--json", "--bundle", str(tmp_path)])

    body = json.loads(result.stdout)
    assert result.exit_code == 1
    assert body["ok"] is False
    assert body["bundles"][0]["cells"] == 0


def _registry(home: Path, plugins: object) -> None:
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plugins": plugins}), "utf-8")


def test_installed_bundles_without_a_registry_is_empty(home: Path) -> None:
    assert installed_bundles(home) == []


def test_installed_bundles_of_a_malformed_registry_is_empty(home: Path) -> None:
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", "utf-8")

    assert installed_bundles(home) == []


def test_installed_bundles_keeps_existing_eawf_installs_once(tmp_path: Path, home: Path) -> None:
    root = tmp_path / "cache" / "eawf"
    root.mkdir(parents=True)
    _registry(
        home,
        {
            "eawf@eawf": [{"installPath": str(root)}, {"installPath": str(root)}],
            "eawf@other": [{"installPath": str(tmp_path / "gone")}, "junk"],
            "other@eawf": [{"installPath": str(root)}],
        },
    )

    assert installed_bundles(home) == [root]


def test_two_installed_bundles_are_a_double_subscription(tmp_path: Path, home: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _registry(
        home, {"eawf@a": [{"installPath": str(first)}], "eawf@b": [{"installPath": str(second)}]}
    )

    result = _doctor()

    assert result.exit_code == 1
    assert "subscription: expected 1 installed eawf plugin got 2" in result.stdout


def test_launcher_version_without_eawf_on_path_is_none(tmp_path: Path) -> None:
    assert launcher_version({"PATH": str(tmp_path)}) is None


def test_cell_failure_line() -> None:
    failure = CellFailure("no-git", "pre_tool_use", "bash", "exit", "0", "2")

    assert failure.line() == "no-git pre_tool_use bash: exit expected 0 got 2"


def test_report_without_bundles_or_findings_is_ok() -> None:
    assert HooksReport(bundles=(), findings=()).ok


@pytest.mark.parametrize(
    ("cc_event", "stdout", "error"),
    [
        ("SessionEnd", "", None),
        ("SessionEnd", " \n", None),
        ("SessionStart", '{"hookSpecificOutput":{"hookEventName":"SessionStart"}}', None),
        ("SessionStart", '{"systemMessage":"x"}', None),
        ("UserPromptSubmit", '{"decision":"block","reason":"r"}', None),
        ("SessionEnd", "{", "not one JSON document: {"),
        ("SessionEnd", "[]", "a JSON list, not an object"),
        ("SubagentStop", '{"body":{}}', "keys ['body']"),
        ("SessionEnd", '{"hookSpecificOutput":{}}', "keys ['hookSpecificOutput']"),
        (
            "PreToolUse",
            '{"hookSpecificOutput":{"hookEventName":"PostToolUse"}}',
            "hookSpecificOutput not named PreToolUse",
        ),
        (
            "SessionStart",
            '{"hookSpecificOutput":{"hookEventName":"SessionStart","x":1}}',
            "hookSpecificOutput keys ['x']",
        ),
    ],
)
def test_allowed_shape_error(cc_event: str, stdout: str, error: str | None) -> None:
    assert allowed_shape_error(cc_event, stdout) == error


def test_allowed_shape_error_refuses_an_unknown_event() -> None:
    with pytest.raises(KeyError):
        allowed_shape_error("Stop", "")


def test_payloads_for_adds_the_daemon_variants_only_when_managed() -> None:
    event = MatrixEvent("session_start", "SessionStart")

    unmanaged = [p.label for p in payloads_for(event, managed=False)]
    managed = [p.label for p in payloads_for(event, managed=True)]

    assert unmanaged == ["startup", "empty", "malformed"]
    assert managed == ["startup", "empty", "malformed", "daemon-down", "stale-daemon"]


def test_payloads_for_refuses_an_event_without_payloads() -> None:
    with pytest.raises(KeyError):
        payloads_for(MatrixEvent("pre_commit", "PreToolUse"), managed=False)


def test_the_matrix_covers_ten_events_and_six_contexts() -> None:
    assert len(matrix_events()) == 10
    assert len(CONTEXTS) == 6
    assert {"managed-root", "managed-subfolder", "managed-worktree"} == MANAGED_CONTEXTS


def test_a_dead_stand_in_refuses_connections(tmp_path: Path) -> None:
    with StaleDaemon(tmp_path / "s.sock", serve=False) as path:
        probe = socket.socket(socket.AF_UNIX)
        with pytest.raises(OSError):
            probe.connect(str(path))
        probe.close()


def test_a_stale_stand_in_answers_version_000(tmp_path: Path) -> None:
    with (
        StaleDaemon(tmp_path / "s.sock", serve=True) as path,
        socket.socket(socket.AF_UNIX) as probe,
    ):
        probe.connect(str(path))
        probe.sendall(b'{"id":"p","method":"daemon.ping"}\n')
        reply = json.loads(probe.makefile("rb").readline())

    assert reply["id"] == "p"
    assert reply["result"] == {"version": "0.0.0", "pid": os.getpid()}


def test_hook_health_without_a_workspace_is_skipped() -> None:
    assert check_hook_health(workspace=None).status == "ok"


def test_hook_health_without_failures_is_ok(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()

    result = check_hook_health(workspace=tmp_path)

    assert (result.name, result.status) == (HOOK_HEALTH_CHECK, "ok")


def test_hook_health_warns_about_recorded_failures(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    failure = HookFailure("e", "h", HOOK_RAISED, "E", "", datetime.now(UTC))
    record_failures(tmp_path / ".ea", [failure, failure])

    result = check_hook_health(workspace=tmp_path)

    assert result.status == "warn"
    assert result.detail == (
        "2 hook failures recorded (top: hook_raised x2); run eawf doctor hooks"
    )


def test_hook_health_warns_about_an_unreadable_record(tmp_path: Path) -> None:
    (tmp_path / ".ea" / HEALTH_RECORD).mkdir(parents=True)

    assert check_hook_health(workspace=tmp_path).status == "warn"


def test_run_all_registers_hook_health() -> None:
    assert "check_hook_health" in checks.run_all.__code__.co_names
