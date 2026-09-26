"""``eawf hook run session_start`` reports stale rule projections once per session.

Every runtime that runs a session-start hook receives the report: Claude and
Codex as a context document on stdout, OpenCode through its plugin bridge. A
rule source that fails to load or compile is reported as one typed line naming
its failure code and the refresh command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.platform.rules.render import CARD_TARGET, render_rule_projections
from eawf.platform.rules.staleness import REFRESH_COMMAND, stamp_graph_digest
from eawf.runtime.hooks.event import HookEventType
from eawf.runtime.runtimes.opencode.plugin_install import install_plugin as install_opencode
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.hooks import render_hook_sh

_RULE = {
    "rule_id": "repo.changelog",
    "obligation_id": "demo.changelog",
    "revision": 1,
    "title": "Keep release notes in the changelog",
    "zone": "constitution",
    "force": "must",
    "effectiveness": "behavioral",
    "instruction": "Keep the release notes in the changelog under the version heading.",
    "verification": {"method": "review"},
}


@pytest.fixture
def stale_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        yaml.safe_dump({"schema_version": 1, "modules": [], "rules": [_RULE]}), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    render_rule_projections(root)
    card = root / CARD_TARGET
    text = card.read_text(encoding="utf-8")
    digest = stamp_graph_digest(text)
    assert digest is not None
    card.write_text(text.replace(digest, f"sha256:{'0' * 64}", 1), encoding="utf-8")
    return root


def _invoke(root: Path, runtime: str, session_id: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        app,
        ["-w", str(root), "hook", "run", "session_start", "--runtime", runtime],
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": session_id}),
    )
    return result.exit_code, result.stdout


def test_hook_run_session_start_prints_claude_context_once(stale_repo: Path) -> None:
    code, out = _invoke(stale_repo, "claude", "S-1")
    assert code == 0, out
    context = json.loads(out)["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert f"`{REFRESH_COMMAND}`" in context["additionalContext"]
    code, out = _invoke(stale_repo, "claude", "S-1")
    assert code == 0, out
    assert out.strip() == ""


def test_hook_run_session_start_envelope_carries_the_report(stale_repo: Path) -> None:
    code, out = _invoke(stale_repo, "generic", "S-9")
    assert code == 0, out
    results = json.loads(out)["body"]["results"]
    outputs = [r["output"] for r in results if r["name"] == "rules.projection_staleness"]
    assert len(outputs) == 1
    assert REFRESH_COMMAND in outputs[0]


# ---- Codex and OpenCode receive the report ----------------------------------


@pytest.mark.parametrize("runtime", ["codex", "opencode"])
def test_hook_run_session_start_prints_the_context_document_on(
    stale_repo: Path, runtime: str
) -> None:
    code, out = _invoke(stale_repo, runtime, "S-2")
    assert code == 0, out
    context = json.loads(out)["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    assert f"`{REFRESH_COMMAND}`" in context["additionalContext"]
    code, out = _invoke(stale_repo, runtime, "S-2")
    assert code == 0, out
    assert out.strip() == ""


def test_render_hook_sh_codex_session_start_keeps_stdout() -> None:
    start = render_hook_sh(HookEventType.SESSION_START, runtime="codex")
    assert start.rstrip("\n").endswith("hook run session_start --runtime codex")
    end = render_hook_sh(HookEventType.SESSION_END, runtime="codex")
    assert end.rstrip("\n").endswith("--runtime codex >/dev/null")


def test_render_hook_sh_claude_session_start_is_unchanged() -> None:
    start = render_hook_sh(HookEventType.SESSION_START)
    assert start.rstrip("\n").endswith("hook run session_start --runtime claude")
    assert "\nfi\n\nprintf" in start


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_opencode_bridge_surfaces_the_session_context(tmp_path: Path) -> None:
    install_opencode(tmp_path, persist_manifest=False)
    bridge = tmp_path / ".opencode" / "plugins" / "eawf.js"
    assert bridge.is_file()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    document = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "R"}}
    fake = fake_bin / "eawf"
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\necho '{json.dumps(document)}'\n", "utf-8")
    fake.chmod(0o755)
    script = (
        f"const r = require({json.dumps(str(bridge))}).hooks.onSessionStart({{}});"
        "process.stdout.write(JSON.stringify(r));"
    )
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, env=env, check=True
    )
    assert json.loads(result.stdout)["context"] == "R"
    assert "R" in result.stderr


# ---- a broken rule source is one typed line, not a traceback ----------------


def _broken_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: list[object]) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "broken"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(
        yaml.safe_dump({"schema_version": 1, "modules": [], "rules": rules}), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("rules", "code"),
    [
        ([{**_RULE, "zone": "nowhere"}], "rule_source_schema"),
        ([_RULE, {**_RULE, "obligation_id": "demo.other"}], "rule_duplicate_id"),
    ],
    ids=["schema", "compile"],
)
@pytest.mark.parametrize("runtime", ["claude", "codex", "opencode"])
def test_hook_run_session_start_reports_a_rule_failure_as_one_typed_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rules: list[object], code: str, runtime: str
) -> None:
    root = _broken_repo(tmp_path, monkeypatch, rules)
    exit_code, out = _invoke(root, runtime, "S-3")
    assert exit_code == 0, out
    report = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(report.splitlines()) == 1
    assert report.startswith("eawf rules: the rule projections could not be checked (")
    assert f"({code}: " in report
    assert f"`{REFRESH_COMMAND}`" in report
    assert "Error(" not in report
    exit_code, out = _invoke(root, runtime, "S-3")
    assert out.strip() == ""


def test_hook_run_session_start_envelope_carries_a_rule_failure_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _broken_repo(tmp_path, monkeypatch, [{**_RULE, "zone": "nowhere"}])
    exit_code, out = _invoke(root, "generic", "S-4")
    assert exit_code == 0, out
    results = json.loads(out)["body"]["results"]
    (row,) = [r for r in results if r["name"] == "rules.projection_staleness"]
    assert row["raised"] is False
    assert "rule_source_schema" in row["output"]
