"""Each catalog skill declares how it behaves outside an eawf-managed repository.

The ``standalone`` class drives three surfaces: the ``hooks/skill-classes.tsv``
the plugin ships for the hook wrapper, the conditional authority lines of every
shipped page, and the research page's file rules. The render check also refuses
a page naming an ``eawf`` verb the CLI lacks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.workflow.skills.bodies.chassis import (
    MANAGED_AUTHORITY_LINE,
    OUTPUT_PROSE_LINE,
    REFUSED_AUTHORITY_LINE,
    STANDALONE_AUTHORITY_LINE,
    chassis_findings,
    shipped_skill_page,
)
from eawf.workflow.skills.catalog import (
    NOT_MANAGED_REFUSAL,
    SKILL_CATALOG,
    SkillCatalogEntry,
    SkillRetiredError,
    UnknownSkillError,
    render_skill_classes_tsv,
    resolve_skill,
    standalone_class,
)

_SUPPORTED = {"research"}
_DEGRADED = {"spike", "mockup", "refactor", "test"}
_REFUSED = {
    "reflect",
    "why",
    "memory",
    "attend",
    "backlog",
    "decide",
    "dispatch",
    "integrate",
    "verify",
    "plan",
    "milestone",
    "track",
    "accept",
    "release",
    "campaign",
}


def _pages() -> dict[str, str]:
    return {entry.skill_id: shipped_skill_page(entry) for entry in SKILL_CATALOG.entries}


def _authority(page: str) -> list[str]:
    start = page.index("## 1. Authority\n\n") + len("## 1. Authority\n\n")
    return page[start : page.index("\n\n## 2. Context")].splitlines()


def _entry_fields(**overrides: Any) -> dict[str, Any]:
    fields = resolve_skill("research").model_dump()
    fields.update(overrides)
    return fields


def test_standalone_classes() -> None:
    classes = {entry.skill_id: entry.standalone for entry in SKILL_CATALOG.entries}
    assert set(classes) == _SUPPORTED | _DEGRADED | _REFUSED
    assert {skill for skill, kind in classes.items() if kind == "supported"} == _SUPPORTED
    assert {skill for skill, kind in classes.items() if kind == "degraded"} == _DEGRADED
    assert {skill for skill, kind in classes.items() if kind == "refused"} == _REFUSED
    for entry in SKILL_CATALOG.entries:
        if entry.standalone != "refused":
            assert 0 < len(entry.standalone_note) <= 160
    for role in ("executor", "planner", "auditor"):
        assert standalone_class(f"eawf-rules-{role}") == "refused"
        assert standalone_class(f"/eawf-rules-{role}") == "refused"
    assert standalone_class("/research") == "supported"
    assert standalone_class("spike") == "degraded"


def test_rule_carrier_prefix_matches_the_carriers() -> None:
    from eawf.platform.rules.carriers import carrier_name

    assert standalone_class(carrier_name("reviewer")) == "refused"


def test_note_is_required_unless_refused() -> None:
    with pytest.raises(ValidationError, match="needs a note"):
        SkillCatalogEntry.model_validate(_entry_fields(standalone_note=""))
    with pytest.raises(ValidationError, match="needs a note"):
        SkillCatalogEntry.model_validate(_entry_fields(standalone="degraded", standalone_note=" "))
    refused = SkillCatalogEntry.model_validate(
        _entry_fields(standalone="refused", standalone_note="")
    )
    assert refused.standalone == "refused"


def test_note_length_boundary() -> None:
    at_cap = SkillCatalogEntry.model_validate(_entry_fields(standalone_note="x" * 160))
    assert len(at_cap.standalone_note) == 160
    single = SkillCatalogEntry.model_validate(_entry_fields(standalone_note="x"))
    assert single.standalone_note == "x"
    with pytest.raises(ValidationError, match="160"):
        SkillCatalogEntry.model_validate(_entry_fields(standalone_note="x" * 161))


def test_standalone_class_is_closed_and_defaults_to_refused() -> None:
    with pytest.raises(ValidationError, match="standalone"):
        SkillCatalogEntry.model_validate(_entry_fields(standalone="anywhere"))
    fields = _entry_fields()
    del fields["standalone"]
    del fields["standalone_note"]
    assert SkillCatalogEntry.model_validate(fields).standalone == "refused"
    with pytest.raises(ValidationError, match="extra"):
        SkillCatalogEntry.model_validate(_entry_fields(standalone_reason="none"))


def test_standalone_class_refuses_bad_names() -> None:
    with pytest.raises(TypeError, match="must be str"):
        standalone_class(None)  # type: ignore[arg-type]
    with pytest.raises(UnknownSkillError):
        standalone_class("")
    with pytest.raises(UnknownSkillError):
        standalone_class("no-such-skill")
    with pytest.raises(SkillRetiredError):
        standalone_class("/review")


def test_skill_classes_tsv(tmp_path: Path) -> None:
    package_plugin(tmp_path / "plugin", extra_tools={})
    tsv = (tmp_path / "plugin" / "hooks" / "skill-classes.tsv").read_text(encoding="utf-8")

    lines = tsv.splitlines()
    assert tsv.endswith("\n")
    assert len(lines) == len(SKILL_CATALOG.entries)
    assert [line.split("\t") for line in lines] == [
        [entry.skill_id, entry.standalone] for entry in SKILL_CATALOG.entries
    ]
    assert tsv == render_skill_classes_tsv()


def test_skill_classes_tsv_ships_with_the_hooks_only(tmp_path: Path) -> None:
    package_plugin(tmp_path / "plugin", extra_tools={}, include_hooks=False)
    assert not (tmp_path / "plugin" / "hooks").exists()


def test_conditional_authority_lines() -> None:
    pages = _pages()
    assert MANAGED_AUTHORITY_LINE.startswith("In an eawf-managed repository:")
    assert STANDALONE_AUTHORITY_LINE.startswith("Outside an eawf-managed repository:")
    assert (
        "Needs an eawf-managed repository; outside one the hook refuses it with"
        f" `{NOT_MANAGED_REFUSAL}`."
    ) == REFUSED_AUTHORITY_LINE
    for skill_id, page in pages.items():
        assert "enclosing Run, Task or Campaign scope" not in page, skill_id
        authority = _authority(page)
        if skill_id in _REFUSED:
            assert authority[0] == f"- {REFUSED_AUTHORITY_LINE}", skill_id
            assert f"- {STANDALONE_AUTHORITY_LINE}" not in authority, skill_id
            assert f"- {MANAGED_AUTHORITY_LINE}" not in authority, skill_id
        else:
            assert authority[0] == f"- {MANAGED_AUTHORITY_LINE}", skill_id
            assert f"- {STANDALONE_AUTHORITY_LINE}" in authority, skill_id
            assert REFUSED_AUTHORITY_LINE not in page, skill_id
            assert not [line for line in authority if "compiled capsule of the" in line]
            entry = resolve_skill(skill_id)
            note = f"- Standalone use ({entry.standalone}): {entry.standalone_note}"
            assert note in authority, skill_id


def test_research_page_texts() -> None:
    page = shipped_skill_page(resolve_skill("research"))
    plain = page.replace("`", "")
    assert (
        "Local write root: .ea/local/research in an eawf-managed repository; outside one, none,"
        " unless --save <path> names a file."
    ) in plain
    assert (
        "8. Write the brief. In an eawf-managed repository, write it to the `--save` path under"
        " `.ea/local/research/`, by default `<date>-<slug>.md` there. Outside one, write no file"
        " and return the brief in the conversation, unless `--save <path>` names a file. Either"
        " way: an H1 title, then exactly the sections Question, Findings, Alternatives,"
        " Recommendation, Open gaps, References in that order. References is a numbered table"
        " with the columns `#` and `Reference`, one repo-relative path, URL or Eawf URN per row,"
        " each cited in the prose as `[N]`."
    ) in page
    assert (
        "9. If the brief is a file, run `eawf research check <brief>` and fix every error it"
        " names. If a report file exists, run `eawf skill check-report /research <report>`."
        " With no file, check the section order and the References table yourself. Stop at the"
        " last pass, one rendered page, or the first hard budget cap."
    ) in page
    assert "`--save needs a path outside an eawf-managed repository`" in page


def test_memory_grammar_and_verb_check() -> None:
    memory = resolve_skill("memory")
    assert memory.grammar.usage.startswith("/memory <list|view|add|promote> ")
    assert memory.grammar.actions == ("list", "view", "add", "promote")
    page = shipped_skill_page(memory)
    assert "List returns" in page
    assert "Search returns" not in page
    assert chassis_findings(page, memory) == ()

    fixture = page.replace("`eawf memory list`", "`eawf memory search`")
    assert fixture != page
    assert chassis_findings(fixture, memory) == (
        "`eawf memory search` names an eawf verb the CLI lacks",
    )

    for skill_id, rendered in _pages().items():
        assert f"{OUTPUT_PROSE_LINE}\n" in rendered, skill_id
    assert OUTPUT_PROSE_LINE == (
        "Show the operator the outcome and summary as prose, not the JSON. Print the JSON only"
        " with `--output json`."
    )


@pytest.mark.parametrize(
    ("command", "known"),
    [
        ("`eawf memory list`", True),
        ("`eawf research check <brief>`", True),
        ("`eawf skill check-report /research <report>`", True),
        ("`eawf <verb> --help`", True),
        ("`eawf memory`", True),
        ("`eawf nosuch`", False),
        ("`eawf memory list extra-word`", True),
        ("`eawf memory searches --scope x`", False),
    ],
)
def test_verb_check_boundaries(command: str, *, known: bool) -> None:
    memory = resolve_skill("memory")
    page = shipped_skill_page(memory).replace("`eawf memory list`", command)
    findings = [f for f in chassis_findings(page, memory) if "names an eawf verb" in f]
    assert (findings == []) is known, findings
