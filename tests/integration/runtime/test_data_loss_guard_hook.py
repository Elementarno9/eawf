"""The data-loss guard on the live pre-tool path: ``eawf hook run pre_tool_use``.

Each case feeds the real hook command the payload a host sends and reads back what the
host would read: the deny document on stdout, or nothing. The mutations named in the
payloads are judged and never executed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Final

import jsonschema
import pytest
from typer.testing import CliRunner

from eawf.kernel.runtime.sandbox_decision import SandboxDecisionOutcome, sandbox_decisions
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.hooks.data_loss_guard import DENY_EMISSION_RUNTIMES, UNENFORCED_RUNTIMES
from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.runtimes.codex.hook_map import CODEX_HOOK_EVENT_NAMES
from eawf.runtime.runtimes.codex.plugin_install import _render_hook_config
from eawf.runtime.sandbox.data_loss import DATA_LOSS_POLICY_REVISION
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.render.hooks import render_hook_sh
from tests.tui.surfaces.tui.console.test_transcript_live import HOST_SESSION, PARENT, live_tree

__all__ = ["live_tree"]

#: The pinned pre-tool decision wire of each host the guard emits to.
HOST_SCHEMAS: Final = Path(__file__).resolve().parents[2] / "fixtures/hosts/pre_tool_use"
SCHEMA_BY_RUNTIME: Final = {
    "claude": "claude-code-2.1.285.output.schema.json",
    "codex": "codex-0.154.0.output.schema.json",
}


class _Unreachable:
    """A daemon client factory whose daemon is down, counting every attempt."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise ConnectionRefusedError("daemon unreachable")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A main checkout with one managed worktree, a tmp HOME and a down daemon."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    main = tmp_path / "repo"
    (main / ".git" / "worktrees" / "w1").mkdir(parents=True)
    (main / ".ea").mkdir()
    worktree = main / ".ea" / "worktrees" / "w1"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/w1\n")
    return main


@pytest.fixture
def down(monkeypatch: pytest.MonkeyPatch) -> _Unreachable:
    factory = _Unreachable()
    monkeypatch.setattr("eawf.runtime.hooks.runner._default_daemon_client_factory", factory)
    return factory


def _hook(
    repo: Path,
    tool: str,
    tool_input: object,
    *,
    runtime: str = "claude",
    extra: tuple[str, ...] = (),
) -> Any:
    body = {
        "session_id": HOST_SESSION,
        "hook_event_name": "PreToolUse",
        "cwd": str(repo),
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "toolu_01GuardA1b2C3d4E5f6G7h8",
    }
    return CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", "pre_tool_use", "--runtime", runtime, *extra],
        input=json.dumps(body),
    )


def _decision(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    return document["hookSpecificOutput"]


@pytest.mark.parametrize("runtime", sorted(DENY_EMISSION_RUNTIMES))
@pytest.mark.parametrize(
    ("tool", "tool_input", "rule"),
    [
        ("Bash", {"command": "rm -rf .ea/worktrees/w1"}, "foreign_worktree_mutation"),
        ("Bash", {"command": "git worktree add ../elsewhere"}, "out_of_root_worktree"),
        ("Edit", {"file_path": ".ea/state.json"}, "canonical_store_edit"),
        ("EnterWorktree", {"name": "x"}, "out_of_root_worktree"),
        ("Bash", {"command": 'echo "open'}, "unjudged"),
    ],
)
def test_surf_051_surf_103_the_live_hook_denies_with_the_daemon_down(
    repo: Path, down: _Unreachable, runtime: str, tool: str, tool_input: object, rule: str
) -> None:
    result = _hook(repo, tool, tool_input, runtime=runtime)
    decision = _decision(result)
    assert decision["permissionDecision"] == "deny"
    assert f"({rule})" in decision["permissionDecisionReason"]
    assert down.calls == 1, (
        "the refusal is recorded best-effort, and the down daemon did not stop it"
    )


@pytest.mark.parametrize("runtime", sorted(DENY_EMISSION_RUNTIMES))
@pytest.mark.parametrize("stdin", ["not json", "[1, 2]"])
def test_surf_051_an_unreadable_payload_is_denied(
    repo: Path, down: _Unreachable, runtime: str, stdin: str
) -> None:
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", "pre_tool_use", "--runtime", runtime],
        input=stdin,
    )
    decision = _decision(result)
    assert decision["permissionDecision"] == "deny"
    assert "(unjudged)" in decision["permissionDecisionReason"]


def test_surf_051_a_bundle_epoch_mismatch_does_not_stand_the_guard_down(
    repo: Path, down: _Unreachable
) -> None:
    result = _hook(repo, "Bash", {"command": "rm -rf .ea"}, extra=("--target-epoch", "999"))
    assert _decision(result)["permissionDecision"] == "deny"


def test_surf_051_a_drifted_commit_is_denied_against_the_session_project_dir(
    repo: Path, down: _Unreachable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    body = {
        "session_id": HOST_SESSION,
        "cwd": str(repo / ".ea" / "worktrees" / "w1"),
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m wip"},
        "tool_use_id": "toolu_01Drift",
    }
    result = CliRunner().invoke(
        cli,
        ["--workspace", str(repo), "hook", "run", "pre_tool_use", "--runtime", "claude"],
        input=json.dumps(body),
    )
    assert "(drifted_commit)" in _decision(result)["permissionDecisionReason"]


def test_surf_052_an_allowed_call_gets_no_decision(repo: Path, down: _Unreachable) -> None:
    result = _hook(repo, "Bash", {"command": "git status"})
    assert result.exit_code == 0, result.output
    assert result.stdout == "", "an allowed call hands Claude Code no decision"


def test_surf_103_codex_runs_the_guard_alone_and_prints_nothing_on_allow(
    repo: Path, down: _Unreachable
) -> None:
    result = _hook(repo, "Bash", {"command": "ls"}, runtime="codex")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert down.calls == 0, "no observer runs on Codex's pre-tool hook"


@pytest.mark.parametrize("runtime", sorted(DENY_EMISSION_RUNTIMES))
def test_surf_053_the_deny_validates_against_each_host_decision_wire(
    repo: Path, down: _Unreachable, runtime: str
) -> None:
    schema = json.loads((HOST_SCHEMAS / SCHEMA_BY_RUNTIME[runtime]).read_text())
    result = _hook(repo, "Bash", {"command": "rm -rf .ea"}, runtime=runtime)
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    jsonschema.validate(document, schema)
    assert document["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_surf_053_the_guard_is_enabled_only_where_emission_is_verified() -> None:
    assert set(SCHEMA_BY_RUNTIME) == DENY_EMISSION_RUNTIMES
    assert set(UNENFORCED_RUNTIMES).isdisjoint(DENY_EMISSION_RUNTIMES)
    assert "opencode" in UNENFORCED_RUNTIMES


def test_surf_103_both_plugins_subscribe_the_pre_tool_guard() -> None:
    from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json

    claude = build_plugin_hooks_json()["hooks"]["PreToolUse"]
    assert any(
        entry["matcher"] == "" and entry["hooks"][0]["command"].endswith("/pre_tool_use.sh")
        for entry in claude
    )
    assert CODEX_HOOK_EVENT_NAMES[HookEventType.PRE_TOOL_USE] == "PreToolUse"
    codex = json.loads(_render_hook_config())["hooks"]["PreToolUse"]
    assert codex[0]["hooks"][0]["command"].endswith('/hooks/pre_tool_use.sh"')


def _fake_uv(bin_dir: Path, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\n{body}\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)


def _wrapper(
    tmp_path: Path, runtime: str, tool: str, uv_body: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / f"pre_tool_use-{runtime}.sh"
    script.write_text(render_hook_sh(HookEventType.PRE_TOOL_USE, runtime=runtime))
    _fake_uv(tmp_path / "bin", uv_body)
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": "rm -rf .ea"}})
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    return subprocess.run(
        ["bash", str(script)], input=payload, capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.parametrize("runtime", sorted(DENY_EMISSION_RUNTIMES))
def test_surf_051_the_wrapper_fails_closed_when_eawf_cannot_run(
    tmp_path: Path, runtime: str
) -> None:
    broken = _wrapper(tmp_path, runtime, "Bash", "exit 1")
    assert broken.returncode == 2, "exit 2 is the deny both hosts honour"
    assert "could not run" in broken.stderr
    unjudged = _wrapper(tmp_path, runtime, "Read", "exit 1")
    assert unjudged.returncode == 0, "a tool that cannot mutate owned state is never stopped"


@pytest.mark.parametrize("runtime", sorted(DENY_EMISSION_RUNTIMES))
def test_surf_053_the_wrapper_passes_the_deny_to_the_host(tmp_path: Path, runtime: str) -> None:
    deny = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}})
    passed = _wrapper(tmp_path, runtime, "Bash", f"printf '%s' '{deny}'")
    assert passed.returncode == 0
    assert json.loads(passed.stdout) == json.loads(deny)


def test_surf_051_a_live_denial_is_filed_as_a_sandbox_decision(live_tree: Any) -> None:
    repo, _daemon = live_tree
    result = _hook(repo, "Write", {"file_path": ".ea/state.json", "content": "{}"})
    assert _decision(result)["permissionDecision"] == "deny"
    ledgers = sorted(repo.rglob(f"ledger/{Epoch2Collection.RECEIPT.value}.jsonl"))
    decisions = [
        decision
        for ledger in ledgers
        for decision in sandbox_decisions(read_ledger_records(ledger))
    ]
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.decision is SandboxDecisionOutcome.DENIED
    assert decision.run_ref.entity_key == PARENT
    assert decision.rule == "data_loss.canonical_store_edit"
    assert decision.tool_id == "write"
    assert decision.policy_revision == DATA_LOSS_POLICY_REVISION
    assert ".ea/state.json" not in decision.reason
    assert os.fspath(repo) not in decision.model_dump_json()
