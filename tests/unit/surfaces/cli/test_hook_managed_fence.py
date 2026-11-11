"""The hook acts only inside a repository eawf manages.

A repository without ``.ea`` is not epoch 1: the hook stands down there
without a word and blocks no skill. The root is the ``-w`` directory, else the
main checkout of the host's ``cwd``, so a subfolder of a managed repository
answers for that repository. A tree in the eawf 0.8 layout is named once, at
session start. The bundle-epoch fence is gone; ``--target-epoch`` is ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the fence's gates pin this module

from eawf.kernel.migration.epoch2.canary import V08_LAYOUT_MESSAGE, uses_v08_layout
from eawf.kernel.state.epoch2.authority import (
    NativeAuthorityRequiredError,
    require_native_authority,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import hook as hook_command
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.skills.publication import is_managed
from tests._epoch2_helpers import lay_epoch2_tree
from tests._linked_worktree import git, repo_with_linked_worktree

runner = CliRunner()


def _run_hook(event: str, payload: dict[str, Any], *extra: str) -> Any:
    return runner.invoke(
        app,
        [*extra, "hook", "run", event, "--runtime", "claude", "--target-epoch", "2"],
        input=json.dumps(payload),
    )


def _prompt(cwd: Path) -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "host-1",
        "prompt": "/eawf:research q",
        "cwd": str(cwd),
    }


def _v08_tree(root: Path) -> Path:
    (root / ".ea" / "state").mkdir(parents=True)
    return root


# ---- is_managed --------------------------------------------------------------


def test_is_managed_true_for_an_ea_directory(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    assert is_managed(tmp_path)


def test_is_managed_false_for_an_empty_directory(tmp_path: Path) -> None:
    assert not is_managed(tmp_path)


def test_is_managed_false_when_ea_is_a_file(tmp_path: Path) -> None:
    (tmp_path / ".ea").write_text("", encoding="utf-8")
    assert not is_managed(tmp_path)


def test_is_managed_false_for_a_missing_root(tmp_path: Path) -> None:
    assert not is_managed(tmp_path / "absent")


def test_is_managed_rejects_a_non_path() -> None:
    with pytest.raises(TypeError):
        is_managed("repo")  # type: ignore[arg-type]


# ---- the hook's repository context -------------------------------------------


def test_root_from_payload_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "linked-repo")
    (main / "src").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    def root(stdin: str, workspace: Path | None = None) -> Path:
        return hook_command._hook_context(stdin, GlobalFlags(workspace=workspace)).main

    from_src = json.dumps({"cwd": str(main / "src")})
    # The -w flag wins over every cwd.
    assert root(from_src, workspace=elsewhere) == elsewhere.resolve()
    assert root(json.dumps({"cwd": str(elsewhere)}), workspace=linked) == main
    # The payload cwd resolves to its main checkout, from a subfolder or a linked worktree.
    assert root(from_src) == main
    assert root(json.dumps({"cwd": str(linked)})) == main
    # No payload cwd falls back to the process cwd.
    monkeypatch.chdir(main / "src")
    assert root("{}") == main
    assert root("not json") == main
    # Without git, the walk stops at the first directory holding .ea.
    plain = tmp_path / "plain"
    deep = plain / "a" / "b"
    deep.mkdir(parents=True)
    (plain / ".ea").mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert root(json.dumps({"cwd": str(deep)})) == plain.resolve()
    monkeypatch.chdir(deep)
    assert root("{}") == plain.resolve()
    # Without git or .ea, the root is the cwd itself.
    (plain / ".ea").rmdir()
    assert root("{}") == deep.resolve()


def test_vcs_package_loads_a_re_export_on_first_access() -> None:
    import eawf.runtime.vcs as vcs
    from eawf.runtime.vcs.coauthor import VcsConfig

    assert vcs.VcsConfig is VcsConfig
    with pytest.raises(AttributeError, match="no attribute 'absent'"):
        vcs.absent  # noqa: B018


# ---- the payload cwd ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stdin_text", "expected"),
    [
        ("", None),
        ("{}", None),
        ('{"cwd": ""}', None),
        ('{"cwd": 3}', None),
        ("not json", None),
        ("[1]", None),
        ('{"cwd": "/repo/src"}', Path("/repo/src")),
    ],
)
def test_payload_cwd_reads_only_a_non_empty_string(stdin_text: str, expected: Path | None) -> None:
    assert hook_command._payload_cwd(stdin_text) == expected


def test_parse_payload_still_refuses_malformed_json() -> None:
    with pytest.raises(cli_errors.UserError):
        hook_command._parse_payload("not json")


# ---- the hook in an unmanaged or managed repository ----------------------------


def test_unmanaged_git_dir_stands_down_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    result = _run_hook("user_prompt_submit", {**_prompt(tmp_path), "prompt": "/eawf:plan x"})
    assert result.exit_code == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_managed_subfolder_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git(tmp_path, "init", "-q")
    lay_epoch2_tree(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.chdir(src)
    result = _run_hook("user_prompt_submit", _prompt(src))
    assert result.exit_code == 0
    assert '"decision"' not in result.stdout


def test_an_epoch1_tree_is_no_longer_refused_by_the_bundle_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale-bundle fence is gone: an older bundle's ``--target-epoch`` is ignored."""
    git(tmp_path, "init", "-q")
    (tmp_path / ".ea").mkdir()
    monkeypatch.chdir(tmp_path)
    result = _run_hook("user_prompt_submit", _prompt(tmp_path))
    assert result.exit_code == 0
    assert '"decision"' not in result.stdout


# ---- the eawf 0.8 layout -----------------------------------------------------


def test_v08_layout_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _v08_tree(tmp_path)
    monkeypatch.chdir(root)
    session = {"hook_event_name": "SessionStart", "session_id": "host-1", "cwd": str(root)}
    started = _run_hook("session_start", session)
    assert started.exit_code == 0
    assert started.stdout == f"{V08_LAYOUT_MESSAGE}\n"
    assert started.stderr == ""
    tool = {
        "hook_event_name": "PreToolUse",
        "session_id": "host-1",
        "cwd": str(root),
        "tool_name": "Skill",
        "tool_input": {"skill": "eawf:research", "args": "q"},
    }
    used = _run_hook("pre_tool_use", tool)
    assert used.exit_code == 0
    assert used.stdout == ""
    assert used.stderr == ""
    with pytest.raises(NativeAuthorityRequiredError) as refused:
        require_native_authority(root / ".ea")
    assert str(refused.value) == V08_LAYOUT_MESSAGE


def test_uses_v08_layout_needs_state_without_generations(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    assert not uses_v08_layout(tree)
    (tree / "state").mkdir(parents=True)
    assert uses_v08_layout(tree)
    (tree / "generations").mkdir()
    assert not uses_v08_layout(tree)


def test_uses_v08_layout_false_when_state_is_a_file(tmp_path: Path) -> None:
    tmp_path.joinpath("state").write_text("{}", encoding="utf-8")
    assert not uses_v08_layout(tmp_path)


def test_an_epoch1_tree_keeps_the_epoch2_refusal(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    with pytest.raises(NativeAuthorityRequiredError) as refused:
        require_native_authority(tmp_path / ".ea")
    assert "resolves to authority epoch 1" in str(refused.value)


def test_uses_v08_layout_rejects_a_non_path() -> None:
    with pytest.raises(TypeError):
        uses_v08_layout(".ea")  # type: ignore[arg-type]
