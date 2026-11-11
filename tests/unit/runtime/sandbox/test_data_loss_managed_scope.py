"""The data-loss guard judges only in a repository eawf manages.

Each case hands the pre-tool guard the payload Claude Code sends from a throwaway
repository and reads back what the host would read on stdout. The mutations named in
the payloads are judged and never executed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.daemon.runtime_dir import TREES_DIRNAME, runtime_base_dir
from eawf.runtime.hooks.context import managed_tree, resolve_hook_context
from eawf.runtime.hooks.data_loss_guard import (
    DATA_LOSS_GUARD_HOOK,
    DENIAL_UNRECORDED,
    guard_pre_tool_use,
)
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.health import HEALTH_RECORD
from eawf.runtime.sandbox.data_loss import judge_tool_call
from eawf.surfaces.cli import _daemon_client
from eawf.surfaces.cli.commands import hook
from tests._linked_worktree import git

SESSION: Final = "0b6c1f2e-6a43-4d55-9c1e-2f7a9d1c3b4e"
ADD_ADVICE: Final = "git worktree add .ea/worktrees/<name> -b <branch>"


class _Down:
    """A daemon client factory whose daemon is down, counting every attempt."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise ConnectionRefusedError("daemon unreachable")


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated runtime base, HOME and working directory; the runtime dir follows XDG."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime = tmp_path / "xdg"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    for name in ("EAWF_RUNTIME_DIR", "EA_STATE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return runtime


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> _Down:
    factory = _Down()
    monkeypatch.setattr("eawf.runtime.hooks.runner._default_daemon_client_factory", factory)
    return factory


def _repo(root: Path, *, kind: str) -> Path:
    """Return a ``managed``, ``unmanaged-git`` or ``no-git`` directory at *root*."""
    root.mkdir(parents=True)
    root = root.resolve()
    if kind == "no-git":
        return root
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "root")
    if kind == "managed":
        (root / ".ea").mkdir()
        (root / ".ea" / "state.json").write_text("{}\n")
    return root


def _payload(cwd: Path, tool: str, tool_input: object) -> str:
    return json.dumps(
        {
            "session_id": SESSION,
            "hook_event_name": "PreToolUse",
            "cwd": str(cwd),
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_01ScopeA1b2C3d4E5f6G7h8",
        }
    )


def _guard(capsys: pytest.CaptureFixture[str], top: Path, stdin: str) -> tuple[bool, str]:
    """Run the CLI's pre-tool guard step from *top*; return whether it answered and its stdout."""
    context = resolve_hook_context(top)
    answered = hook._guarded_pre_tool_use(
        "pre_tool_use", "claude", stdin, context, datetime.now(UTC)
    )
    return answered, capsys.readouterr().out


def _reason(capsys: pytest.CaptureFixture[str], top: Path, stdin: str) -> str:
    answered, out = _guard(capsys, top, stdin)
    assert answered
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    return str(decision["permissionDecisionReason"])


def _event(payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        scope_id="",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={HookEventType.PRE_TOOL_USE.value: payload},
    )


def _tree_entries(runtime: Path) -> list[Path]:
    # The platform's own base: macOS keeps it under the home, not *runtime*.
    trees = runtime_base_dir() / TREES_DIRNAME
    assert trees.is_relative_to(runtime.parent)
    return sorted(trees.iterdir()) if trees.is_dir() else []


def _health_rows(repo: Path) -> list[dict[str, Any]]:
    record = repo / ".ea" / HEALTH_RECORD
    if not record.is_file():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("kind", ["unmanaged-git", "no-git"])
@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("EnterWorktree", {"name": "x"}),
        ("Bash", {"command": "git worktree add ../x"}),
        ("Bash", {"command": "rm -rf .ea"}),
    ],
    ids=["enter", "add-out", "rm-ea"],
)
def test_guard_off_outside_managed(
    tmp_path: Path,
    xdg: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    tool: str,
    tool_input: object,
) -> None:
    repo = _repo(tmp_path / "repo", kind=kind)
    answered, out = _guard(capsys, repo, _payload(repo, tool, tool_input))
    assert (answered, out) == (False, "")
    assert _tree_entries(xdg) == []
    assert not (repo / ".ea").exists()


@pytest.mark.parametrize(
    ("tool", "tool_input", "rule"),
    [
        ("Bash", {"command": "rm -rf .ea/worktrees/w1"}, "foreign_worktree_mutation"),
        ("Bash", {"command": "git worktree add ../x"}, "out_of_root_worktree"),
        ("Edit", {"file_path": ".ea/state.json"}, "canonical_store_edit"),
    ],
)
def test_guard_off_outside_managed_keeps_the_patterns_inside(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    tool_input: object,
    rule: str,
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    assert f"({rule})" in _reason(capsys, repo, _payload(repo, tool, tool_input))
    assert _tree_entries(xdg) == []


def test_guard_off_outside_managed_keeps_the_drifted_commit_inside(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    worktree = repo / ".ea" / "worktrees" / "w1"
    git(repo, "worktree", "add", "-q", str(worktree), "-b", "w1")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    stdin = _payload(worktree, "Bash", {"command": "git commit -m wip"})
    assert "(drifted_commit)" in _reason(capsys, worktree, stdin)


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("EnterWorktree", {"name": "x"}),
        ("Agent", {"prompt": "p", "isolation": "worktree"}),
        ("Task", {"prompt": "p", "isolation": "worktree"}),
        ("Bash", {"command": "git worktree add ../x"}),
    ],
    ids=["enter", "agent", "task", "add-out"],
)
def test_managed_worktree_denials(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    tool_input: object,
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    reason = _reason(capsys, repo, _payload(repo, tool, tool_input))
    assert "(out_of_root_worktree)" in reason
    assert ADD_ADVICE in reason


@pytest.mark.parametrize(
    "command",
    ["git worktree add .ea/worktrees/x", "git worktree add .ea/worktrees/x -b x"],
)
def test_managed_worktree_denials_admit_an_add_under_the_root(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    answered, out = _guard(capsys, repo, _payload(repo, "Bash", {"command": command}))
    assert (answered, out) == (False, "")
    assert down.calls == 0


@pytest.mark.parametrize(
    "command",
    ["git worktree remove .ea/worktrees/x", "git worktree move .ea/worktrees/x ../y"],
    ids=["remove", "move"],
)
def test_remove_refusal_names_gc(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    reason = _reason(capsys, repo, _payload(repo, "Bash", {"command": command}))
    assert "(foreign_worktree_mutation)" in reason
    assert "eawf worktree gc --dry-run" in reason
    assert "prints the exact git worktree remove line per landed, clean tree" in reason
    assert "for the operator" in reason
    assert "once its work has landed" not in reason


def test_remove_refusal_names_gc_for_an_rm_too(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    stdin = _payload(repo, "Bash", {"command": "rm -rf .ea/worktrees/x"})
    assert "eawf worktree gc --dry-run" in _reason(capsys, repo, stdin)


def test_denial_record_no_spawn(
    tmp_path: Path,
    xdg: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[dict[str, Any]] = []
    spawned: list[Path] = []

    class _Recording(_daemon_client.DaemonClient):
        def __init__(self, **kwargs: Any) -> None:
            opened.append(kwargs)
            super().__init__(**kwargs)

    def _no_spawn(runtime_dir: Path) -> int:
        spawned.append(runtime_dir)
        raise AssertionError("a hook must not start a daemon")

    monkeypatch.setattr(_daemon_client, "DaemonClient", _Recording)
    monkeypatch.setattr(_daemon_client, "auto_spawn_daemon", _no_spawn)
    repo = _repo(tmp_path / "repo", kind="managed")

    reason = _reason(capsys, repo, _payload(repo, "Bash", {"command": "rm -rf .ea"}))

    assert "(canonical_store_edit)" in reason
    assert len(opened) == 1
    assert opened[0]["spawn"] is False
    assert opened[0]["call_timeout_seconds"] == pytest.approx(1.5)
    assert spawned == []
    rows = _health_rows(repo)
    assert [(row["hook"], row["code"]) for row in rows] == [
        (DATA_LOSS_GUARD_HOOK, DENIAL_UNRECORDED)
    ]
    assert rows[0]["error"] == "DaemonNotRunningError"


@pytest.mark.parametrize("stdin", ["not json", "[1, 2]", "{"])
def test_denial_record_no_spawn_passes_an_unparsed_payload(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    answered, out = _guard(capsys, repo, stdin)
    assert (answered, out) == (True, "")
    assert down.calls == 0
    assert _health_rows(repo) == []


@pytest.mark.parametrize("payload", [{}, {"tool_name": ""}, {"tool_name": 7}])
def test_denial_record_no_spawn_passes_a_payload_naming_no_tool(
    tmp_path: Path, xdg: Path, down: _Down, payload: dict[str, Any]
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    event = _event({"session_id": SESSION, "cwd": str(repo), **payload})
    assert guard_pre_tool_use(event, context=resolve_hook_context(repo)) is None
    assert down.calls == 0


def test_denial_record_no_spawn_keeps_unjudged_for_a_parsed_call(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    reason = _reason(capsys, repo, _payload(repo, "Bash", {"command": "cat <"}))
    assert "(unjudged)" in reason
    assert down.calls == 1


def test_denial_record_no_spawn_skips_the_record_without_a_session(
    tmp_path: Path, xdg: Path, down: _Down
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    event = _event({"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "rm -rf .ea"}})
    denial = guard_pre_tool_use(event, context=resolve_hook_context(repo))
    assert denial is not None and denial.rule == "canonical_store_edit"
    assert down.calls == 0
    assert _health_rows(repo) == []


# ---- the guard follows the session, never the paths a call names ---------------


@pytest.mark.parametrize("kind", ["unmanaged-git", "no-git"])
@pytest.mark.parametrize(
    "command",
    ["rm -rf {managed}/.ea", "rm -rf ../managed/.ea", "cd /; rm -rf '{managed}/.ea/state.json'"],
    ids=["absolute", "relative", "quoted"],
)
def test_guard_off_for_a_managed_path_named_from_outside(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    command: str,
) -> None:
    # The accepted cost: a session that is not in a managed tree is not judged.
    managed = _repo(tmp_path / "managed", kind="managed")
    here = _repo(tmp_path / "here", kind=kind)
    stdin = _payload(here, "Bash", {"command": command.format(managed=managed)})
    assert _guard(capsys, here, stdin) == (False, "")
    assert down.calls == 0
    assert _health_rows(managed) == []
    assert not (here / ".ea").exists()


def test_guard_on_for_a_managed_project_dir(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _repo(tmp_path / "managed", kind="managed")
    here = _repo(tmp_path / "here", kind="unmanaged-git")
    stdin = _payload(here, "EnterWorktree", {"name": "x"})
    answered, out = _guard(capsys, here, stdin)
    assert (answered, out) == (False, "")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(managed))
    assert "(out_of_root_worktree)" in _reason(capsys, here, stdin)
    # Only Claude names a project directory; another runtime's call stays off.
    event = _event(json.loads(stdin))
    codex = event.model_copy(update={"runtime": "codex"})
    assert guard_pre_tool_use(codex, context=resolve_hook_context(here)) is None


def test_guard_on_through_a_symlink_and_a_linked_worktree(
    tmp_path: Path, xdg: Path, down: _Down, capsys: pytest.CaptureFixture[str]
) -> None:
    managed = _repo(tmp_path / "managed", kind="managed")
    linked = tmp_path / "linked"
    git(managed, "worktree", "add", "-q", str(linked), "-b", "side")
    here = _repo(tmp_path / "here", kind="no-git")
    link = here / "link"
    link.symlink_to(managed)
    # A cwd reached through a symlink is the tree it points into.
    by_link = _payload(link, "Bash", {"command": "rm -rf .ea"})
    assert _guard(capsys, link, _payload(link, "Bash", {"command": "ls"})) == (False, "")
    assert "(canonical_store_edit)" in _reason(capsys, link, by_link)
    # A linked worktree answers for its main checkout, managed or not its own .ea.
    from_linked = _payload(linked, "Bash", {"command": "rm -rf .ea"})
    assert "(canonical_store_edit)" in _reason(capsys, linked, from_linked)
    into_managed = _payload(linked, "Write", {"file_path": f"{managed}/.ea/state.json"})
    assert "(canonical_store_edit)" in _reason(capsys, linked, into_managed)


def test_guard_stays_off_when_nothing_reached_is_managed(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _repo(tmp_path / "other", kind="unmanaged-git")
    here = _repo(tmp_path / "here", kind="no-git")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    stdin = _payload(here, "Bash", {"command": f"rm -rf {other}/.ea ~/.ea /nowhere/.ea"})
    answered, out = _guard(capsys, here, stdin)
    assert (answered, out) == (False, "")
    assert down.calls == 0


def _state(root: Path) -> None:
    (root / ".ea").mkdir()
    (root / ".ea" / "state.json").write_text("{}\n")


def _committed(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "root")


def _git_managed(repo: Path) -> Path:
    _committed(repo)
    _state(repo)
    return repo / "sub"


def _git_bare_ea(repo: Path) -> Path:
    _committed(repo)
    (repo / ".ea").mkdir()
    return repo / "sub"


def _git_unmanaged(repo: Path) -> Path:
    _committed(repo)
    return repo / "sub"


def _nogit_state(repo: Path) -> Path:
    _state(repo)
    return repo / "sub"


def _nogit_bare_ea(repo: Path) -> Path:
    (repo / ".ea").mkdir()
    return repo / "sub"


def _git_inside_holder(repo: Path) -> Path:
    _state(repo)
    inner = repo / "sub"
    git(inner, "init", "-q")
    return inner


def _linked_worktree(repo: Path) -> Path:
    _git_managed(repo)
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "-b", "side")
    return linked


def _missing_under_managed(repo: Path) -> Path:
    _git_managed(repo)
    return repo / "gone" / "deeper"


@pytest.mark.parametrize(
    ("build", "managed"),
    [
        (_git_managed, True),
        (_git_bare_ea, True),
        (_git_unmanaged, False),
        (_nogit_state, True),
        (_nogit_bare_ea, False),
        # An unmanaged clone inside a tree outside git is passed on the way up.
        (_git_inside_holder, True),
        (_linked_worktree, True),
        (_missing_under_managed, True),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else str(value),
)
def test_managed_tree_definition(
    tmp_path: Path, build: Callable[[Path], Path], managed: bool
) -> None:
    repo = (tmp_path / "repo").resolve()
    (repo / "sub").mkdir(parents=True)
    assert managed_tree(build(repo)) == (repo if managed else None)


def test_managed_tree_at_the_filesystem_root() -> None:
    root = Path("/")
    assert managed_tree(root) == (root if (root / ".ea" / "state.json").is_file() else None)


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": "ls {managed}/README.md"}),
        ("Bash", {"command": "rm -rf {here}/.ea"}),
        ("Bash", {"command": "cd {parent} && rm -rf here"}),
        ("Bash", {"command": "rm -rf $UNSET_IN_TEST/.ea"}),
        ("Bash", {"command": "git worktree add ../x"}),
        ("EnterWorktree", {"name": "x"}),
        ("Agent", {"prompt": "p", "isolation": "worktree"}),
    ],
    ids=["ls", "unmanaged-ea", "unmanaged-repo", "unset-variable", "add-out", "enter", "agent"],
)
def test_guard_reach_stays_off_for_unmanaged_targets(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    tool: str,
    tool_input: dict[str, str],
) -> None:
    parent = tmp_path / "p"
    managed = _repo(parent / "managed", kind="managed")
    here = _repo(parent / "here", kind="unmanaged-git")
    filled = json.loads(
        json.dumps(tool_input)
        .replace("{managed}", str(managed))
        .replace("{here}", str(here))
        .replace("{parent}", str(parent.resolve()))
    )
    answered, out = _guard(capsys, here, _payload(here, tool, filled))
    assert (answered, out) == (False, "")
    assert down.calls == 0


def test_guard_on_for_every_call_of_a_managed_project(
    tmp_path: Path,
    xdg: Path,
    down: _Down,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = _repo(tmp_path / "managed", kind="managed")
    here = _repo(tmp_path / "here", kind="unmanaged-git")
    stdin = _payload(here, "Bash", {"command": "rm -rf $UNSET_IN_TEST/.ea"})
    assert _guard(capsys, here, stdin) == (False, "")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(managed))
    assert "(canonical_store_edit)" in _reason(capsys, here, stdin)
    resolved = _payload(here, "Bash", {"command": f"rm -rf {here}/.ea"})
    assert "(canonical_store_edit)" in _reason(capsys, here, resolved)
    # The record lands in the managed tree the session is in.
    assert [r["code"] for r in _health_rows(managed)] == [DENIAL_UNRECORDED] * 2
    assert not (here / ".ea").exists()


# The judge reads these as the shell does; the previous release missed each
# destructive one inside a managed repository.
@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("rm -rf repo/.e\\\na", "canonical_store_edit"),
        ("env -u FOO rm -rf repo/.ea", "canonical_store_edit"),
        ("env --chdir=repo rm -rf .ea", "canonical_store_edit"),
        ("env -Crepo rm -rf .ea", "canonical_store_edit"),
        ("env -C repo git clean -fdx", "canonical_store_edit"),
        ("D=repo; rm -rf $D", "foreign_worktree_mutation"),
        ("export D=repo; rm -rf ${D}", "foreign_worktree_mutation"),
        ("rm -rf $EAWF_TEST_REPO", "foreign_worktree_mutation"),
        ("D=repo > repo/.ea/state.json", "canonical_store_edit"),
        ("env -C repo true && git clean -fdx", None),
        ("echo .e\\\\\na", None),
        ("D=; rm -rf $D", None),
        ("rm -rf $UNSET_IN_TEST", None),
        ("env -C", None),
    ],
    ids=[
        "continuation",
        "env-unset",
        "env-chdir-equals",
        "env-chdir-joined",
        "env-chdir-git",
        "line-variable",
        "exported-braced",
        "environment-variable",
        "assignment-redirect",
        "env-chdir-scoped",
        "escaped-backslash-newline",
        "empty-variable",
        "unset-variable",
        "env-chdir-missing",
    ],
)
def test_judge_reads_paths_as_the_shell_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, rule: str | None
) -> None:
    repo = _repo(tmp_path / "repo", kind="managed")
    monkeypatch.setenv("EAWF_TEST_REPO", str(repo))
    monkeypatch.delenv("UNSET_IN_TEST", raising=False)
    denial = judge_tool_call("Bash", {"command": command}, cwd=tmp_path, anchor=tmp_path)
    assert (None if denial is None else denial.rule) == rule
