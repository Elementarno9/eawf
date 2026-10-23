"""Hard rule exceptions lead their rules, and the STE prose hook is advisory.

An A/B run with a small model missed an exception buried after a semicolon or
at the end of a rule, whichever prose style the rule used. Each hard exception
therefore opens its rule as its own ``Never`` sentence, both in the selected
rule set and in the rendered full policy.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from eawf.platform.rules import load_rule_source
from eawf.platform.rules.modules import select_rule_modules
from eawf.platform.rules.render import POLICY_TARGET, plan_rule_projections

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Each hard exception and the rule it must open.
_HARD_EXCEPTIONS = {
    "eawf.core.integrity.deletion": (
        "Never delete schemas, golden fixtures, LICENSE, CHANGELOG.md or uncommitted files."
    ),
    "eawf.core.state.daemon-mutator": "Never write state directly while a daemon runs.",
    "eawf.core.state.scope-revisability": "Never rewrite started or completed work.",
    "eawf.core.lifecycle.task-verification-scope": (
        "Never shorten the full test run for a schema-migration or security-scoped change."
    ),
}


def test_hard_exceptions_lead_their_rules() -> None:
    records = select_rule_modules(load_rule_source(_REPO_ROOT)).records
    instructions = {str(record.rule_id): record.instruction for record in records}
    policy = dict(plan_rule_projections(_REPO_ROOT).outputs)[POLICY_TARGET]
    for rule_id, sentence in _HARD_EXCEPTIONS.items():
        assert instructions[rule_id].startswith(f"{sentence} "), rule_id
        assert f".** {sentence} " in policy, rule_id


def _ste_hook() -> dict[str, object]:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    return next(hook for hook in hooks if hook["id"] == "ste-prose")


def _hook_argv(hook: dict[str, object]) -> list[str]:
    argv = shlex.split(str(hook["entry"]))
    assert argv[:3] == ["uv", "run", "python"]
    return [sys.executable, *argv[3:]]


@pytest.mark.parametrize(
    "path",
    [
        "src/eawf/platform/rules/data/modules/eawf.core.state.yaml",
        "src/eawf/platform/profiles/data/core.yaml",
        "src/eawf/surfaces/render/skills/registry.py",
    ],
)
def test_ste_hook_watches_rule_profile_and_skill_sources(path: str) -> None:
    assert re.search(str(_ste_hook()["files"]), path)


def test_ste_hook_is_advisory(tmp_path: Path) -> None:
    hook = _ste_hook()
    assert hook["verbose"] is True
    assert hook["pass_filenames"] is False
    long_rule = tmp_path / "rule.yaml"
    long_rule.write_text(f"instruction: {' '.join(['Run'] * 30)}.\n", encoding="utf-8")
    argv = _hook_argv(hook)
    for extra in ([], [str(long_rule)]):
        run = subprocess.run(
            [*argv, *extra], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
        )
        assert run.returncode == 0, run.stderr
    assert "instruction-length" in run.stdout
