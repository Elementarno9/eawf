"""The packaged wrapper keeps the data-loss guard on for a session in a managed tree.

A session that changed into an unmanaged directory keeps the guard through
the host's project directory; a session outside every managed tree is not
judged, whatever its calls name. The wrapper passes a call exactly when the
guard would judge it. Health rows the wrapper writes stay valid JSON lines and
the record stays bounded.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.hooks import data_loss_guard
from eawf.runtime.hooks.context import resolve_hook_context
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.health import (
    HEALTH_CURSOR,
    HEALTH_RECORD,
    HEALTH_RECORD_CAP,
    HEALTH_ROTATED,
    read_cursor,
    read_health_rows,
)
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.runtime.vcs.branch_name import BareRepositoryError, main_checkout, repository_home
from tests._linked_worktree import git
from tests.integration.runtime.hooks.test_hook_wrapper_fail_open import (
    _SYSTEM_PATH,
    _env,
    _health,
    _run,
    _stub,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed"),
]

#: Records each eawf call, then ends without an answer.
_LOGGING_EAWF = '#!/bin/bash\ncat >/dev/null\nprintf \'eawf %s\\n\' "$*" >>"$EAWF_TEST_LOG"\n'
_LOGGING_UV = """#!/bin/bash
while [ $# -gt 0 ] && [ "$1" != "eawf" ]; do shift; done
exec "$@"
"""

#: Variables of the session running these tests that would steer the hook.
_HOST_ENV = (
    "EA_STATE",
    "EAWF_RUNTIME_DIR",
    "EAWF_HOOKS",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
)


@pytest.fixture(scope="module")
def wrapper(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("plugin") / "eawf"
    package_plugin(target, include_marketplace=False, include_readme=False, extra_tools={})
    return target / "hooks" / "eawf-hook.sh"


def _managed(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "root")
    (root / ".ea").mkdir()
    (root / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    return root.resolve()


def _unmanaged(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q")
    return root.resolve()


def _payload(cwd: Path, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": "host-1",
        "hook_event_name": "PreToolUse",
        "cwd": str(cwd),
        "tool_name": tool,
        "tool_input": tool_input,
    }


def _real_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """The test session's environment, so the wrapper runs this checkout's eawf."""
    env = {k: v for k, v in os.environ.items() if k not in _HOST_ENV}
    runtime = tmp_path / "xdg"
    runtime.mkdir(exist_ok=True)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["TMPDIR"] = str(tmp_path)
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    return {**env, **extra}


def _deny_reason(stdout: str) -> str:
    decision = json.loads(stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return str(decision["permissionDecisionReason"])


# ---- the reproduction, real wrapper and real eawf ---------------------------------------------


def test_cd_into_an_unmanaged_directory_keeps_the_guard_on(wrapper: Path, tmp_path: Path) -> None:
    managed = _managed(tmp_path / "managed")
    elsewhere = _unmanaged(tmp_path / "elsewhere")
    env = _real_env(tmp_path)

    call = _payload(managed / ".ea", "Bash", {"command": f"rm -rf {managed}/.ea"})
    result = _run(wrapper, "pre_tool_use", call, cwd=managed / ".ea", env=env)
    assert "(canonical_store_edit)" in _deny_reason(result.stdout)
    # A session outside every managed tree is not judged, whatever it names.
    call = _payload(elsewhere, "Bash", {"command": f"rm -rf {managed}/.ea"})
    result = _run(wrapper, "pre_tool_use", call, cwd=elsewhere, env=env)
    assert (result.returncode, result.stdout) == (0, "")

    # A session whose project is managed keeps the whole guard.
    project = _real_env(tmp_path, CLAUDE_PROJECT_DIR=str(managed))
    result = _run(wrapper, "pre_tool_use", call, cwd=elsewhere, env=project)
    assert "(canonical_store_edit)" in _deny_reason(result.stdout)
    call = _payload(elsewhere, "EnterWorktree", {"name": "x"})
    result = _run(wrapper, "pre_tool_use", call, cwd=elsewhere, env=project)
    assert "(out_of_root_worktree)" in _deny_reason(result.stdout)

    # Inside the managed repository the guard denies as it always did.
    call = _payload(managed, "Agent", {"prompt": "p", "isolation": "worktree"})
    result = _run(wrapper, "pre_tool_use", call, cwd=managed, env=env)
    assert "(out_of_root_worktree)" in _deny_reason(result.stdout)


def test_outside_every_managed_repository_the_guard_stays_off(
    wrapper: Path, tmp_path: Path
) -> None:
    other = _unmanaged(tmp_path / "other")
    elsewhere = _unmanaged(tmp_path / "elsewhere")
    env = _real_env(tmp_path, CLAUDE_PROJECT_DIR=str(other))
    call = _payload(elsewhere, "Bash", {"command": f"rm -rf {other}/.ea; git worktree add ../x"})
    result = _run(wrapper, "pre_tool_use", call, cwd=elsewhere, env=env)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


# ---- the wrapper passes a call exactly when the session is in a managed tree ------------------


@pytest.mark.parametrize("project", [False, True], ids=["off", "project-managed"])
@pytest.mark.parametrize(
    ("tool", "tool_input", "denies"),
    [
        ("Bash", {"command": "rm -rf {managed}/.ea"}, True),
        ("Bash", {"command": "rm -rf .ea"}, True),
        ("Bash", {"command": "cd {here} && rm -rf 'link/.ea'"}, True),
        ("Bash", {"command": "git -C ../managed clean -fdx"}, True),
        ("Bash", {"command": "ls ../managed"}, False),
        ("Write", {"file_path": "{managed}/.ea/state.json", "content": "x"}, True),
        ("Edit", {"file_path": "{linked}/.ea/state.json"}, True),
        ("EnterWorktree", {"name": "x"}, True),
        ("Agent", {"prompt": "p", "isolation": "worktree"}, True),
        ("Agent", {"prompt": "p"}, False),
        ("exec_command", {"cmd": ["rm", "-rf", "{managed}/.ea"]}, True),
        ("apply_patch", {"input": "*** Delete File: {managed}/.ea/state.json\n"}, True),
        ("Read", {"file_path": "{managed}/.ea/state.json"}, False),
    ],
)
def test_wrapper_passes_every_call_the_guard_denies(
    wrapper: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    tool_input: dict[str, Any],
    project: bool,
    denies: bool,
) -> None:
    # Off, the session is not in a managed tree: nothing passes and nothing is
    # denied, whatever the call names.
    passes = project
    denies = denies and project
    managed = _managed(tmp_path / "managed")
    linked = tmp_path / "linked"
    git(managed, "worktree", "add", "-q", str(linked), "-b", "side")
    here = _unmanaged(tmp_path / "here")
    (here / "link").symlink_to(managed)
    filled = json.loads(
        json.dumps(tool_input)
        .replace("{managed}", str(managed))
        .replace("{here}", str(here))
        .replace("{linked}", str(linked.resolve()))
    )
    payload = _payload(here, tool, filled)
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _LOGGING_UV)
    _stub(stubs, "eawf", _LOGGING_EAWF)
    extra = {"CLAUDE_PROJECT_DIR": str(managed)} if project else {}
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}", **extra)

    result = _run(wrapper, "pre_tool_use", payload, cwd=here, env=env)

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    log = tmp_path / "calls.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    # A passed call runs eawf without -w, so only the guard acts.
    assert calls == (["eawf hook run pre_tool_use --runtime claude"] if passes else [])

    monkeypatch.setenv("HOME", env["HOME"])
    if project:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(managed))
    else:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    event = HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        scope_id="",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={HookEventType.PRE_TOOL_USE.value: payload},
    )
    denial = data_loss_guard.guard_pre_tool_use(
        event, context=resolve_hook_context(here), daemon_client_factory=_unreachable
    )
    assert (denial is not None) == denies
    # The wrapper stands down only calls the guard would allow.
    assert passes or not denies


def _unreachable() -> Any:
    raise ConnectionRefusedError("daemon unreachable")


def test_a_passed_call_never_creates_an_ea_in_an_unmanaged_repository(
    wrapper: Path, tmp_path: Path
) -> None:
    managed = _managed(tmp_path / "managed")
    here = _unmanaged(tmp_path / "here")
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _LOGGING_UV)
    _stub(stubs, "eawf", "#!/bin/bash\ncat >/dev/null\necho broken >&2\nexit 3\n")
    payload = _payload(here, "Bash", {"command": "rm -rf .ea"})

    result = _run(
        wrapper, "pre_tool_use", payload, cwd=here, env=_env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")
    )
    assert (result.returncode, result.stdout) == (0, "")
    assert not (here / ".ea").exists()

    # A session started in a managed repository files the failure there.
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}", CLAUDE_PROJECT_DIR=str(managed))
    result = _run(wrapper, "pre_tool_use", payload, cwd=here, env=env)
    assert (result.returncode, result.stdout) == (0, "")
    assert not (here / ".ea").exists()
    assert [r["code"] for r in _health(managed)] == ["hook_exit"]


# ---- a separate git directory -----------------------------------------------------------------


def test_a_separate_git_dir_checkout_is_managed_on_both_sides(
    wrapper: Path, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q", f"--separate-git-dir={tmp_path / 'store.git'}")
    (checkout / ".ea").mkdir()
    (checkout / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    (checkout / "src").mkdir()
    checkout = checkout.resolve()
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _LOGGING_UV)
    _stub(stubs, "eawf", _LOGGING_EAWF)
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")

    payload = {"cwd": str(checkout / "src"), "prompt": "hello"}
    result = _run(wrapper, "user_prompt_submit", payload, cwd=checkout / "src", env=env)

    assert result.returncode == 0
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert calls == [f"eawf -w {checkout} hook run user_prompt_submit --runtime claude"]
    assert main_checkout(checkout / "src") == checkout
    assert resolve_hook_context(checkout / "src").main == checkout


@pytest.mark.parametrize("layout", ["separate", "separate-with-worktree", "bare"])
def test_separate_git_dir_repro_linked_tree_disagreement(
    wrapper: Path, tmp_path: Path, layout: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    if layout == "bare":
        seed = _unmanaged(tmp_path / "seed")
        git(seed, "commit", "-q", "--allow-empty", "-m", "root")
        git(tmp_path, "clone", "-q", "--bare", str(seed), str(tmp_path / "store.git"))
        source = tmp_path / "store.git"
    else:
        git(checkout, "init", "-q", f"--separate-git-dir={tmp_path / 'store.git'}")
        if layout == "separate-with-worktree":
            git(checkout, "config", "core.worktree", str(checkout))
        git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
        source = checkout
    linked = tmp_path / "linked"
    git(source, "worktree", "add", "-q", str(linked), "-b", "side")
    linked = linked.resolve()
    checkout = checkout.resolve()
    for tree in (checkout, linked):
        (tree / ".ea").mkdir(exist_ok=True)
        (tree / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _LOGGING_UV)
    _stub(stubs, "eawf", _LOGGING_EAWF)
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}")

    result = _run(wrapper, "user_prompt_submit", {"cwd": str(linked)}, cwd=linked, env=env)

    assert (result.returncode, result.stdout) == (0, "")
    log = tmp_path / "calls.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    context = resolve_hook_context(linked)
    try:
        found = repository_home(linked)
    except BareRepositoryError:
        found = None
    home = None if found is None else found.main_root
    if home is None:
        # No main checkout git can name: every side stands down, none crashes.
        assert layout != "separate-with-worktree"
        assert (calls, context.kind) == ([], "no-git")
    else:
        assert home == checkout
        assert (context.kind, context.main) == ("managed", checkout)
        assert calls == [f"eawf -w {checkout} hook run user_prompt_submit --runtime claude"]


# ---- health rows the wrapper writes -----------------------------------------------------------


@pytest.mark.parametrize(
    "stderr_line",
    [
        'a\x01b\x0cc "quoted" back\\slash\x1b[31m',
        "x" * 499 + "é" + "tail",
        "x" * 498 + "€" + "tail",
        "x" * 497 + "😀" + "tail",
        "\\" * 600,
        "plain",
    ],
    ids=[
        "controls",
        "two-byte-at-cut",
        "three-byte-at-cut",
        "four-byte-at-cut",
        "backslashes",
        "plain",
    ],
)
def test_a_child_failure_row_is_valid_json(wrapper: Path, tmp_path: Path, stderr_line: str) -> None:
    managed = _managed(tmp_path / "managed")
    stubs = tmp_path / "stubs"
    _stub(stubs, "uv", _LOGGING_UV)
    script = tmp_path / "line.txt"
    script.write_bytes(stderr_line.encode("utf-8"))
    _stub(stubs, "eawf", f"#!/bin/bash\ncat >/dev/null\ncat '{script}' >&2\nexit 3\n")
    env = _env(tmp_path, f"{stubs}:{_SYSTEM_PATH}", LANG="C.UTF-8")

    result = _run(wrapper, "post_tool_use", {"cwd": str(managed)}, cwd=managed, env=env)

    assert result.returncode == 0
    read = read_health_rows(managed / ".ea")
    assert read.malformed == 0
    (row,) = read.rows
    assert row.code == "hook_exit"
    expected = "".join(" " if ord(c) < 0x20 else c for c in stderr_line).encode("utf-8")[:500]
    assert row.detail == expected.decode("utf-8", errors="ignore")


def test_the_wrapper_rotates_a_full_record_and_moves_the_cursor(
    wrapper: Path, tmp_path: Path
) -> None:
    managed = _managed(tmp_path / "managed")
    ea = managed / ".ea"
    record = ea / HEALTH_RECORD
    record.parent.mkdir(parents=True)
    row = json.dumps(
        {"at": "t", "event": "e", "hook": "h", "code": "old", "error": "", "detail": ""}
    )
    older = (row + "\n").encode("utf-8") * 10
    (ea / HEALTH_ROTATED).write_bytes(older)
    full = (row.replace("old", "full") + "\n").encode("utf-8")
    record.write_bytes(full * (HEALTH_RECORD_CAP // len(full) + 1))
    cursor = len(older) + 3 * len(full)
    (ea / HEALTH_CURSOR).write_text(f"{cursor}\n", encoding="utf-8")

    result = _run(
        wrapper,
        "post_tool_use",
        {"cwd": str(managed)},
        cwd=managed,
        env=_env(tmp_path, _SYSTEM_PATH),
    )

    assert result.returncode == 0
    assert (ea / HEALTH_ROTATED).read_bytes().startswith(full)
    assert [r["code"] for r in _health(managed)] == ["runner_missing"]
    # The cursor still points just past the third "full" row.
    assert read_cursor(ea) == 3 * len(full)
    unread = read_health_rows(ea, offset=read_cursor(ea)).rows
    assert [r.code for r in unread][-1] == "runner_missing"
    assert {r.code for r in unread[:-1]} == {"full"}
