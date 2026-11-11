"""The plugin's one hook wrapper probes the repository first and always exits 0.

The wrapper is packaged exactly as the plugin ships it and run through
``bash`` the way the manifest runs it. Outside a managed repository it starts
no interpreter, writes nothing and prints nothing but the refusal of a
managed-only skill. Inside one, a missing ``uv`` or ``eawf`` and a child that
exits non-zero each leave one health row and an exit status of 0.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.config.schema import MAX_PERMISSION_WAIT_SECONDS
from eawf.runtime.hooks.health import HEALTH_RECORD, read_health_rows, take_failure_warning
from eawf.runtime.runtimes.claude.hook_map import (
    _PERMISSION_TIMEOUT_MARGIN_S,
    handler_backed_plugin_hooks,
)
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import hook as hook_command
from eawf.workflow.skills.catalog import NOT_MANAGED_REFUSAL
from tests._linked_worktree import git, repo_with_linked_worktree

pytestmark = pytest.mark.integration

_BASH = "/bin/bash"
_SYSTEM_PATH = "/usr/bin:/bin"
_EVENTS: tuple[str, ...] = tuple(spec.event_type.value for spec in handler_backed_plugin_hooks())
_PLAN_REFUSAL = json.dumps(
    {"decision": "block", "reason": NOT_MANAGED_REFUSAL}, separators=(",", ":")
)

# A stand-in for uv: records its arguments, then runs what follows ``run ... eawf``.
_STUB_UV = """#!/bin/bash
printf '%s\\n' "$*" >>"$EAWF_TEST_LOG"
while [ $# -gt 0 ] && [ "$1" != "eawf" ]; do shift; done
exec "$@"
"""


@pytest.fixture(scope="module")
def wrapper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wrapper as ``package_plugin`` writes it, beside its skill classes."""
    target = tmp_path_factory.mktemp("plugin") / "eawf"
    package_plugin(target, include_marketplace=False, include_readme=False, extra_tools={})
    return target / "hooks" / "eawf-hook.sh"


def _stub(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, path: str, **extra: str) -> dict[str, str]:
    """A hook environment with a private HOME, so the PATH bootstrap finds nothing of ours."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runtime = tmp_path / "xdg"
    runtime.mkdir(exist_ok=True)
    return {
        "PATH": path,
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        "TMPDIR": str(tmp_path),
        "EAWF_TEST_LOG": str(tmp_path / "calls.log"),
        **extra,
    }


def _run(
    wrapper: Path, event: str, payload: dict[str, Any] | str, *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [_BASH, str(wrapper), event],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=30,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _health(main: Path) -> list[dict[str, Any]]:
    record = main / ".ea" / HEALTH_RECORD
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


def _scratch_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    return root.resolve()


# ---- CR-856 manifest shape -------------------------------------------------------------------


def test_manifest_shape(wrapper: Path) -> None:
    manifest = json.loads((wrapper.parent / "hooks.json").read_text(encoding="utf-8"))
    # The permission hook outlasts the longest wait a repository may set.
    permission = MAX_PERMISSION_WAIT_SECONDS + _PERMISSION_TIMEOUT_MARGIN_S
    timeouts = {"UserPromptSubmit": 3, "PreToolUse": 5, "PermissionRequest": permission}
    seen: list[str] = []
    for cc_event, entries in manifest["hooks"].items():
        for entry in entries:
            (hook,) = entry["hooks"]
            event = hook["command"].rsplit(" ", 1)[1]
            assert hook["command"] == f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/eawf-hook.sh" {event}'
            assert hook["timeout"] == timeouts.get(cc_event, 10), cc_event
            seen.append(event)
    assert sorted(seen) == sorted(_EVENTS)
    assert len(_EVENTS) == 10
    assert sorted(p.name for p in wrapper.parent.glob("*.sh")) == ["eawf-hook.sh"]
    text = wrapper.read_text(encoding="utf-8")
    assert "set -e" not in text
    assert "--target-epoch" not in text


# ---- CR-857 unmanaged skills -----------------------------------------------------------------


def test_unmanaged_research_silent_plan_refused(wrapper: Path, tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path / "scratch")
    env = _env(tmp_path, os.environ["PATH"])

    research = _run(
        wrapper,
        "user_prompt_submit",
        {"cwd": str(repo), "prompt": "/eawf:research x"},
        cwd=repo,
        env=env,
    )
    plan = _run(
        wrapper,
        "user_prompt_submit",
        {"cwd": str(repo), "prompt": "/eawf:plan x"},
        cwd=repo,
        env=env,
    )

    assert (research.returncode, research.stdout, research.stderr) == (0, "", "")
    assert (plan.returncode, plan.stdout, plan.stderr) == (0, _PLAN_REFUSAL, "")


@pytest.mark.parametrize(
    ("event", "payload", "refused"),
    [
        ("user_prompt_submit", {"prompt": "/eawf:eawf-rules-executor"}, True),
        ("user_prompt_submit", {"prompt": "/eawf:plan"}, True),
        ("user_prompt_submit", {"prompt": "/eawf:planner x"}, False),
        ("user_prompt_submit", {"prompt": "/plan x"}, False),
        ("user_prompt_submit", {"prompt": "please /eawf:plan x"}, False),
        ("user_prompt_submit", {"prompt": "/eawf:spike x"}, False),
        ("pre_tool_use", {"tool_name": "Skill", "tool_input": {"skill": "eawf:plan"}}, True),
        ("pre_tool_use", {"tool_name": "Skill", "tool_input": {"skill": "eawf:research"}}, False),
        ("pre_tool_use", {"tool_name": "Bash", "tool_input": {"skill": "eawf:plan"}}, False),
        ("post_tool_use", {"tool_name": "Skill", "tool_input": {"skill": "eawf:plan"}}, False),
    ],
)
def test_unmanaged_refuses_only_a_managed_only_eawf_skill(
    wrapper: Path, tmp_path: Path, event: str, payload: dict[str, Any], refused: bool
) -> None:
    repo = _scratch_repo(tmp_path / "scratch")
    result = _run(
        wrapper, event, {"cwd": str(repo), **payload}, cwd=repo, env=_env(tmp_path, _SYSTEM_PATH)
    )
    assert (result.returncode, result.stderr) == (0, "")
    if not refused:
        assert result.stdout == ""
    elif event == "user_prompt_submit":
        assert result.stdout == _PLAN_REFUSAL
    else:
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == NOT_MANAGED_REFUSAL


def test_unmanaged_without_skill_classes_refuses_nothing(wrapper: Path, tmp_path: Path) -> None:
    """A wrapper copied away from its skill classes stands down instead of guessing."""
    lone = tmp_path / "lone" / "eawf-hook.sh"
    lone.parent.mkdir()
    lone.write_bytes(wrapper.read_bytes())
    repo = _scratch_repo(tmp_path / "scratch")
    result = _run(
        lone,
        "user_prompt_submit",
        {"cwd": str(repo), "prompt": "/eawf:plan x"},
        cwd=repo,
        env=_env(tmp_path, _SYSTEM_PATH),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


# ---- CR-858 managed subfolder, worktree and the off switch -----------------------------------


def test_managed_subfolder_and_off_switch(wrapper: Path, tmp_path: Path) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "managed")
    subfolder = main / "src"
    subfolder.mkdir()
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _STUB_UV)
    _stub(stubs, "eawf", '#!/bin/bash\nprintf \'eawf %s\\n\' "$*" >>"$EAWF_TEST_LOG"\n')
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")

    for cwd in (subfolder, linked):
        result = _run(
            wrapper,
            "user_prompt_submit",
            {"cwd": str(cwd), "prompt": "/eawf:research x"},
            cwd=cwd,
            env=env,
        )
        assert (result.returncode, result.stdout) == (0, ""), cwd
    # Both answered for the main checkout's eawf, never stood down as unmanaged.
    assert [line for line in _calls(tmp_path) if line.startswith("eawf ")] == [
        f"eawf -w {main} hook run user_prompt_submit --runtime claude"
    ] * 2
    assert _health(main) == []

    # The off switch exits before the probe: a git that records its calls never runs.
    (tmp_path / "calls.log").unlink()
    _stub(stubs, "git", '#!/bin/bash\nprintf \'git %s\\n\' "$*" >>"$EAWF_TEST_LOG"\nexit 1\n')
    off = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}", EAWF_HOOKS="off")
    for event in _EVENTS:
        result = _run(wrapper, event, {"cwd": str(main), "prompt": "/eawf:plan"}, cwd=main, env=off)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), event
    assert _calls(tmp_path) == []


# ---- CR-859 runner missing and child failure -------------------------------------------------


def test_a_wrapper_row_carries_the_session_and_is_warned_about(
    wrapper: Path, tmp_path: Path
) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "managed")
    payload = {"cwd": str(main), "session_id": "host-7", "tool_name": "Bash"}

    result = _run(wrapper, "post_tool_use", payload, cwd=main, env=_env(tmp_path, _SYSTEM_PATH))

    assert (result.returncode, result.stdout) == (0, "")
    rows = read_health_rows(main / ".ea").rows
    assert [(row.hook, row.code, row.session_id) for row in rows] == [
        ("wrapper", "runner_missing", "host-7")
    ]
    assert take_failure_warning(main / ".ea") == (
        "eawf: 1 hook failures since the last session (top: runner_missing x1); "
        "run eawf doctor hooks"
    )


def test_runner_missing_and_child_failure_pass(wrapper: Path, tmp_path: Path) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "managed")
    only_uv = tmp_path / "only-uv"
    _stub(only_uv, "uv", _STUB_UV)
    payload = {"cwd": str(main), "session_id": "s", "tool_name": "Bash", "tool_input": {}}

    for path in (_SYSTEM_PATH, f"{_SYSTEM_PATH}:{only_uv}"):
        env = _env(tmp_path, path)
        for event in _EVENTS:
            before = len(_health(main))
            result = _run(wrapper, event, payload, cwd=main, env=env)
            assert (result.returncode, result.stdout) == (0, ""), (path, event)
            rows = _health(main)[before:]
            assert [(r["code"], r["event"]) for r in rows] == [("runner_missing", event)]
            assert rows[0]["error"] in {"uv", "eawf"}
    assert [r["error"] for r in _health(main)[len(_EVENTS) :]] == ["eawf"] * len(_EVENTS)

    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _STUB_UV)
    _stub(
        stubs,
        "eawf",
        '#!/bin/bash\ncat >/dev/null\nprintf \'{"decision":"block","reason":"x"}\'\n'
        "echo 'Traceback: boom' >&2\nexit 3\n",
    )
    before = len(_health(main))
    result = _run(
        wrapper, "pre_tool_use", payload, cwd=main, env=_env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")
    )
    assert (result.returncode, result.stdout) == (0, "")
    rows = _health(main)[before:]
    assert [(r["code"], r["error"], r["detail"], r["hook"]) for r in rows] == [
        ("hook_exit", "exit 3", "Traceback: boom", "wrapper")
    ]
    assert sorted(rows[0]) == ["at", "code", "detail", "error", "event", "hook", "session_id"]
    assert rows[0]["session_id"] == "s"


def test_child_answer_passes_through_when_it_exits_zero(wrapper: Path, tmp_path: Path) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "managed")
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _STUB_UV)
    deny = '{"hookSpecificOutput":{"permissionDecision":"deny"}}'
    _stub(stubs, "eawf", f"#!/bin/bash\ncat >/dev/null\necho 'note' >&2\nprintf '%s\\n' '{deny}'\n")
    result = _run(
        wrapper,
        "pre_tool_use",
        {"cwd": str(main)},
        cwd=main,
        env=_env(tmp_path, f"{stubs}:{_SYSTEM_PATH}"),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, f"{deny}\n", "note\n")
    assert _health(main) == []


def test_project_venv_eawf_runs_without_sync(wrapper: Path, tmp_path: Path) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "managed")
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", '#!/bin/bash\nprintf \'%s\\n\' "$*" >>"$EAWF_TEST_LOG"\n')
    _stub(main / ".venv" / "bin", "eawf", "#!/bin/bash\n")
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")
    _run(wrapper, "session_end", {"cwd": str(linked)}, cwd=linked, env=env)
    _stub(linked / ".venv" / "bin", "eawf", "#!/bin/bash\n")
    _run(wrapper, "session_end", {"cwd": str(linked)}, cwd=linked, env=env)
    assert _calls(tmp_path) == [
        f"run --project {main} --no-sync eawf -w {main} hook run session_end --runtime claude",
        f"run --project {linked} --no-sync eawf -w {main} hook run session_end --runtime claude",
    ]


def test_a_cwd_with_escaped_slashes_names_its_managed_tree(wrapper: Path, tmp_path: Path) -> None:
    # A JSON encoder may write "/" as "\/"; bash 3.2, the system bash on macOS,
    # keeps the backslash of a "\/" replacement inside double quotes.
    main, _linked = repo_with_linked_worktree(tmp_path / "managed")
    plain = tmp_path / "plain"
    plain.mkdir()
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", '#!/bin/bash\nprintf \'%s\\n\' "$*" >>"$EAWF_TEST_LOG"\n')
    _stub(stubs, "eawf", "#!/bin/bash\n")
    escaped = json.dumps(str(main)).replace("/", "\\/")
    result = _run(
        wrapper,
        "session_end",
        f'{{"session_id": "s", "cwd": {escaped}}}',
        cwd=plain,
        env=_env(tmp_path, f"{stubs}:{_SYSTEM_PATH}"),
    )
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [
        f"run --no-project eawf -w {main} hook run session_end --runtime claude"
    ]


@pytest.mark.parametrize("payload", ["", "{not json", '{"cwd": 3}'])
def test_unreadable_payload_falls_back_to_the_working_directory(
    wrapper: Path, tmp_path: Path, payload: str
) -> None:
    repo = _scratch_repo(tmp_path / "scratch")
    result = _run(wrapper, "pre_tool_use", payload, cwd=repo, env=_env(tmp_path, _SYSTEM_PATH))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("event", ["", "Pre-Tool", "pre_tool_use;id"])
def test_a_missing_or_odd_event_argument_exits_zero(
    wrapper: Path, tmp_path: Path, event: str
) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "managed")
    result = _run(wrapper, event, {"cwd": str(main)}, cwd=main, env=_env(tmp_path, _SYSTEM_PATH))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert _health(main) == []


# ---- CR-860 stand-down budget, hidden flag, fence gone ----------------------------------------


def _tree_listing(*roots: Path) -> set[Path]:
    return {path for root in roots for path in root.rglob("*")}


#: A script that does nothing, timed beside the wrapper as the baseline of one bash start.
_EMPTY_SHELL = Path("/dev/null")


def stand_down_p99(script: Path, cwd: Path, env: dict[str, str], *, runs: int = 20) -> float:
    """Return the nearest-rank p99, in seconds, of *runs* stand-downs of *script* in *cwd*.

    Every run must exit 0 and print nothing, as a stand-down does.
    """
    samples: list[float] = []
    for index in range(runs):
        event = _EVENTS[index % len(_EVENTS)]
        payload = {"cwd": str(cwd), "prompt": "/eawf:research x", "tool_name": "Bash"}
        started = time.perf_counter()
        result = _run(script, event, payload, cwd=cwd, env=env)
        samples.append(time.perf_counter() - started)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", ""), event
    return sorted(samples)[math.ceil(0.99 * runs) - 1]


def test_unmanaged_stand_down_budget(wrapper: Path, tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path / "scratch")
    plain = tmp_path / "plain"
    plain.mkdir()
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _STUB_UV)
    _stub(stubs, "eawf", '#!/bin/bash\nprintf \'eawf %s\\n\' "$*" >>"$EAWF_TEST_LOG"\n')
    env = _env(tmp_path, f"{stubs}:{os.environ['PATH']}")
    watched = (repo, plain, Path(env["XDG_RUNTIME_DIR"]), Path(env["HOME"]))
    before = _tree_listing(*watched)

    for cwd in (repo, plain):
        stand_down = stand_down_p99(wrapper, cwd, env)
        empty_shell = stand_down_p99(_EMPTY_SHELL, cwd, env)
        # Load slows both alike; an interpreter start would cost tens of empty shells.
        if sys.platform.startswith("linux"):
            assert stand_down < 10 * empty_shell, (stand_down, empty_shell)

    assert _tree_listing(*watched) == before
    assert _calls(tmp_path) == []

    # An older wrapper still passes --target-epoch; it is accepted, ignored and hidden.
    accepted = CliRunner().invoke(
        app,
        ["-w", str(plain), "hook", "run", "pre_commit", "--target-epoch", "1"],
        input="{}",
    )
    assert accepted.exit_code == 0, accepted.output
    help_text = CliRunner().invoke(app, ["hook", "run", "--help"]).output
    assert "--target-epoch" not in help_text
    assert not hasattr(hook_command, "_refused_stale_bundle")
    assert not hasattr(hook_command, "_stood_down_for_epoch")
