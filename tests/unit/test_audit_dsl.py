"""Unit tests for :mod:`eawf.audit_dsl` — DSL loader + registry + runner.

Coverage matrix (one boundary + error pair per check kind, plus
loader paths):

* loader: missing-file, malformed-yaml, schema-mismatch, valid load.
* file_exists: present, absent, missing-arg.
* path_glob_nonempty: matches, no matches, missing-arg.
* regex_in_file: match, no-match, missing-file, missing-arg.
* state_field_equals: equal, mismatch, missing-segment, non-json
  state file, missing-arg, custom state_path.
* command_exit_zero: zero, non-zero, missing-binary, missing-arg.
* criterion_in_diff: match (file scope), match (dir scope), no-match,
  missing-criterion, missing-pattern, missing-scopes, bad-regex.
* run_checks: cwd resolution, golden sample.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.audit_dsl import (
    CHECK_REGISTRY,
    CheckFile,
    CheckResult,
    CheckSpec,
    load_spec,
    run_checks,
)
from eawf.cli.errors import InvalidInput

# ---- helpers ---------------------------------------------------------------


def _write_yaml(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "checks.yaml"
    target.write_text(body, encoding="utf-8")
    return target


def _run_one(kind: str, name: str, args: dict[str, Any], cwd: Path) -> CheckResult:
    spec = CheckSpec(kind=kind, name=name, args=args)  # type: ignore[arg-type]
    return CHECK_REGISTRY[kind](spec, cwd.resolve())


# ---- loader ----------------------------------------------------------------


def test_load_spec_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(InvalidInput, match="not found"):
        load_spec(tmp_path / "nope.yaml")


def test_load_spec_invalid_yaml_raises(tmp_path: Path) -> None:
    spec_path = _write_yaml(tmp_path, "schema_version: '1.0'\nchecks: [\n")
    with pytest.raises(InvalidInput, match="not valid yaml"):
        load_spec(spec_path)


def test_load_spec_empty_document_raises(tmp_path: Path) -> None:
    spec_path = _write_yaml(tmp_path, "")
    with pytest.raises(InvalidInput, match="empty"):
        load_spec(spec_path)


def test_load_spec_unknown_kind_raises(tmp_path: Path) -> None:
    body = """\
schema_version: "1.0"
checks:
  - kind: not_a_real_kind
    name: bogus
    args: {}
"""
    spec_path = _write_yaml(tmp_path, body)
    with pytest.raises(InvalidInput, match="schema mismatch"):
        load_spec(spec_path)


def test_load_spec_wrong_schema_version_raises(tmp_path: Path) -> None:
    body = """\
schema_version: "2.0"
checks: []
"""
    spec_path = _write_yaml(tmp_path, body)
    with pytest.raises(InvalidInput, match="schema mismatch"):
        load_spec(spec_path)


def test_load_spec_extra_top_level_key_rejected(tmp_path: Path) -> None:
    body = """\
schema_version: "1.0"
checks: []
extra_key: nope
"""
    spec_path = _write_yaml(tmp_path, body)
    with pytest.raises(InvalidInput, match="schema mismatch"):
        load_spec(spec_path)


def test_load_spec_returns_checkspec_list(tmp_path: Path) -> None:
    body = """\
schema_version: "1.0"
checks:
  - kind: file_exists
    name: x
    args: {path: somewhere.txt}
"""
    spec_path = _write_yaml(tmp_path, body)
    specs = load_spec(spec_path)
    assert len(specs) == 1
    assert specs[0].kind == "file_exists"
    assert specs[0].name == "x"


def test_load_spec_golden_sample_validates() -> None:
    """The committed golden yaml is a valid DSL document."""
    sample = Path(__file__).resolve().parents[1] / "golden" / "audit_dsl" / "sample.yaml"
    specs = load_spec(sample)
    kinds = [s.kind for s in specs]
    assert kinds == [
        "file_exists",
        "regex_in_file",
        "state_field_equals",
        "path_glob_nonempty",
        "command_exit_zero",
    ]


# ---- file_exists -----------------------------------------------------------


def test_file_exists_pass(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("hi", encoding="utf-8")
    result = _run_one("file_exists", "marker", {"path": "marker.txt"}, tmp_path)
    assert result.passed is True
    assert "marker.txt" in (result.details or "")


def test_file_exists_fail_when_absent(tmp_path: Path) -> None:
    result = _run_one("file_exists", "marker", {"path": "missing.txt"}, tmp_path)
    assert result.passed is False


def test_file_exists_missing_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or non-str arg 'path'"):
        _run_one("file_exists", "marker", {}, tmp_path)


# ---- path_glob_nonempty ----------------------------------------------------


def test_path_glob_nonempty_pass(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("# py", encoding="utf-8")
    (tmp_path / "b.py").write_text("# py", encoding="utf-8")
    result = _run_one("path_glob_nonempty", "py", {"pattern": "*.py"}, tmp_path)
    assert result.passed is True
    assert "matches=2" in (result.details or "")


def test_path_glob_nonempty_fail(tmp_path: Path) -> None:
    result = _run_one("path_glob_nonempty", "py", {"pattern": "*.go"}, tmp_path)
    assert result.passed is False
    assert "matches=0" in (result.details or "")


def test_path_glob_nonempty_missing_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or non-str arg 'pattern'"):
        _run_one("path_glob_nonempty", "py", {}, tmp_path)


# ---- regex_in_file ---------------------------------------------------------


def test_regex_in_file_match(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text("## v0.2.0\n- P13 wave\n", encoding="utf-8")
    result = _run_one(
        "regex_in_file",
        "changelog",
        {"path": "CHANGELOG.md", "pattern": r"P13"},
        tmp_path,
    )
    assert result.passed is True


def test_regex_in_file_no_match(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("hello\n", encoding="utf-8")
    result = _run_one(
        "regex_in_file",
        "x",
        {"path": "x.md", "pattern": r"world"},
        tmp_path,
    )
    assert result.passed is False


def test_regex_in_file_missing_file(tmp_path: Path) -> None:
    result = _run_one(
        "regex_in_file",
        "x",
        {"path": "missing.md", "pattern": "."},
        tmp_path,
    )
    assert result.passed is False
    assert "not found" in (result.details or "")


def test_regex_in_file_missing_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or non-str arg 'pattern'"):
        _run_one("regex_in_file", "x", {"path": "x.md"}, tmp_path)


# ---- state_field_equals ----------------------------------------------------


def test_state_field_equals_pass(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "schema",
        {"field": "schema_version", "value": "1.0"},
        tmp_path,
    )
    assert result.passed is True


def test_state_field_equals_mismatch(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text(json.dumps({"schema_version": "2.0"}), encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "schema",
        {"field": "schema_version", "value": "1.0"},
        tmp_path,
    )
    assert result.passed is False
    assert "expected=" in (result.details or "")


def test_state_field_equals_nested_dotpath(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text(json.dumps({"project": {"code": "EAWF"}}), encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "proj",
        {"field": "project.code", "value": "EAWF"},
        tmp_path,
    )
    assert result.passed is True


def test_state_field_equals_missing_segment_fails_cleanly(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "nope",
        {"field": "no.such.path", "value": "x"},
        tmp_path,
    )
    assert result.passed is False
    assert "unreachable" in (result.details or "")


def test_state_field_equals_missing_state_file(tmp_path: Path) -> None:
    result = _run_one(
        "state_field_equals",
        "schema",
        {"field": "schema_version", "value": "1.0"},
        tmp_path,
    )
    assert result.passed is False
    assert "not found" in (result.details or "")


def test_state_field_equals_non_json_state_file(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    (ea_dir / "state.json").write_text("not json", encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "schema",
        {"field": "schema_version", "value": "1.0"},
        tmp_path,
    )
    assert result.passed is False
    assert "not valid JSON" in (result.details or "")


def test_state_field_equals_custom_state_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"k": "v"}), encoding="utf-8")
    result = _run_one(
        "state_field_equals",
        "custom",
        {"field": "k", "value": "v", "state_path": "custom.json"},
        tmp_path,
    )
    assert result.passed is True


def test_state_field_equals_missing_value_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing required arg 'value'"):
        _run_one(
            "state_field_equals",
            "x",
            {"field": "k"},
            tmp_path,
        )


# ---- command_exit_zero -----------------------------------------------------


def test_command_exit_zero_pass(tmp_path: Path) -> None:
    result = _run_one(
        "command_exit_zero",
        "echo",
        {"argv": [sys.executable, "-c", "import sys; sys.exit(0)"]},
        tmp_path,
    )
    assert result.passed is True
    assert "returncode=0" in (result.details or "")


def test_command_exit_zero_nonzero(tmp_path: Path) -> None:
    result = _run_one(
        "command_exit_zero",
        "nonzero",
        {"argv": [sys.executable, "-c", "import sys; sys.exit(7)"]},
        tmp_path,
    )
    assert result.passed is False
    assert "returncode=7" in (result.details or "")


def test_command_exit_zero_missing_binary(tmp_path: Path) -> None:
    result = _run_one(
        "command_exit_zero",
        "ghost",
        {"argv": ["definitely-not-a-real-binary-eawf-w04"]},
        tmp_path,
    )
    assert result.passed is False
    assert "not executable" in (result.details or "")


def test_command_exit_zero_missing_argv_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv"):
        _run_one("command_exit_zero", "x", {}, tmp_path)


def test_command_exit_zero_empty_argv_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="argv"):
        _run_one("command_exit_zero", "x", {"argv": []}, tmp_path)


# ---- criterion_in_diff -----------------------------------------------------


def test_criterion_in_diff_match_file_scope(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("# guard kept\nx = 1\n", encoding="utf-8")
    result = _run_one(
        "criterion_in_diff",
        "c1",
        {"criterion": "guard kept", "pattern": "guard kept", "file_scopes": ["mod.py"]},
        tmp_path,
    )
    assert result.passed is True
    assert "guard kept" in (result.details or "")


def test_criterion_in_diff_match_dir_scope(tmp_path: Path) -> None:
    pkg = tmp_path / "src" / "eawf"
    pkg.mkdir(parents=True)
    (pkg / "deep.py").write_text("MARKER_TOKEN = True\n", encoding="utf-8")
    result = _run_one(
        "criterion_in_diff",
        "c1",
        {"criterion": "marker present", "pattern": "MARKER_TOKEN", "file_scopes": ["src/eawf"]},
        tmp_path,
    )
    assert result.passed is True


def test_criterion_in_diff_no_match_fails(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_one(
        "criterion_in_diff",
        "c1",
        {"criterion": "missing thing", "pattern": "absent_token", "file_scopes": ["mod.py"]},
        tmp_path,
    )
    assert result.passed is False
    assert "unmet criterion" in (result.details or "")
    assert "missing thing" in (result.details or "")


def test_criterion_in_diff_missing_criterion_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or empty str arg 'criterion'"):
        _run_one(
            "criterion_in_diff",
            "c1",
            {"pattern": "x", "file_scopes": ["mod.py"]},
            tmp_path,
        )


def test_criterion_in_diff_missing_pattern_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or empty str arg 'pattern'"):
        _run_one(
            "criterion_in_diff",
            "c1",
            {"criterion": "x", "file_scopes": ["mod.py"]},
            tmp_path,
        )


def test_criterion_in_diff_missing_scopes_arg_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        _run_one(
            "criterion_in_diff",
            "c1",
            {"criterion": "x", "pattern": "y", "file_scopes": []},
            tmp_path,
        )


def test_criterion_in_diff_bad_regex_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a valid regex"):
        _run_one(
            "criterion_in_diff",
            "c1",
            {"criterion": "x", "pattern": "[unterminated", "file_scopes": ["mod.py"]},
            tmp_path,
        )


def test_criterion_in_diff_absent_scope_fails_cleanly(tmp_path: Path) -> None:
    """A file_scope that does not exist → not found, not a crash."""
    result = _run_one(
        "criterion_in_diff",
        "c1",
        {"criterion": "x", "pattern": "x", "file_scopes": ["does_not_exist.py"]},
        tmp_path,
    )
    assert result.passed is False
    assert "0 file(s) searched" in (result.details or "")


# ---- run_checks ------------------------------------------------------------


def test_run_checks_cwd_defaults_to_pathcwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``cwd`` kwarg, the runner resolves against ``Path.cwd``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "marker.txt").write_text("y", encoding="utf-8")
    specs = [
        CheckSpec(kind="file_exists", name="m", args={"path": "marker.txt"}),
    ]
    results = run_checks(specs)
    assert len(results) == 1
    assert results[0].passed is True


def test_run_checks_preserves_order(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("", encoding="utf-8")
    specs = [
        CheckSpec(kind="file_exists", name="first", args={"path": "a"}),
        CheckSpec(kind="file_exists", name="second", args={"path": "missing"}),
    ]
    results = run_checks(specs, cwd=tmp_path)
    assert [r.name for r in results] == ["first", "second"]
    assert [r.passed for r in results] == [True, False]


def test_run_checks_golden_sample_smoke(tmp_path: Path) -> None:
    """Smoke run of the golden yaml against a hand-built mini-repo."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("P13 line\n", encoding="utf-8")
    src = tmp_path / "src" / "eawf"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "state.json").write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
    sample = Path(__file__).resolve().parents[1] / "golden" / "audit_dsl" / "sample.yaml"
    specs = load_spec(sample)
    results = run_checks(specs, cwd=tmp_path)
    assert all(r.passed for r in results), [(r.name, r.passed, r.details) for r in results]


# ---- model / surface guards ------------------------------------------------


def test_checkspec_extra_field_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckSpec(  # type: ignore[call-arg]
            kind="file_exists",
            name="x",
            args={},
            extra_field="nope",
        )


def test_checkresult_extra_field_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckResult(  # type: ignore[call-arg]
            name="x",
            kind="file_exists",
            passed=True,
            extra_field="nope",
        )


def test_checkfile_extra_field_forbidden() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CheckFile.model_validate(
            {
                "schema_version": "1.0",
                "checks": [],
                "stray": 1,
            }
        )


def test_check_registry_keys_match_kind_literal() -> None:
    """The registry exactly covers the CheckKind literal — no drift."""
    from typing import get_args

    from eawf.audit_dsl.models import CheckKind

    assert set(CHECK_REGISTRY.keys()) == set(get_args(CheckKind))
