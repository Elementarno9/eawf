"""The four fail-closed data-loss patterns, judged per host tool call.

Every command here is judged, never run: the guard's decision is the subject, and no
case executes the mutation it names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import pytest

from eawf.runtime.daemon.methods.host_deny import DenyRule
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
        ("git push", "main"),
        ("git push", "main/src"),
        ("git -C {main} push", "worktree"),
        ("cd {main} && git push", "worktree"),
        ("git -C {worktree} commit -m x", "main"),
        ("cd {worktree} && git commit -m x", "main"),
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
        "git -C .ea/worktrees/w1 commit -m 'rm -rf .ea; git push'",
        "git -C .ea/worktrees/w1 commit -F - <<'EOF'\nrm -rf .ea\necho x > .ea/state.json\nEOF",
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


@pytest.mark.parametrize(
    "command",
    [
        "echo \"it's fine",
        "grep -n \"don't",
        "echo it\\'s",
        "ls \\",
        "'",
        'git log --format="%s',
    ],
)
def test_a_harmless_command_with_an_unbalanced_quote_is_allowed(tree: _Tree, command: str) -> None:
    assert _bash(tree, command) == "allowed"


@pytest.mark.parametrize(
    ("lexable", "unbalanced", "pattern"),
    [
        ("rm -rf .ea", "rm -rf .ea; echo \"it's", CANONICAL),
        ("rm -rf '.ea'", "rm -rf '.ea' && echo \"don't", CANONICAL),
        ("rm -rf .ea", 'rm -rf \\.ea "x', CANONICAL),
        (
            "git checkout HEAD -- .ea/state.json",
            'git checkout HEAD -- .ea/state.json "x',
            CANONICAL,
        ),
        ("git restore .ea/store", 'git restore .ea/store "x', CANONICAL),
        ("git reset --hard", "git reset --hard; echo \"it's", CANONICAL),
        ("rm -rf .ea/worktrees/w1", 'rm -rf .ea/worktrees/w1 "x', FOREIGN),
        ("git worktree add ../elsewhere", 'git worktree add ../elsewhere "x', OUT_OF_ROOT),
        ("echo x > .ea/state.json", "echo x > .ea/state.json \\", CANONICAL),
    ],
)
def test_a_destructive_command_with_an_unbalanced_quote_keeps_its_pattern(
    tree: _Tree, lexable: str, unbalanced: str, pattern: DataLossPattern
) -> None:
    assert _bash(tree, lexable) is pattern
    assert _bash(tree, unbalanced) is pattern


_QUOTED_SCRIPT_BYPASSES = [
    ("bash -c 'rm -rf .ea'", "bash -c 'rm -rf .ea'; echo it's"),
    ("bash -lc 'rm -rf .ea'", "bash -lc 'rm -rf .ea' it's"),
    ('sh -c "git reset --hard"', 'sh -c "git reset --hard" ; echo don\'t'),
    ("env X='a b' rm -rf .ea", "env X='a b' rm -rf .ea; echo it's"),
]


@pytest.mark.parametrize(("lexable", "unbalanced"), _QUOTED_SCRIPT_BYPASSES)
def test_an_unbalanced_quote_keeps_a_quoted_script_whole(
    tree: _Tree, lexable: str, unbalanced: str
) -> None:
    assert _bash(tree, lexable) is CANONICAL
    assert _bash(tree, unbalanced) is CANONICAL


@pytest.mark.parametrize("wrap", ["echo $({})", "echo `{}`"])
@pytest.mark.parametrize(("lexable", "unbalanced"), _QUOTED_SCRIPT_BYPASSES)
def test_an_unbalanced_quote_keeps_a_substituted_script_whole(
    tree: _Tree, lexable: str, unbalanced: str, wrap: str
) -> None:
    assert _bash(tree, wrap.format(lexable)) is CANONICAL
    assert _bash(tree, wrap.format(unbalanced)) is CANONICAL


def test_an_unbalanced_quote_keeps_the_directory_a_cd_moved_to(tree: _Tree) -> None:
    assert _bash(tree, 'bash -c "cd .ea; echo its"; rm -rf store') is CANONICAL
    assert _bash(tree, 'bash -c "cd .ea; echo it\'s"; rm -rf store') is CANONICAL


def test_a_destructive_command_inside_an_unclosed_quote_is_text(tree: _Tree) -> None:
    assert _bash(tree, "echo \"it's; rm -rf .ea") == "allowed"


def test_an_empty_command_is_allowed(tree: _Tree) -> None:
    assert _bash(tree, "") == "allowed"


def test_surf_051_a_command_given_as_argv_is_judged(tree: _Tree) -> None:
    denial = judge_tool_call(
        "shell", {"command": ["bash", "-lc", "rm -rf .ea"]}, cwd=tree.main, anchor=tree.main
    )
    assert denial is not None and denial.pattern is CANONICAL


def test_surf_052_the_per_call_set_is_exactly_five_patterns() -> None:
    assert {pattern.value for pattern in DataLossPattern} == {
        "foreign_worktree_mutation",
        "out_of_root_worktree",
        "drifted_commit",
        "canonical_store_edit",
        "unlocked_state_commit",
    }
    assert set(RULE_VALUES) == {*(p.value for p in DataLossPattern), UNJUDGED_RULE}
    assert "Read" not in JUDGED_TOOLS and "Grep" not in JUDGED_TOOLS


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Edit", {"file_path": ".ea/generations/gen-1/state.json"}),
        ("Write", {"file_path": "{main}/.ea/generations/gen-1/local/task.json"}),
        ("Bash", {"command": "rm -rf .ea/generations"}),
        ("Bash", {"command": "echo x >> .ea/generations/gen-1/ledger/run.jsonl"}),
        ("Bash", {"command": "git checkout -- .ea/generations/gen-1/state.json"}),
        ("apply_patch", {"input": "*** Update File: .ea/generations/selected.json\n"}),
    ],
)
def test_a_write_to_generation_state_is_denied(
    tree: _Tree, tool: str, tool_input: dict[str, object]
) -> None:
    resolved = {
        key: value.format(main=tree.main) if isinstance(value, str) else value
        for key, value in tool_input.items()
    }
    denial = judge_tool_call(tool, resolved, cwd=tree.main, anchor=tree.main)
    assert denial is not None and denial.pattern is CANONICAL


@pytest.mark.parametrize(
    ("tool", "tool_input", "expected"),
    [
        ("exec_command", {"cmd": "rm -rf w1", "workdir": ".ea/worktrees"}, FOREIGN),
        ("exec_command", {"cmd": "rm -rf w1", "workdir": "{main}/.ea/worktrees"}, FOREIGN),
        ("exec_command", {"cmd": "rm state.json", "workdir": ".ea"}, CANONICAL),
        ("exec_command", {"cmd": "git reset --hard", "workdir": ".ea/worktrees/w1"}, FOREIGN),
        (
            "local_shell",
            {"command": ["rm", "-rf", "w1"], "working_directory": ".ea/worktrees"},
            FOREIGN,
        ),
        ("exec_command", {"cmd": "rm -rf build", "workdir": ".ea/worktrees/w1"}, None),
        ("exec_command", {"cmd": "rm -rf w1", "workdir": None}, None),
    ],
)
def test_a_codex_shell_call_is_judged_in_its_workdir(
    tree: _Tree, tool: str, tool_input: dict[str, object], expected: DataLossPattern | None
) -> None:
    resolved = {
        key: value.format(main=tree.main) if isinstance(value, str) else value
        for key, value in tool_input.items()
    }
    denial = judge_tool_call(tool, resolved, cwd=tree.main, anchor=tree.main)
    assert (denial and denial.pattern) is expected


def test_a_commit_in_an_explicit_codex_workdir_is_not_drift(tree: _Tree) -> None:
    tool_input = {"cmd": "git commit -m x", "workdir": str(tree.worktree)}
    assert judge_tool_call("exec_command", tool_input, cwd=tree.main, anchor=tree.main) is None


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.pyc' -delete",
        "find -L . -name __pycache__ -type d -exec rm -rf {} +",
        "find . -type f -exec grep -l needle {} +",
        "find src -exec grep x {} \\;",
        "find src -delete",
        "find . -name '*.py' -print",
        "mv build/out.txt .",
        "mv -t . build/a.txt build/b.txt",
        "mv a.txt .ea/worktrees/w1/src/",
        "find . -name '*.pyc' | xargs rm -f",
        "echo build | xargs rm -rf",
        "rg -l foo | xargs grep bar",
        "bash -o pipefail -c 'rm -rf build'",
        "git clean -n",
        "git reset --soft HEAD~1",
    ],
)
def test_a_harmless_call_from_the_repo_root_is_allowed(tree: _Tree, command: str) -> None:
    assert _bash(tree, command) == "allowed"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("find . -delete", FOREIGN),
        ("find . -type f -delete", FOREIGN),
        ("find . -name '*.json' -delete", FOREIGN),
        ("find . ! -name '*.pyc' -delete", FOREIGN),
        ("find . -name '*.pyc' -o -path '*' -delete", FOREIGN),
        ("find . -name state.json -exec rm {} +", FOREIGN),
        ("find .ea/local -delete", FOREIGN),
        ("find .ea/state.json -delete", CANONICAL),
        ("mv x.json .ea/state.json", CANONICAL),
        ("mv -t /tmp .ea/worktrees/w1", FOREIGN),
    ],
)
def test_a_find_or_mv_aimed_at_protected_paths_is_denied(
    tree: _Tree, command: str, expected: DataLossPattern
) -> None:
    assert _bash(tree, command) is expected


@pytest.mark.parametrize(
    ("command", "cwd", "expected"),
    [
        ('rm -rf "$PWD/.ea/worktrees/w1"', "main", FOREIGN),
        ("rm -rf ${PWD}/.ea/worktrees/w1", "main", FOREIGN),
        ('rm -rf "$(pwd)/.ea/worktrees/w1"', "main", FOREIGN),
        ("rm -rf $(pwd)/.ea/worktrees/w1", "main", FOREIGN),
        ("rm -rf `pwd -P`/.ea/worktrees/w1", "main", FOREIGN),
        ('rm -rf "$PWD/.."', "worktree/src", FOREIGN),
        ('cd .ea && rm -rf "$PWD/worktrees"', "main", FOREIGN),
        ("echo .ea/worktrees/w1 | xargs rm -rf", "main", FOREIGN),
        ("printf '%s\\n' .ea/state.json | xargs -n 1 rm", "main", CANONICAL),
        ("xargs -0 rm -rf .ea/worktrees/w1 < /dev/null", "main", FOREIGN),
        ("find .ea/worktrees -maxdepth 1 | xargs rm -rf", "main", FOREIGN),
        ("bash -o pipefail -c 'rm -rf .ea/worktrees/w1'", "main", FOREIGN),
        ("bash +x -O extglob -c 'rm -rf .ea/state.json'", "main", CANONICAL),
        ("git clean -ffdx", "main", CANONICAL),
        ("git clean --force -d", "main/src", CANONICAL),
        ("git reset --hard", "main", CANONICAL),
        ("git -C {main} reset --hard HEAD", "worktree", CANONICAL),
    ],
)
def test_a_guard_bypass_is_denied(
    tree: _Tree, command: str, cwd: str, expected: DataLossPattern
) -> None:
    where = {
        "main": tree.main,
        "main/src": tree.main / "src",
        "worktree": tree.worktree,
        "worktree/src": tree.worktree / "src",
    }[cwd]
    assert _bash(tree, command.replace("{main}", str(tree.main)), cwd=where) is expected


@pytest.mark.parametrize(
    "command", ["git ls-files | xargs rm", "xargs rm -rf < paths.txt", "xargs mv -t /tmp"]
)
def test_xargs_removing_what_the_guard_cannot_see_is_unjudged(tree: _Tree, command: str) -> None:
    denial = judge_tool_call("Bash", {"command": command}, cwd=tree.main, anchor=tree.main)
    assert denial is not None and denial.pattern is None


def test_surf_050_the_isolated_subagent_denial_says_where_to_create_the_worktree(
    tree: _Tree,
) -> None:
    denial = judge_tool_call(
        "Agent", {"prompt": "x", "isolation": "worktree"}, cwd=tree.main, anchor=tree.main
    )
    assert denial is not None
    assert "git worktree add .ea/worktrees/<name>" in denial.reason


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .ea/worktrees/w1",
        "git worktree remove .ea/worktrees/w1",
        "git worktree move .ea/worktrees/w1 ../w2",
    ],
    ids=["rm", "git-remove", "git-move"],
)
def test_a_worktree_removal_denial_names_who_removes_it(tree: _Tree, command: str) -> None:
    denial = judge_tool_call("Bash", {"command": command}, cwd=tree.main, anchor=tree.main)
    assert denial is not None and denial.pattern is FOREIGN
    assert "eawf worktree gc --dry-run" in denial.reason
    assert "per landed, clean tree, for the operator" in denial.reason


UNLOCKED = DataLossPattern.UNLOCKED_STATE_COMMIT

#: A command line, or a function that spells it with the directory it names.
_Command = str | Callable[[Path], str]


def _spelled(command: _Command, path: Path) -> str:
    return command if isinstance(command, str) else command(path)


def _commit_in(tree: Path) -> str:
    return f"git -C {tree} commit -m x"


def _cd_and_commit(tree: Path) -> str:
    return f"cd {tree} && git commit -m x"


def _cherry_pick_in(tree: Path) -> str:
    return f"git -C {tree} cherry-pick --continue"


@pytest.mark.parametrize(
    ("command", "where", "wrapper"),
    [
        ("git commit -m x", "main", "eawf git commit"),
        ("git commit -m x", "main/src", "eawf git commit"),
        (_commit_in, "worktree", "eawf git commit"),
        (_cd_and_commit, "worktree", "eawf git commit"),
        ("git add -A && git commit -m y", "main", "eawf git commit"),
        ("rtk git -c user.name=x commit -am y", "main", "eawf git commit"),
        ("git commit -m 'rm -rf .ea; git push'", "main", "eawf git commit"),
        ("git cherry-pick abc1234", "main", "eawf git cherry-pick"),
        (_cherry_pick_in, "worktree", "eawf git cherry-pick"),
        ("pre-commit run", "main", "eawf git commit"),
        ("pre-commit", "main", "eawf git commit"),
        ("pre-commit run ruff --hook-stage pre-commit", "main/src", "eawf git commit"),
        ("pre-commit run --files", "main", "eawf git commit"),
        ("uv run pre-commit run", "main", "eawf git commit"),
        ("uv run --frozen pre-commit run mypy", "main", "eawf git commit"),
        ("uv run --with pre-commit pre-commit run", "main", "eawf git commit"),
        ("uv run --project . pre-commit run", "main", "eawf git commit"),
        ("uv run -p 3.14 -- pre-commit run", "main", "eawf git commit"),
    ],
)
def test_an_unlocked_state_commit_in_the_main_checkout_is_denied(
    tree: _Tree, command: _Command, where: str, wrapper: str
) -> None:
    cwd = {"main": tree.main, "main/src": tree.main / "src", "worktree": tree.worktree}[where]
    denial = judge_tool_call(
        "Bash", {"command": _spelled(command, tree.main)}, cwd=cwd, anchor=tree.main
    )
    assert denial is not None and denial.pattern is UNLOCKED
    assert f"{wrapper} -- <git args>" in denial.reason
    assert denial.rule == "unlocked_state_commit"
    assert RULE_VALUES[denial.rule]


@pytest.mark.parametrize(
    ("command", "where"),
    [
        ("git commit -m x", "worktree"),
        ("git cherry-pick abc1234", "worktree"),
        ("pre-commit run", "worktree"),
        ("git commit -m x", "workspace"),
        (_commit_in, "main"),
        ("pre-commit run --all-files", "main"),
        ("pre-commit run -a", "main"),
        ("pre-commit run --files src/a.py", "main"),
        ("pre-commit run ruff --files=src/a.py", "main"),
        ("uv run pre-commit run --all-files", "main"),
        ("uv run --with pre-commit pre-commit run --all-files", "main"),
        ("pre-commit install", "main"),
        ("pre-commit --version", "main"),
        ("eawf git commit -- -m x", "main"),
        ("uv run eawf git commit -- -m x", "main"),
        ("eawf git cherry-pick -- abc1234", "main"),
        ("git log --oneline -1 && git status", "main"),
    ],
)
def test_a_commit_outside_the_main_checkout_or_through_the_wrapper_is_allowed(
    tree: _Tree, command: _Command, where: str
) -> None:
    cwd = {"main": tree.main, "worktree": tree.worktree, "workspace": tree.workspace}[where]
    denial = judge_tool_call(
        "Bash", {"command": _spelled(command, tree.worktree)}, cwd=cwd, anchor=cwd
    )
    assert denial is None


@pytest.mark.parametrize(
    "command", ["git commit -m x", "git cherry-pick abc1234", "pre-commit run", "pre-commit"]
)
def test_a_commit_in_an_unmanaged_repository_is_allowed(tmp_path: Path, command: str) -> None:
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert judge_tool_call("Bash", {"command": command}, cwd=plain, anchor=plain) is None


def test_a_drifted_commit_into_a_worktree_still_names_the_drift(tree: _Tree) -> None:
    # The unlocked-commit judge looks only at the main checkout, so a commit that
    # drifted into a linked worktree keeps the drift refusal.
    denial = judge_tool_call(
        "Bash", {"command": "git commit -m x"}, cwd=tree.worktree, anchor=tree.main
    )
    assert denial is not None and denial.pattern is DRIFT


def test_every_pattern_is_a_rule_the_daemon_records_a_commit_denial_under() -> None:
    assert set(get_args(DenyRule)) == {*(p.value for p in DataLossPattern), UNJUDGED_RULE}
