"""Contract: every field a skill page lists is one ``eawf skill check-report`` accepts.

The Output slot of each shipped page lists the report fields as a table. The
test reads that table back off the rendered page, builds a report from it and
validates the report through the catalog entry's ``validate_report``, the
library call the ``check-report`` verb makes. A page that asks for a field the
validator rejects, or marks a field optional that the validator requires,
reds here.
"""

from __future__ import annotations

import re
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from eawf.workflow.skills.bodies.chassis import report_fields_table, shipped_skill_page
from eawf.workflow.skills.catalog import SKILL_CATALOG, SkillCatalogEntry

_CATALOG_IDS = tuple(entry.skill_id for entry in SKILL_CATALOG.entries)
_ROW = re.compile(r"^\| `([^`]+)` \| (.+?) \| (yes|no) \| .*\|$", re.MULTILINE)
_LITERAL = re.compile(r'`"([^"]+)"`')

#: A field no page lists and every report model refuses.
_UNLISTED_FIELD = "findings"


def _entry(skill_id: str) -> SkillCatalogEntry:
    entry = SKILL_CATALOG.entry(skill_id)
    assert entry is not None
    return entry


def _listed_fields(page: str) -> list[tuple[str, str, bool]]:
    output = page[page.index("## 6. Output\n") :]
    return [(path, kind, required == "yes") for path, kind, required in _ROW.findall(output)]


def _value(kind: str) -> object:
    if kind.startswith("list of "):
        return [_value(kind.removeprefix("list of "))]
    literal = _LITERAL.search(kind)
    if literal is not None:
        return literal.group(1)
    return {"string": "x", "integer": 1, "boolean": True, "object": {}}[kind]


def _place(report: dict[str, object], path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    node: object = report
    for part in parents:
        assert isinstance(node, dict)
        node = node[part.removesuffix("[]")]
        if part.endswith("[]"):
            assert isinstance(node, list)
            node = node[0]
    assert isinstance(node, dict)
    node[leaf] = value


def _report(fields: list[tuple[str, str, bool]], *, skip: str | None = None) -> dict[str, object]:
    report: dict[str, object] = {}
    for path, kind, _required in fields:
        if skip is not None and (path == skip or path.startswith(f"{skip}.")):
            continue
        _place(report, path, _value(kind))
    return report


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_every_listed_report_field_is_accepted_by_check_report(skill_id: str) -> None:
    entry = _entry(skill_id)
    fields = _listed_fields(shipped_skill_page(entry))

    assert [path for path, _kind, _required in fields][:2] == ["skill_id", "outcome"]
    validated = entry.validate_report(_report(fields))
    assert validated.skill_id == skill_id
    assert validated.outcome == entry.output.terminal_outcomes[0]


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_a_field_the_page_does_not_list_is_refused(skill_id: str) -> None:
    entry = _entry(skill_id)
    fields = _listed_fields(shipped_skill_page(entry))
    report = _report(fields)
    report[_UNLISTED_FIELD] = "x"

    assert _UNLISTED_FIELD not in {path for path, _kind, _required in fields}
    with pytest.raises(ValidationError, match=_UNLISTED_FIELD):
        entry.validate_report(report)


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_required_marks_match_the_validator(skill_id: str) -> None:
    entry = _entry(skill_id)
    fields = _listed_fields(shipped_skill_page(entry))

    for path, _kind, required in fields:
        if "." in path:
            continue
        report = _report(fields, skip=path)
        if required:
            with pytest.raises(ValidationError, match=path):
                entry.validate_report(report)
        else:
            entry.validate_report(report)


def test_coverage_skills_list_the_nested_coverage_fields() -> None:
    entry = next(entry for entry in SKILL_CATALOG.entries if entry.output.coverage)
    paths = [path for path, _kind, _required in _listed_fields(shipped_skill_page(entry))]

    assert paths[-5:] == [
        "coverage",
        "coverage.covered",
        "coverage.not_covered",
        "coverage.not_covered[].item",
        "coverage.not_covered[].reason",
    ]


def test_report_fields_table_renders_a_single_field_model() -> None:
    class OneField(BaseModel):
        only: Literal["a"]

    assert report_fields_table(OneField).splitlines() == [
        "| Field | Type | Required | Description |",
        "| --- | --- | --- | --- |",
        '| `only` | `"a"` | yes |  |',
    ]


def test_report_fields_table_refuses_a_type_it_cannot_render() -> None:
    class Unrenderable(BaseModel):
        ratio: float

    with pytest.raises(TypeError, match="float"):
        report_fields_table(Unrenderable)
