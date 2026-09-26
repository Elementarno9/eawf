"""Contract: the skill catalog is closed, typed, and drives packaging and listing.

The catalog names every presented skill with its invocation grammar, effects
boundary and typed output schema. A retired name refuses with its successor
named instead of aliasing onto it, and the three plugin packagers plus
``eawf skill list`` ship exactly the catalog.
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.runtime.runtimes.claude.plugin_install import install_plugin as install_claude
from eawf.runtime.runtimes.codex.plugin_install import install_plugin as install_codex
from eawf.runtime.runtimes.opencode.plugin_install import install_plugin as install_opencode
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.catalog import (
    SKILL_CATALOG,
    EffectsBoundary,
    InvocationGrammar,
    OutputSchema,
    RetiredSkill,
    SkillCatalog,
    SkillCatalogEntry,
    SkillRetiredError,
    UnknownSkillError,
    resolve_skill,
    shipped_skill_specs,
)

_CATALOG_IDS = (
    "accept",
    "attend",
    "backlog",
    "campaign",
    "decide",
    "dispatch",
    "integrate",
    "memory",
    "milestone",
    "mockup",
    "plan",
    "refactor",
    "reflect",
    "release",
    "research",
    "spike",
    "test",
    "track",
    "verify",
    "why",
)

_RETIRED_SUCCESSORS = {
    "prep": "/plan or /dispatch",
    "audit": "/verify",
    "review": "/verify",
    "security-review": "/verify",
    "ship": "/release",
    "roadmap": "/plan or /milestone",
    "blitz": "/campaign",
    "differentiate": "/spike or /research",
    "extract-function": "/refactor",
    "add-property-test": "/test",
    "write-adr": "/decide",
    "init": "`eawf init`",
}

_ENTRIES = {entry.skill_id: entry for entry in SKILL_CATALOG.entries}


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "skill_id": "demo",
        "skill_class": "lifecycle",
        "audience": "both",
        "description": "Demo skill.",
        "grammar": {"usage": "/demo <go|stop>", "actions": ("go", "stop")},
        "effects": {"summary": "None.", "canonical_mutates": False},
        "output": {"schema_name": "DemoReport", "terminal_outcomes": ("done",)},
    }
    base.update(overrides)
    return base


def test_skill_catalog_holds_exactly_the_twenty_named_skills() -> None:
    ids = tuple(entry.skill_id for entry in SKILL_CATALOG.entries)
    assert ids == _CATALOG_IDS
    assert len(ids) == 20


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_validate_report_accepts_every_declared_outcome(skill_id: str) -> None:
    entry = _ENTRIES[skill_id]
    model = entry.output.model_for(skill_id)
    assert model.__name__ == entry.output.schema_name
    for outcome in entry.output.terminal_outcomes:
        report = entry.validate_report({"skill_id": skill_id, "outcome": outcome})
        assert report.outcome == outcome
        assert isinstance(report, model)


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_validate_report_rejects_undeclared_outcome_and_foreign_skill(skill_id: str) -> None:
    entry = _ENTRIES[skill_id]
    outcome = entry.output.terminal_outcomes[0]
    with pytest.raises(ValidationError):
        entry.validate_report({"skill_id": skill_id, "outcome": "not_an_outcome"})
    with pytest.raises(ValidationError):
        entry.validate_report({"skill_id": "elsewhere", "outcome": outcome})
    with pytest.raises(ValidationError):
        entry.validate_report({"skill_id": skill_id, "outcome": outcome, "extra": 1})
    with pytest.raises(ValidationError):
        entry.validate_report({"skill_id": skill_id})


@pytest.mark.parametrize("skill_id", _CATALOG_IDS)
def test_catalog_entry_declares_grammar_effects_and_output(skill_id: str) -> None:
    entry = _ENTRIES[skill_id]
    assert entry.grammar.usage.startswith(entry.invocation_name)
    assert entry.grammar.argument_hint == entry.grammar.usage.partition(" ")[2]
    assert entry.effects.summary
    assert entry.output.terminal_outcomes
    if entry.effects.canonical_mutates:
        assert entry.effects.rpcs, "a mutating skill must declare its RPC allowlist"


@pytest.mark.parametrize(("retired", "successor"), sorted(_RETIRED_SUCCESSORS.items()))
def test_resolve_skill_refuses_retired_name_naming_successor(retired: str, successor: str) -> None:
    for form in (retired, f"/{retired}"):
        with pytest.raises(SkillRetiredError, match="is retired; use") as excinfo:
            resolve_skill(form)
        assert successor in str(excinfo.value)
        assert excinfo.value.row.skill_id == retired


def test_resolve_skill_retired_names_are_never_aliased() -> None:
    catalog_ids = set(_CATALOG_IDS)
    for row in SKILL_CATALOG.retired:
        assert row.skill_id not in catalog_ids
        assert set(row.successors) <= catalog_ids


@pytest.mark.parametrize("name", ["plan", "/plan"])
def test_resolve_skill_accepts_bare_and_slashed_forms(name: str) -> None:
    assert resolve_skill(name).skill_id == "plan"


@pytest.mark.parametrize("name", ["", "/", "/nope", "plans", "/Plan"])
def test_resolve_skill_unknown_name_raises(name: str) -> None:
    with pytest.raises(UnknownSkillError, match="unknown skill"):
        resolve_skill(name)


def test_resolve_skill_non_string_raises_type_error() -> None:
    with pytest.raises(TypeError, match="must be str"):
        resolve_skill(42)  # type: ignore[arg-type]


def test_invocation_grammar_derives_options_and_hint() -> None:
    grammar = InvocationGrammar(usage="/x <a|b> [--one <v>] [--two] [--one <w>]", actions=("a",))
    assert grammar.options == ("--one", "--two")
    assert grammar.argument_hint == "<a|b> [--one <v>] [--two] [--one <w>]"
    assert InvocationGrammar(usage="/xy").argument_hint == ""


@pytest.mark.parametrize(
    "actions",
    [("missing",), ("go", "go"), ("Go",)],
)
def test_invocation_grammar_rejects_bad_actions(actions: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        InvocationGrammar(usage="/demo <go|Go>", actions=actions)


def test_invocation_grammar_rejects_extra_field_and_short_usage() -> None:
    with pytest.raises(ValidationError):
        InvocationGrammar.model_validate({"usage": "/demo", "extra": True})
    with pytest.raises(ValidationError):
        InvocationGrammar(usage="/d")


@pytest.mark.parametrize("scope", ["/abs/path", "../escape", "a/../../b"])
def test_effects_boundary_rejects_non_relative_write_scope(scope: str) -> None:
    with pytest.raises(ValidationError, match="repo-relative"):
        EffectsBoundary(summary="s", canonical_mutates=False, local_write_scope=scope)


def test_effects_boundary_rejects_duplicate_rpc_and_missing_mutates() -> None:
    with pytest.raises(ValidationError, match="duplicate rpc"):
        EffectsBoundary(summary="s", canonical_mutates=True, rpcs=("a", "a"))
    with pytest.raises(ValidationError):
        EffectsBoundary.model_validate({"summary": "s"})


@pytest.mark.parametrize(
    ("schema_name", "outcomes"),
    [("DemoReport", ()), ("DemoReport", ("x", "x")), ("DemoReport", ("Bad",)), ("demo", ("x",))],
)
def test_output_schema_rejects_bad_shape(schema_name: str, outcomes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        OutputSchema(schema_name=schema_name, terminal_outcomes=outcomes)


@pytest.mark.parametrize(
    ("skill_id", "ok"),
    [("ab", True), ("a" * 32, True), ("a", False), ("a" * 33, False), ("1ab", False)],
)
def test_skill_catalog_entry_id_length_boundaries(skill_id: str, ok: bool) -> None:
    payload = _entry(skill_id=skill_id, grammar={"usage": f"/{skill_id} x"})
    if ok:
        assert SkillCatalogEntry.model_validate(payload).skill_id == skill_id
    else:
        with pytest.raises(ValidationError):
            SkillCatalogEntry.model_validate(payload)


def test_skill_catalog_entry_rejects_inconsistent_audience_and_usage() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        SkillCatalogEntry.model_validate(_entry(grammar={"usage": "/other x"}))
    with pytest.raises(ValidationError, match="not declared actions"):
        SkillCatalogEntry.model_validate(_entry(operator_only_actions=("fly",)))
    with pytest.raises(ValidationError, match="user_only"):
        SkillCatalogEntry.model_validate(
            _entry(audience="user_only", operator_only_actions=("go",))
        )
    with pytest.raises(ValidationError):
        SkillCatalogEntry.model_validate(_entry(audience="everyone"))
    with pytest.raises(ValidationError):
        SkillCatalogEntry.model_validate(_entry(unexpected=True))


def test_retired_skill_requires_a_successor() -> None:
    with pytest.raises(ValidationError, match="names no successor"):
        RetiredSkill(skill_id="old", reason="gone")
    row = RetiredSkill(skill_id="old", successors=("demo",), successor_surface="`x`", reason="r")
    assert row.successor_text() == "/demo or `x`"


def test_skill_catalog_rejects_duplicates_aliases_and_dangling_successors() -> None:
    entry = SkillCatalogEntry.model_validate(_entry())
    with pytest.raises(ValidationError, match="duplicate catalog"):
        SkillCatalog(entries=(entry, entry))
    with pytest.raises(ValidationError, match="still catalog entries"):
        SkillCatalog(
            entries=(entry,),
            retired=(RetiredSkill(skill_id="demo", successors=("demo",), reason="r"),),
        )
    with pytest.raises(ValidationError, match="non-catalog successors"):
        SkillCatalog(
            entries=(entry,),
            retired=(RetiredSkill(skill_id="old", successors=("ghost",), reason="r"),),
        )
    retired = RetiredSkill(skill_id="old", successors=("demo",), reason="r")
    with pytest.raises(ValidationError, match="duplicate retired"):
        SkillCatalog(entries=(entry,), retired=(retired, retired))
    with pytest.raises(ValidationError):
        SkillCatalog(entries=())
    assert SkillCatalog(entries=(entry,), retired=(retired,)).replaced_by("demo") == ("old",)


def test_shipped_skill_specs_project_the_catalog() -> None:
    specs = shipped_skill_specs()
    assert tuple(spec.skill_name for spec in specs) == _CATALOG_IDS
    for spec in specs:
        entry = _ENTRIES[spec.skill_name]
        assert spec.argument_hint == entry.grammar.argument_hint
        assert spec.description == entry.description
        barred = entry.audience == "user_only" or entry.effects.canonical_mutates
        assert spec.disable_model_invocation is barred
        assert spec.body.strip()


def test_packagers_ship_exactly_the_catalog(tmp_path: Path) -> None:
    claude = install_claude(tmp_path / "claude", persist_manifest=False)
    codex = install_codex(tmp_path / "codex")
    opencode = install_opencode(tmp_path / "opencode", persist_manifest=False)
    assert sorted(d.path.parent.name for d in claude.skills) == sorted(_CATALOG_IDS)
    assert sorted(d.path.parent.name for d in codex.skills) == sorted(_CATALOG_IDS)
    assert sorted(d.path.stem for d in opencode.commands) == sorted(_CATALOG_IDS)
    claude_skills = tmp_path / "claude" / ".claude" / "skills"
    for retired in _RETIRED_SUCCESSORS:
        assert not (claude_skills / retired).exists()


def test_skill_list_lists_the_catalog_with_output_schema() -> None:
    result = CliRunner().invoke(app, ["--json", "skill", "list", "--scope", "builtin"])
    assert result.exit_code == 0, result.stdout
    rows = orjson.loads(result.stdout)["skills"]
    assert sorted(row["name"] for row in rows) == sorted(f"/{sid}" for sid in _CATALOG_IDS)
    by_name = {row["name"]: row for row in rows}
    plan = by_name["/plan"]
    assert plan["output_schema"] == "PlanSkillReport"
    assert plan["audience"] == "both"
    assert "approved" in plan["terminal_outcomes"]
    assert plan["argument_hint"] == _ENTRIES["plan"].grammar.argument_hint


@pytest.mark.parametrize("command", ["run", "render"])
def test_skill_cli_refuses_retired_name_naming_successor(command: str) -> None:
    result = CliRunner().invoke(app, ["skill", command, "/prep"], input="")
    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "retired" in output
    assert "/plan or /dispatch" in output
