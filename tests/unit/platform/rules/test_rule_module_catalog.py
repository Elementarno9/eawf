"""Unit tests for builtin rule module selection and the module index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.platform.rules import (
    RuleModuleCatalogError,
    RuleModuleCopyError,
    RuleModuleDocument,
    RuleModuleEntry,
    RuleModuleSelection,
    RuleSourceError,
    builtin_rule_modules,
    compile_rule_records,
    load_rule_source,
    parse_rule_module,
    registered_enforcement_refs,
    render_module_index,
    select_rule_modules,
)
from eawf.platform.rules.modules import MODULE_BINDING_NOTICE, MODULE_VIEW_COMMAND

_PYTHON = "eawf.craft.python"
_TEST = "eawf.craft.test"


def _write_rules(repo: Path, body: dict[str, Any]) -> Path:
    target = repo / ".ea" / "rules.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump({"schema_version": 1, **body}), encoding="utf-8")
    return repo


def _repo_rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_id": "repo.house-style",
        "obligation_id": "repo.house-style",
        "revision": 1,
        "title": "Keep the changelog current",
        "zone": "steering",
        "force": "should",
        "effectiveness": "behavioral",
        "instruction": "Add a changelog line for every user-visible change.",
        "verification": {"method": "review"},
    }
    rule.update(overrides)
    return rule


def _module(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "module_id": "eawf.craft.sample",
        "version": 1,
        "kind": "catalog",
        "topic": "sample",
        "title": "Sample craft",
        "when_to_use": "editing files under the sample directory",
        "rules": [
            {
                "rule_id": "eawf.craft.sample.one",
                "obligation_id": "craft.sample.one",
                "revision": 1,
                "title": "Keep sample files sorted",
                "zone": "retrievable",
                "force": "should",
                "effectiveness": "behavioral",
                "instruction": "Keep entries in every sample file sorted by name.",
                "scope": {"activities": ["implement"]},
                "verification": {"method": "review"},
            }
        ],
    }
    body.update(overrides)
    return body


def _index_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


def test_render_module_index_removing_reference_removes_exactly_its_row(tmp_path: Path) -> None:
    both = select_rule_modules(
        load_rule_source(_write_rules(tmp_path, {"modules": [_PYTHON, _TEST]}))
    )
    rows_both = _index_rows(render_module_index(both))
    one = select_rule_modules(load_rule_source(_write_rules(tmp_path, {"modules": [_PYTHON]})))
    rows_one = _index_rows(render_module_index(one))

    assert len(rows_both) == 2
    removed = [row for row in rows_both if row not in rows_one]
    assert rows_one == [row for row in rows_both if f"`{_PYTHON}`" in row]
    assert len(removed) == 1 and f"`{_TEST}`" in removed[0]


def test_render_module_index_adding_reference_adds_its_row(tmp_path: Path) -> None:
    before = render_module_index(
        select_rule_modules(load_rule_source(_write_rules(tmp_path, {"modules": [_PYTHON]})))
    )
    after = render_module_index(
        select_rule_modules(load_rule_source(_write_rules(tmp_path, {"modules": [_TEST, _PYTHON]})))
    )
    assert len(_index_rows(after)) == len(_index_rows(before)) + 1
    assert f"`{_TEST}`" not in before and f"`{_TEST}`" in after


def test_render_module_index_entry_carries_topic_scope_count_trigger_and_command(
    tmp_path: Path,
) -> None:
    selection = select_rule_modules(
        load_rule_source(_write_rules(tmp_path, {"modules": [_PYTHON]}))
    )
    module = builtin_rule_modules()[_PYTHON]
    (row,) = _index_rows(render_module_index(selection))

    assert f"{len(module.records)} rules" in row
    assert f"[{', '.join(module.scope)}]" in row
    assert f"load when: {module.document.when_to_use}" in row
    assert f"`{MODULE_VIEW_COMMAND} {_PYTHON}`" in row
    assert f" {module.document.topic} " in row


def test_render_module_index_states_that_loaded_modules_bind() -> None:
    selection = RuleModuleSelection(
        entries=(RuleModuleEntry(reference=_PYTHON, module=builtin_rule_modules()[_PYTHON]),)
    )
    text = render_module_index(selection)
    assert text.startswith(MODULE_BINDING_NOTICE)
    assert "bind for the rest of the activity" in MODULE_BINDING_NOTICE


def test_render_module_index_empty_selection_renders_nothing() -> None:
    assert render_module_index(RuleModuleSelection(entries=())) == ""


def test_render_module_index_single_rule_uses_singular_noun() -> None:
    module = parse_rule_module(yaml.safe_dump(_module()).encode(), where="sample.yaml")
    selection = RuleModuleSelection(
        entries=(RuleModuleEntry(reference="eawf.craft.sample", module=module),)
    )
    (row,) = _index_rows(render_module_index(selection))
    assert " 1 rule;" in row


def test_select_rule_modules_unknown_reference_is_reported_unavailable(tmp_path: Path) -> None:
    loaded = load_rule_source(_write_rules(tmp_path, {"modules": ["eawf.craft.absent", _PYTHON]}))
    selection = select_rule_modules(loaded)
    (row_absent, row_python) = _index_rows(render_module_index(selection))

    assert row_absent.startswith("- `eawf.craft.absent` unavailable: ")
    assert f"`{_PYTHON}`" in row_python
    assert all(record.rule_id.startswith(f"{_PYTHON}.") for record in selection.records)


def test_select_rule_modules_order_and_duplicates_do_not_matter(tmp_path: Path) -> None:
    first = select_rule_modules(
        load_rule_source(_write_rules(tmp_path, {"modules": [_TEST, _PYTHON]}))
    )
    second = select_rule_modules(
        load_rule_source(_write_rules(tmp_path, {"modules": [_PYTHON, _TEST, _PYTHON]}))
    )
    assert first == second
    assert [entry.reference for entry in first.entries] == [_PYTHON, _TEST]


def test_select_rule_modules_no_selection_activates_nothing(tmp_path: Path) -> None:
    selection = select_rule_modules(load_rule_source(_write_rules(tmp_path, {})))
    assert selection.entries == ()
    assert selection.records == ()


def test_select_rule_modules_copied_builtin_instruction_raises_copy_refusal(tmp_path: Path) -> None:
    copied = builtin_rule_modules()[_PYTHON].records[0].instruction
    rules = [_repo_rule(instruction=f"  {copied.upper()}  ")]
    loaded = load_rule_source(_write_rules(tmp_path, {"rules": rules}))

    with pytest.raises(
        RuleModuleCopyError, match=r"copies builtin rule eawf\.craft\.python"
    ) as info:
        select_rule_modules(loaded)
    assert isinstance(info.value, RuleSourceError)
    assert info.value.code == "rule_module_copy"


def test_select_rule_modules_copied_rationale_of_unselected_module_is_refused(
    tmp_path: Path,
) -> None:
    rationale = builtin_rule_modules()[_TEST].records[0].rationale
    assert rationale is not None
    loaded = load_rule_source(
        _write_rules(tmp_path, {"modules": [_PYTHON], "rules": [_repo_rule(rationale=rationale)]})
    )
    with pytest.raises(RuleModuleCopyError, match="rationale"):
        select_rule_modules(loaded)


def test_select_rule_modules_original_repository_prose_is_accepted(tmp_path: Path) -> None:
    loaded = load_rule_source(_write_rules(tmp_path, {"rules": [_repo_rule()]}))
    assert select_rule_modules(loaded).entries == ()


def test_select_rule_modules_records_compile_as_builtin_layer(tmp_path: Path) -> None:
    loaded = load_rule_source(
        _write_rules(tmp_path, {"modules": [_PYTHON, _TEST], "rules": [_repo_rule()]})
    )
    selection = select_rule_modules(loaded)
    graph = compile_rule_records(
        (*selection.records, *loaded.rules),
        modules=loaded.modules,
        enforcement_refs=frozenset(),
    )
    builtin = [rule for rule in graph.rules if rule.record.source.kind == "builtin"]
    assert len(builtin) == len(selection.records)
    assert {rule.record.source.locator for rule in builtin} == {f"{_PYTHON}@1", f"{_TEST}@1"}
    assert not any(rule.protected for rule in builtin)
    assert graph.modules == (_PYTHON, _TEST)


def test_builtin_rule_modules_every_module_compiles_clean() -> None:
    catalog = builtin_rule_modules()
    assert set(catalog) >= {_PYTHON, _TEST, "eawf.craft.markdown", "eawf.craft.plotting"}
    records = [record for module in catalog.values() for record in module.records]
    graph = compile_rule_records(
        records, modules=catalog.keys(), enforcement_refs=registered_enforcement_refs()
    )
    assert len(graph.rules) == len(records)
    for reference, module in catalog.items():
        assert module.document.module_id == reference
        assert module.source.kind == "builtin"
        assert all(record.source == module.source for record in module.records)


def test_builtin_rule_modules_is_read_only_and_idempotent() -> None:
    first = builtin_rule_modules()
    assert builtin_rule_modules() is first
    with pytest.raises(TypeError):
        first[_PYTHON] = first[_TEST]  # type: ignore[index]


def test_parse_rule_module_digest_binds_exact_bytes() -> None:
    raw = yaml.safe_dump(_module()).encode()
    module = parse_rule_module(raw, where="sample.yaml")
    changed = parse_rule_module(raw + b"\n", where="sample.yaml")
    assert module.source.locator == "eawf.craft.sample@1"
    assert module.source.digest != changed.source.digest


@pytest.mark.parametrize(
    "raw",
    [b"- not\n- a mapping\n", b"key: [unclosed\n", b"\xff\xfe"],
)
def test_parse_rule_module_malformed_bytes_raise_catalog_error(raw: bytes) -> None:
    with pytest.raises(RuleModuleCatalogError, match=r"bad\.yaml"):
        parse_rule_module(raw, where="bad.yaml")


def test_rule_module_document_catalog_rule_in_constitution_is_refused() -> None:
    body = _module()
    body["rules"][0]["zone"] = "constitution"
    with pytest.raises(ValidationError, match="constitution"):
        RuleModuleDocument.model_validate(body)


def test_rule_module_document_core_module_may_hold_constitution_rules() -> None:
    body = _module(kind="core")
    body["rules"][0]["zone"] = "constitution"
    body["rules"][0]["scope"] = {}
    assert RuleModuleDocument.model_validate(body).kind == "core"


def test_rule_module_document_unscoped_catalog_rule_is_refused() -> None:
    body = _module()
    body["rules"][0]["scope"] = {"paths": ["src/sample"]}
    with pytest.raises(ValidationError, match="activity or role"):
        RuleModuleDocument.model_validate(body)


def test_rule_module_document_role_scope_satisfies_selector() -> None:
    body = _module()
    body["rules"][0]["scope"] = {"roles": ["reviewer"]}
    assert RuleModuleDocument.model_validate(body).rules[0].scope.roles == ("reviewer",)


def test_rule_module_document_trigger_restating_title_is_refused() -> None:
    with pytest.raises(ValidationError, match="restates the title"):
        RuleModuleDocument.model_validate(_module(when_to_use="when sample craft rules"))


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ("too short", "at least 16"),
        ("x" * 201, "at most 200"),
        ("editing files under the sample directory.", "period"),
    ],
)
def test_rule_module_document_trigger_bounds(trigger: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        RuleModuleDocument.model_validate(_module(when_to_use=trigger))


def test_rule_module_document_missing_trigger_is_refused() -> None:
    body = _module()
    del body["when_to_use"]
    with pytest.raises(ValidationError, match="when_to_use"):
        RuleModuleDocument.model_validate(body)


def test_rule_module_document_rule_outside_module_namespace_is_refused() -> None:
    body = _module()
    body["rules"][0]["rule_id"] = "eawf.craft.other.one"
    with pytest.raises(ValidationError, match=r"must start with 'eawf\.craft\.sample\.'"):
        RuleModuleDocument.model_validate(body)


def test_rule_module_document_non_builtin_module_id_is_refused() -> None:
    body = _module(module_id="repo.sample")
    body["rules"][0]["rule_id"] = "repo.sample.one"
    with pytest.raises(ValidationError, match="must start with 'eawf'"):
        RuleModuleDocument.model_validate(body)


def test_rule_module_document_empty_rules_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        RuleModuleDocument.model_validate(_module(rules=[]))


def test_rule_module_document_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError, match="extra"):
        RuleModuleDocument.model_validate(_module(body="copied prose"))


def test_rule_module_document_wrong_type_is_refused() -> None:
    with pytest.raises(ValidationError):
        RuleModuleDocument.model_validate(_module(topic=5))


def test_rule_module_entry_requires_module_or_reason() -> None:
    with pytest.raises(ValidationError, match="either a module or an unavailable_reason"):
        RuleModuleEntry(reference=_PYTHON)
    with pytest.raises(ValidationError, match="either a module or an unavailable_reason"):
        RuleModuleEntry(
            reference=_PYTHON, module=builtin_rule_modules()[_PYTHON], unavailable_reason="x"
        )
