"""The four fail-closed data-loss patterns, judged per host tool call.

Every command here is judged, never run: the guard's decision is the subject, and no
case executes the mutation it names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from eawf.runtime.sandbox.data_loss import (
    JUDGED_TOOLS,
    RULE_VALUES,
    UNJUDGED_RULE,
    DataLossPattern,
    judge_tool_call,
)

FOREIGN = DataLossPattern.FOREIGN_WORKTREE_MUTATION
OUT_OF_ROOT = DataLossPattern.OUT_OF_ROOT_WORKTREE
DRIFT = DataLossPattern.DRIFTED_COMMIT
CANONICAL = DataLossPattern.CANONICAL_STORE_EDIT


@dataclass(frozen=True)
class _Tree:
    """A main checkout with one epoch-1 worktree and one leased workspace."""

    main: Path
    worktree: Path
    workspace: Path


@pytest.fixture
def tree(tmp_path: Path) -> _Tree:
    main = tmp_path / "repo"
    (main / ".git" / "worktrees").mkdir(parents=True)
    (main / ".ea" / "store").mkdir(parents=True)
    (main / "src").mkdir()
    worktree = main / ".ea" / "worktrees" / "w1"
    workspace = main / ".ea" / "local" / "epoch2" / "workspaces" / "wsh-1"
    for name, checkout in (("w1", worktree), ("wsh-1", workspace)):
        (checkout / "src").mkdir(parents=True)
        (main / ".git" / "worktrees" / name).mkdir()
        (checkout / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n")
    return _Tree(main=main, worktree=worktree, workspace=workspace)


def _bash(tree: _Tree, command: str, *, cwd: Path | None = None) -> DataLossPattern | None | str:
    denial = judge_tool_call("Bash", {"command": command}, cwd=cwd or tree.main, anchor=tree.main)
    return "allowed" if denial is None else denial.pattern


@pytest.mark.parametrize(
    "command",
    [
        "echo {} > .ea/state.json",
        "cat x >> './.ea/store/event.jsonl'",
        'jq . a.json | tee ".ea/state.json"',
        "sed -i '' 's/a/b/' .ea/state.json",
        "sed -i.bak -e 's/a/b/' .ea/store/decision.jsonl",
        "perl -pi -e 's/a/b/' .ea/state.json",
        "cp /tmp/x .ea/state.json",
        "cp -t .ea/store /tmp/x",
        "mv .ea/state.json /tmp/state.json",
        "rm -f .ea/store/*.jsonl",
        "rm -rf .ea",
        "truncate -s 0 .ea/store/event.jsonl",
        "dd if=/dev/zero of=.ea/state.json",
        "git checkout HEAD -- .ea/state.json",
        "git restore --source HEAD~1 .ea/state.json",
        "sqlite3 .ea/telemetry.db 'delete from runs'",
        """python3 -c "open('.ea/state.json', 'w').write('{}')" """,
        """uv run python -c "import json; json.dump({}, open('.ea/state.json', 'w'))" """,
        "bash -lc 'echo x > .ea/state.json'",
        "cd .ea && echo x > state.json",
        "true; echo x >.ea/ledger/run.jsonl",
        "x=$(echo 1 > .ea/state.json)",
        'echo "$(rm .ea/state.json)"',
        "echo `rm .ea/state.json`",
        "rm .ea/local/epoch2/leases/LSE-1.json",
        "rm .ea/worktrees/w1/.ea/state.json",
        "env FOO=1 rtk rm .ea/state.json",
        "ls\nrm .ea/state.json",
    ],
)
def test_surf_051_a_direct_write_to_a_canonical_file_is_denied(tree: _Tree, command: str) -> None:
    assert _bash(tree, command) is CANONICAL


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Edit", {"file_path": ".ea/state.json", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "{main}/.ea/store/event.jsonl", "content": ""}),
        ("MultiEdit", {"file_path": "src/../.ea/state.json", "edits": []}),
        ("NotebookEdit", {"notebook_path": ".ea/ledger/x.ipynb", "new_source": ""}),
        ("apply_patch", {"input": "*** Begin Patch\n*** Update File: .ea/state.json\n@@\n"}),
        ("apply_patch", {"command": ["apply_patch", "*** Delete File: .ea/store/a.jsonl\n"]}),
    ],
)
def test_surf_051_a_file_tool_writing_a_canonical_file_is_denied(
    tree: _Tree, tool: str, tool_input: dict[str, object]
) -> None:
    resolved = {
        key: value.format(main=tree.main) if isinstance(value, str) else value
        for key, value in tool_input.items()
    }
    denial = judge_tool_call(tool, resolved, cwd=tree.main, anchor=tree.main)
    assert denial is not None and denial.pattern is CANONICAL


@pytest.mark.parametrize(
    "command",
    [
        "git worktree remove .ea/worktrees/w1",
        "git worktree remove --force {main}/.ea/worktrees/w1",
        "git -C {main} worktree move .ea/worktrees/w1 /tmp/w1",
        "rm -rf .ea/worktrees/w1",
        "rm -rf ./.ea/worktrees/w1/",
        "rm -rf .ea/worktrees/*",
        "rm -rf .ea/worktrees",
        "rm -rf {main}",
        "mv .ea/worktrees/w1 /tmp/w1",
        "find .ea/worktrees -delete",
        "rm -rf .ea/local/epoch2/workspaces/wsh-1",
        "git -C .ea/worktrees/w1 reset --hard HEAD~1",
        "cd .ea/worktrees/w1 && git clean -fdx",
        "git -C .ea/worktrees/w1 checkout -- .",
        "git -C .ea/worktrees/w1 restore src/a.py",
        "git -C .ea/worktrees/w1 stash drop",
        "git --work-tree=.ea/worktrees/w1 switch --discard-changes main",
        "sudo -n git -C '.ea/worktrees/w1' reset --hard",
    ],
)
def test_surf_051_a_foreign_mutation_of_a_managed_worktree_is_denied(
    tree: _Tree, command: str
) -> None:
    assert _bash(tree, command.format(main=tree.main)) is FOREIGN


@pytest.mark.parametrize(
    "command",
    [
        "git worktree add ../elsewhere -b feat",
        "git worktree add -b feat /tmp/wt HEAD",
        "git worktree add .claude/worktrees/x",
        "git worktree add .ea/worktrees",
        "git -C .ea/worktrees/w1 worktree add ../../../sibling",
        "cd /tmp && git -C {main} worktree add --detach ../wt HEAD",
    ],
)
def test_surf_050_surf_051_out_of_root_worktree_creation_is_denied(
    tree: _Tree, command: str
) -> None:
    assert _bash(tree, command.format(main=tree.main)) is OUT_OF_ROOT


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("EnterWorktree", {"name": "feature"}),
        ("EnterWorktree", {}),
        ("Agent", {"prompt": "x", "isolation": "worktree"}),
        ("Task", {"prompt": "x", "isolation": "worktree"}),
    ],
)
def test_surf_050_a_host_native_worktree_is_denied(
    tree: _Tree, tool: str, tool_input: dict[str, object]
) -> None:
    denial = judge_tool_call(tool, tool_input, cwd=tree.main, anchor=tree.main)
    assert denial is not None and denial.pattern is OUT_OF_ROOT


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'wip'",
        "git push origin HEAD",
        "rtk git commit -am x",
        "GIT_AUTHOR_NAME=x git -c user.name=x commit -m y",
        "git add -A && git commit -m y",
        "bash -c 'git push'",
    ],
)
def test_surf_051_a_commit_from_a_drifted_working_directory_is_denied(
    tree: _Tree, command: str
) -> None:
    assert _bash(tree, command, cwd=tree.worktree / "src") is DRIFT


@pytest.mark.parametrize(
    ("command", "where"),
    [
        ("git commit -m x", "main"),
        ("git commit -m x", "main/src"),
        ("git -C {main} commit -m x", "worktree"),
        ("cd {main} && git commit -m x", "worktree"),
        ("git -C {worktree} commit -m x", "main"),
        ("git -C {worktree} push", "main"),
        ("git --git-dir={main}/.git push", "worktree"),
    ],
)
def test_surf_051_an_anchored_or_explicit_commit_is_allowed(
    tree: _Tree, command: str, where: str
) -> None:
    cwd = {"main": tree.main, "main/src": tree.main / "src", "worktree": tree.worktree}[where]
    assert _bash(tree, command.format(main=tree.main, worktree=tree.worktree), cwd=cwd) == (
        "allowed"
    )


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat .ea/state.json",
        "jq '.phases' .ea/state.json",
        "grep -c x .ea/store/event.jsonl",
        "sqlite3 -readonly .ea/telemetry.db 'select 1'",
        """python3 -c "print(open('.ea/state.json').read())" """,
        "git add .ea/state.json",
        "git status && git log --oneline -3",
        "git worktree list",
        "git worktree prune",
        "git worktree add .ea/worktrees/new -b feat",
        "git worktree add -b feat .ea/worktrees/new base",
        "git worktree remove /tmp/unmanaged",
        "rm -rf .ea/worktrees/w1/build",
        "rm -rf build dist",
        "git -C .ea/worktrees/w1 status",
        "git -C .ea/worktrees/w1 restore --staged src/a.py",
        "git -C .ea/worktrees/w1 stash list",
        "git commit -m 'rm -rf .ea; git push'",
        "git commit -F - <<'EOF'\nrm -rf .ea\necho x > .ea/state.json\nEOF",
        "echo 'git worktree remove .ea/worktrees/w1'",
        "eawf worktree cleanup --wave P01-I01-W01",
        "uv run pytest -q tests/unit",
        "echo hi 2>&1 > out.txt",
    ],
)
def test_surf_052_everything_outside_the_four_patterns_is_allowed(
    tree: _Tree, command: str
) -> None:
    assert _bash(tree, command) == "allowed"


def test_surf_051_a_session_resetting_its_own_worktree_is_allowed(tree: _Tree) -> None:
    assert _bash(tree, "git reset --hard HEAD", cwd=tree.worktree) == "allowed"
    assert _bash(tree, "git clean -fd", cwd=tree.worktree / "src") == "allowed"


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Edit", {"file_path": ".ea/local/research/brief.md"}),
        ("Write", {"file_path": ".ea/local/epoch2/workspaces/wsh-1/src/a.py"}),
        ("Edit", {"file_path": ".ea/worktrees/w1/src/a.py"}),
        ("Agent", {"prompt": "x"}),
        ("Read", {"file_path": ".ea/state.json"}),
        ("Read", "not even a mapping"),
        ("Grep", None),
    ],
)
def test_surf_052_a_call_outside_the_patterns_is_allowed(
    tree: _Tree, tool: str, tool_input: object
) -> None:
    assert judge_tool_call(tool, tool_input, cwd=tree.main, anchor=tree.main) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Bash", {"command": 'echo "unterminated'}),
        ("Bash", {"command": "cat <"}),
        ("Bash", {}),
        ("Bash", "echo hi"),
        ("Edit", {"old_string": "a"}),
        ("apply_patch", {}),
        ("Agent", ["isolation"]),
    ],
)
def test_surf_051_a_judged_call_that_cannot_be_read_is_denied(
    tree: _Tree, tool: str, tool_input: object
) -> None:
    denial = judge_tool_call(tool, tool_input, cwd=tree.main, anchor=tree.main)
    assert denial is not None
    assert denial.pattern is None
    assert denial.rule == UNJUDGED_RULE


def test_surf_051_a_command_given_as_argv_is_judged(tree: _Tree) -> None:
    denial = judge_tool_call(
        "shell", {"command": ["bash", "-lc", "rm -rf .ea"]}, cwd=tree.main, anchor=tree.main
    )
    assert denial is not None and denial.pattern is CANONICAL


def test_surf_052_the_per_call_set_is_exactly_four_patterns() -> None:
    assert {pattern.value for pattern in DataLossPattern} == {
        "foreign_worktree_mutation",
        "out_of_root_worktree",
        "drifted_commit",
        "canonical_store_edit",
    }
    assert set(RULE_VALUES) == {*(p.value for p in DataLossPattern), UNJUDGED_RULE}
    assert "Read" not in JUDGED_TOOLS and "Grep" not in JUDGED_TOOLS


def test_surf_050_the_isolated_subagent_denial_says_where_to_create_the_worktree(
    tree: _Tree,
) -> None:
    denial = judge_tool_call(
        "Agent", {"prompt": "x", "isolation": "worktree"}, cwd=tree.main, anchor=tree.main
    )
    assert denial is not None
    assert "git worktree add .ea/worktrees/<name>" in denial.reason
