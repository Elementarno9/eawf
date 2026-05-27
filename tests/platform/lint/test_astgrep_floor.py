"""Tests for the generated ast-grep reviewdog floor."""

from __future__ import annotations

from pathlib import Path

import yaml

from eawf.platform.lint.tools import astgrep_floor

REPO_ROOT = Path(__file__).parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "reviewdog.yml"


def test_floor_rules_are_deterministic_polyglot_floor() -> None:
    rules = astgrep_floor.FLOOR_RULES

    assert [rule.id for rule in rules] == sorted(rule.id for rule in rules)
    assert {rule.language for rule in rules} == {
        "JavaScript",
        "Python",
        "TypeScript",
        "Yaml",
    }
    assert {rule.severity for rule in rules} == {"warning"}
    assert all("Mode A floor-only" in rule.note for rule in rules)


def test_render_config_encodes_mode_a_floor_policy() -> None:
    config = yaml.safe_load(astgrep_floor.render_config(".ast-grep-floor/rules"))
    policy = astgrep_floor.REVIEWDOG_FLOOR_POLICY

    assert config == {"ruleDirs": [".ast-grep-floor/rules"]}
    assert policy.ceremony_mode == "A"
    assert policy.floor_only is True
    assert policy.reporter == "github-pr-review"
    assert policy.fail_level == "none"
    assert policy.level == "warning"


def test_write_floor_emits_repo_relative_generated_files(tmp_path: Path) -> None:
    paths = astgrep_floor.write_floor(
        tmp_path,
        output_dir=Path(".ast-grep-floor"),
        config_path=Path("sgconfig.yml"),
    )

    assert paths.config_path == tmp_path / "sgconfig.yml"
    assert paths.rule_dir == tmp_path / ".ast-grep-floor" / "rules"
    assert [path.name for path in paths.rule_paths] == [
        f"{rule.id}.yml" for rule in astgrep_floor.FLOOR_RULES
    ]
    config_text = paths.config_path.read_text(encoding="utf-8")
    assert astgrep_floor.GENERATED_SENTINEL in config_text
    assert str(tmp_path) not in config_text

    rule_body = yaml.safe_load(paths.rule_paths[0].read_text(encoding="utf-8"))
    assert rule_body["id"] == astgrep_floor.FLOOR_RULES[0].id
    assert rule_body["rule"]["pattern"] == astgrep_floor.FLOOR_RULES[0].pattern


def test_write_floor_refuses_non_generated_config(tmp_path: Path) -> None:
    config_path = tmp_path / "sgconfig.yml"
    config_path.write_text("ruleDirs: []\n", encoding="utf-8")

    try:
        astgrep_floor.write_floor(tmp_path, config_path=Path("sgconfig.yml"))
    except FileExistsError as exc:
        assert "refusing to overwrite non-generated file" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")


def test_reviewdog_workflow_runs_visible_floor_on_pull_requests() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    job = workflow["jobs"]["ast-grep-floor"]
    reviewdog_step = job["steps"][-1]

    assert "pull_request:" in raw
    assert "paths:" not in raw
    assert job["name"] == "ast-grep visible floor"
    assert workflow["permissions"]["pull-requests"] == "write"
    assert reviewdog_step["uses"] == "reviewdog/action-ast-grep@v1.56.0"
    assert reviewdog_step["with"]["reporter"] == astgrep_floor.REVIEWDOG_FLOOR_POLICY.reporter
    assert reviewdog_step["with"]["level"] == astgrep_floor.REVIEWDOG_FLOOR_POLICY.level
    assert reviewdog_step["with"]["fail_level"] == astgrep_floor.REVIEWDOG_FLOOR_POLICY.fail_level
    assert reviewdog_step["with"]["sg_config"] == "sgconfig.yml"
    assert reviewdog_step["with"]["sg_flags"] == "--no-ignore hidden"
