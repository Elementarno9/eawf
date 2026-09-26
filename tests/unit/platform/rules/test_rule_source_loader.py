"""Unit tests for the rule record and the ``.ea/rules.yaml`` loader."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.platform.rules import (
    AuthoredRule,
    RuleRecord,
    RuleSourceAbsolutePathError,
    RuleSourceError,
    RuleSourceIdentity,
    RuleSourceIncludeError,
    RuleSourceLeakError,
    RuleSourceMissingError,
    RuleSourceNamespaceError,
    RuleSourcePathError,
    RuleSourceSchemaError,
    RuleSourceSymlinkEscapeError,
    RuleSourceTraversalError,
    load_rule_source,
)

# Leak fixtures are assembled at runtime so the literal shapes never appear in
# a committed line the secret and path scanners read.
MACOS_HOME = "/" + "Users" + "/" + "alice"
ANTHROPIC_SHAPE = "sk-" + "ant-" + "A1" * 12
GITHUB_SHAPE = "gh" + "p_" + "A" * 36

_DIGEST = "sha256:" + "0" * 64


def _rule(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": "repo.commit-prefix",
        "obligation_id": "commit.prefix",
        "revision": 1,
        "title": "Write wave commits with a typed subject",
        "zone": "steering",
        "force": "should",
        "effectiveness": "behavioral",
        "instruction": "Start every wave commit subject with a conventional type.",
        "verification": {"method": "review"},
    }
    body.update(overrides)
    return body


def _write_source(repo: Path, document: dict[str, Any] | str) -> Path:
    source = repo / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    text = document if isinstance(document, str) else yaml.safe_dump(document)
    source.write_text(text, encoding="utf-8")
    return source


def _doc(*rules: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"schema_version": 1, "rules": list(rules), **extra}


def _identity() -> RuleSourceIdentity:
    return RuleSourceIdentity(kind="repository", locator=".ea/rules.yaml", digest=_DIGEST)


# ---- the record --------------------------------------------------------------


def test_rule_record_unknown_field_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuleRecord.model_validate({**_rule(), "source": _identity(), "owner": "someone"})


def test_authored_rule_self_declared_source_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AuthoredRule.model_validate({**_rule(), "source": _identity().model_dump()})


def test_rule_record_is_frozen() -> None:
    record = RuleRecord.model_validate({**_rule(), "source": _identity()})
    with pytest.raises(ValidationError, match="frozen"):
        record.title = "Changed"


def test_rule_record_title_at_72_chars_accepted() -> None:
    rule = AuthoredRule.model_validate(_rule(title="x" * 72))
    assert len(rule.title) == 72


def test_rule_record_title_at_73_chars_rejected() -> None:
    with pytest.raises(ValidationError, match="at most 72"):
        AuthoredRule.model_validate(_rule(title="x" * 73))


def test_rule_record_title_trailing_period_rejected() -> None:
    with pytest.raises(ValidationError, match="period"):
        AuthoredRule.model_validate(_rule(title="Write tests."))


def test_rule_record_instruction_bounded_at_320_chars() -> None:
    assert AuthoredRule.model_validate(_rule(instruction="y" * 320))
    with pytest.raises(ValidationError, match="at most 320"):
        AuthoredRule.model_validate(_rule(instruction="y" * 321))


def test_rule_record_empty_instruction_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        AuthoredRule.model_validate(_rule(instruction="   "))


def test_rule_record_authoritative_effectiveness_rejected() -> None:
    with pytest.raises(ValidationError, match="effectiveness"):
        AuthoredRule.model_validate(_rule(effectiveness="authoritative"))


def test_rule_record_unqualified_rule_id_rejected() -> None:
    with pytest.raises(ValidationError, match="rule_id"):
        AuthoredRule.model_validate(_rule(rule_id="commit-prefix"))


def test_rule_record_zero_revision_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        AuthoredRule.model_validate(_rule(revision=0))


def test_rule_record_list_revision_rejected() -> None:
    with pytest.raises(ValidationError, match="revision"):
        AuthoredRule.model_validate(_rule(revision=[1]))


def test_rule_record_structural_without_enforcement_rejected() -> None:
    with pytest.raises(ValidationError, match="enforcement_ref"):
        AuthoredRule.model_validate(_rule(verification={"method": "structural"}))


def test_rule_record_structural_with_enforcement_accepted() -> None:
    rule = AuthoredRule.model_validate(
        _rule(verification={"method": "structural"}, enforcement_ref="eawf.lint.commit_prefix")
    )
    assert rule.enforcement_ref == "eawf.lint.commit_prefix"


def test_rule_record_keeps_instruction_rationale_procedure_verification_separate() -> None:
    rule = AuthoredRule.model_validate(
        _rule(
            rationale="Typed subjects keep the changelog derivable.",
            procedure_ref="docs/rules/commit-prefix.md",
            verification={"method": "registered_check", "check_refs": ["eawf.lint.commit"]},
        )
    )
    assert rule.instruction.startswith("Start every")
    assert rule.rationale == "Typed subjects keep the changelog derivable."
    assert rule.procedure_ref == "docs/rules/commit-prefix.md"
    assert rule.verification.check_refs == ("eawf.lint.commit",)
    assert rule.audience == "interactive_harness"


# ---- loader: accepted sources ------------------------------------------------


def test_load_rule_source_empty_rules_returns_empty_tuple(tmp_path: Path) -> None:
    _write_source(tmp_path, {"schema_version": 1})
    loaded = load_rule_source(tmp_path)
    assert loaded.rules == ()
    assert loaded.modules == ()


def test_load_rule_source_single_rule_stamps_source_identity(tmp_path: Path) -> None:
    source = _write_source(tmp_path, _doc(_rule(), modules=["eawf.craft.commit"]))
    loaded = load_rule_source(tmp_path)
    digest = f"sha256:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    assert loaded.source == RuleSourceIdentity(
        kind="repository", locator=".ea/rules.yaml", digest=digest
    )
    assert loaded.modules == ("eawf.craft.commit",)
    assert [rule.rule_id for rule in loaded.rules] == ["repo.commit-prefix"]
    assert loaded.rules[0].source == loaded.source


def test_load_rule_source_keeps_authored_order(tmp_path: Path) -> None:
    second = _rule(rule_id="repo.a-second", obligation_id="second")
    _write_source(tmp_path, _doc(_rule(), second))
    loaded = load_rule_source(tmp_path)
    assert [rule.rule_id for rule in loaded.rules] == ["repo.commit-prefix", "repo.a-second"]


def test_load_rule_source_accepts_glob_registered_ref_and_prose_url(tmp_path: Path) -> None:
    rule = _rule(
        scope={"paths": ["src/eawf/**/*.py"], "roles": ["executor"]},
        procedure_ref="ref:eawf.procedures.commit",
        rationale="See https://www.conventionalcommits.org for the grammar",
    )
    _write_source(tmp_path, _doc(rule))
    loaded = load_rule_source(tmp_path)
    assert loaded.rules[0].scope.paths == ("src/eawf/**/*.py",)


def test_load_rule_source_accepts_in_repo_symlink(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "docs")
    _write_source(tmp_path, _doc(_rule(procedure_ref="alias/commit.md")))
    assert load_rule_source(tmp_path).rules[0].procedure_ref == "alias/commit.md"


# ---- loader: refusals --------------------------------------------------------


def test_load_rule_source_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RuleSourceMissingError):
        load_rule_source(tmp_path)


@pytest.mark.parametrize(
    ("text", "match"),
    [("schema_version: [1\n", "not valid YAML"), ("- a\n- b\n", "YAML mapping")],
)
def test_load_rule_source_non_mapping_raises_schema_error(
    tmp_path: Path, text: str, match: str
) -> None:
    _write_source(tmp_path, text)
    with pytest.raises(RuleSourceSchemaError, match=match):
        load_rule_source(tmp_path)


def test_load_rule_source_unknown_field_raises_schema_error(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(owner="someone")))
    with pytest.raises(RuleSourceSchemaError, match="extra_forbidden") as info:
        load_rule_source(tmp_path)
    assert isinstance(info.value.__cause__, ValidationError)


def test_load_rule_source_unknown_schema_version_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, {"schema_version": 2})
    with pytest.raises(RuleSourceSchemaError, match="schema_version"):
        load_rule_source(tmp_path)


def test_load_rule_source_parent_traversal_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(scope={"paths": ["docs/../../etc/passwd"]})))
    with pytest.raises(RuleSourceTraversalError, match=r"rules\[0\]\.scope\.paths\[0\]"):
        load_rule_source(tmp_path)


def test_load_rule_source_traversal_in_procedure_ref_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(procedure_ref="../sibling/rules.md")))
    with pytest.raises(RuleSourceTraversalError, match="procedure_ref"):
        load_rule_source(tmp_path)


@pytest.mark.parametrize("path", ["/etc/passwd", "~/notes.md", "C:/repo/notes.md"])
def test_load_rule_source_absolute_path_raises(tmp_path: Path, path: str) -> None:
    _write_source(tmp_path, _doc(_rule(procedure_ref=path)))
    with pytest.raises(RuleSourceAbsolutePathError, match="absolute"):
        load_rule_source(tmp_path)


def test_load_rule_source_home_anchored_path_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(scope={"paths": [f"{MACOS_HOME}/repo/src"]})))
    with pytest.raises(RuleSourceError) as info:
        load_rule_source(tmp_path)
    assert MACOS_HOME not in str(info.value)


@pytest.mark.parametrize("path", ["./docs/a.md", "docs//a.md", "docs\\a.md", "docs/./a.md"])
def test_load_rule_source_unnormalized_path_raises(tmp_path: Path, path: str) -> None:
    _write_source(tmp_path, _doc(_rule(procedure_ref=path)))
    with pytest.raises(RuleSourcePathError):
        load_rule_source(tmp_path)


def test_load_rule_source_symlink_escape_in_path_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    repo.mkdir()
    (repo / "docs").symlink_to(outside)
    _write_source(repo, _doc(_rule(scope={"paths": ["docs/**/*.md"]})))
    with pytest.raises(RuleSourceSymlinkEscapeError, match="symlink"):
        load_rule_source(repo)


def test_load_rule_source_symlinked_source_file_outside_repo_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    foreign = tmp_path / "foreign-rules.yaml"
    foreign.write_text(yaml.safe_dump(_doc(_rule())), encoding="utf-8")
    (repo / ".ea" / "rules.yaml").symlink_to(foreign)
    with pytest.raises(RuleSourceSymlinkEscapeError, match=r"rules\.yaml"):
        load_rule_source(repo)


@pytest.mark.parametrize("token", [ANTHROPIC_SHAPE, GITHUB_SHAPE])
def test_load_rule_source_api_key_shaped_value_raises(tmp_path: Path, token: str) -> None:
    _write_source(tmp_path, _doc(_rule(rationale=f"Authenticate with {token} first")))
    with pytest.raises(RuleSourceLeakError, match=r"rules\[0\]\.rationale \(token\)") as info:
        load_rule_source(tmp_path)
    assert token not in str(info.value)


def test_load_rule_source_home_path_in_prose_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(instruction=f"Read {MACOS_HOME}/notes first")))
    with pytest.raises(RuleSourceLeakError, match="home_path"):
        load_rule_source(tmp_path)


def test_load_rule_source_personal_email_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(rationale="Ask carol" + "@" + "corp-mail.io")))
    with pytest.raises(RuleSourceLeakError, match="email"):
        load_rule_source(tmp_path)


@pytest.mark.parametrize("key", ["include", "imports", "extends", "$ref"])
def test_load_rule_source_include_directive_raises(tmp_path: Path, key: str) -> None:
    _write_source(tmp_path, _doc(_rule(), **{key: "rules/extra.yaml"}))
    with pytest.raises(RuleSourceIncludeError, match="include directives"):
        load_rule_source(tmp_path)


@pytest.mark.parametrize(
    "value", ["https://example.com/rules.yaml", "git@example.com:org/rules.git"]
)
def test_load_rule_source_remote_reference_raises(tmp_path: Path, value: str) -> None:
    _write_source(tmp_path, _doc(_rule(procedure_ref=value)))
    with pytest.raises(RuleSourceIncludeError, match="remote reference"):
        load_rule_source(tmp_path)


def test_load_rule_source_foreign_namespace_raises(tmp_path: Path) -> None:
    _write_source(tmp_path, _doc(_rule(rule_id="eawf.core.commit-prefix")))
    with pytest.raises(RuleSourceNamespaceError, match="repo"):
        load_rule_source(tmp_path)


def test_rule_source_error_codes_are_distinct() -> None:
    classes = [
        RuleSourceMissingError,
        RuleSourceSchemaError,
        RuleSourceIncludeError,
        RuleSourceLeakError,
        RuleSourceNamespaceError,
        RuleSourcePathError,
        RuleSourceAbsolutePathError,
        RuleSourceTraversalError,
        RuleSourceSymlinkEscapeError,
    ]
    codes = {cls.code for cls in classes}
    assert len(codes) == len(classes)
    assert all(issubclass(cls, RuleSourceError) for cls in classes)
