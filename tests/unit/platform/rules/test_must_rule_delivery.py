"""A ``must`` rule compiles only when every supported runtime receives it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.platform.rules import (
    RuleCompileError,
    RuleGraph,
    RuleMustUndeliveredError,
    RuleRecord,
    RuleRoleUndeliveredError,
    builtin_rule_modules,
    compile_card_graph,
    compile_conduct_graph,
    compile_rule_graph,
    compile_rule_records,
    load_rule_source,
    registered_agent_roles,
    registered_enforcement_refs,
    registered_projection_readers,
    rule_digest,
    select_rule_modules,
)
from eawf.platform.rules.compile import ProjectionReaders
from eawf.platform.rules.host_facts import HOST_RUNTIMES, load_host_facts
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    builtin_rule_provider,
    plan_rule_projections,
)
from eawf.surfaces.render.agents import AGENT_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[4]

_SOURCES: dict[str, dict[str, str]] = {
    "builtin": {"kind": "builtin", "locator": "eawf.core", "digest": "sha256:" + "1" * 64},
    "workspace": {"kind": "workspace", "locator": "team.rules", "digest": "sha256:" + "2" * 64},
    "repository": {
        "kind": "repository",
        "locator": ".ea/rules.yaml",
        "digest": "sha256:" + "3" * 64,
    },
}
_NAMESPACE = {"builtin": "eawf", "workspace": "workspace", "repository": "repo"}
_INSTRUCTION = "Record the reason for every skipped test in the test body."


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": "repo.skip-reason",
        "obligation_id": "demo.skip-reason",
        "revision": 1,
        "title": "Record why a test is skipped",
        "zone": "steering",
        "force": "must",
        "effectiveness": "behavioral",
        "instruction": _INSTRUCTION,
        "scope": {"activities": ["test"]},
        "verification": {"method": "review"},
    }
    body.update(overrides)
    return body


def _record(*, layer: str = "repository", **overrides: Any) -> RuleRecord:
    body = _body(**overrides)
    body["rule_id"] = f"{_NAMESPACE[layer]}.skip-reason"
    return RuleRecord.model_validate({**body, "source": _SOURCES[layer]})


def _compile(*records: RuleRecord, readers: ProjectionReaders | None = None) -> RuleGraph:
    return compile_rule_records(
        records,
        modules=(),
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers() if readers is None else readers,
    )


def _write_source(repo: Path, *rules: dict[str, Any]) -> None:
    source = repo / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "modules": [], "rules": list(rules)}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# ---- gate-fire proof: the refusal ------------------------------------------


def test_compile_rule_records_refuses_retrievable_must_rule() -> None:
    with pytest.raises(RuleMustUndeliveredError, match="module view") as excinfo:
        _compile(_record(zone="retrievable"))
    assert isinstance(excinfo.value, RuleCompileError)
    assert excinfo.value.code == "rule_must_undelivered"


def test_compile_rule_records_refuses_workspace_must_opencode_never_loads() -> None:
    with pytest.raises(RuleMustUndeliveredError, match=r"rendered only into policy.*opencode"):
        _compile(_record(layer="workspace"))


def test_compile_rule_records_refuses_workspace_constitution_must() -> None:
    record = _record(layer="workspace", zone="constitution", scope={})
    with pytest.raises(RuleMustUndeliveredError, match="opencode never loads"):
        _compile(record)


def test_compile_rule_records_refuses_shipped_module_must_moved_to_retrievable() -> None:
    python = builtin_rule_modules()["eawf.craft.python"]
    must = next(record for record in python.records if record.force == "must")
    reverted = must.model_copy(update={"zone": "retrievable"})
    with pytest.raises(RuleMustUndeliveredError, match=must.rule_id):
        _compile(reverted)


def test_compile_rule_graph_refuses_undelivered_must_on_the_production_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "demo"
    _write_source(repo, _body(zone="retrievable"))
    with pytest.raises(RuleMustUndeliveredError):
        compile_rule_graph(repo, home=tmp_path)
    with pytest.raises(RuleMustUndeliveredError):
        compile_card_graph(repo)
    with pytest.raises(RuleMustUndeliveredError):
        plan_rule_projections(repo, home=tmp_path)


# ---- gate-fire proof: a role rule no agent definition embeds -----------------


@pytest.mark.parametrize("roles", [["nobody"], ["nobody", "ghost"]])
def test_compile_rule_records_refuses_a_must_rule_scoped_to_roles_without_an_agent(
    roles: list[str],
) -> None:
    record = _record(layer="builtin", zone="retrievable", scope={"roles": roles})
    with pytest.raises(RuleRoleUndeliveredError, match="no rendered agent definition") as excinfo:
        _compile(record)
    assert isinstance(excinfo.value, RuleMustUndeliveredError)
    assert isinstance(excinfo.value, RuleCompileError)
    assert excinfo.value.code == "rule_role_undelivered"


@pytest.mark.parametrize("layer", ["workspace", "repository"])
def test_compile_rule_records_refuses_a_non_builtin_role_must_rule(layer: str) -> None:
    record = _record(layer=layer, zone="retrievable", scope={"roles": ["executor"]})
    with pytest.raises(RuleRoleUndeliveredError, match=f"the {layer} layer") as excinfo:
        _compile(record)
    assert excinfo.value.code == "rule_role_undelivered"


def test_compile_rule_records_refuses_a_catalog_module_role_must_rule() -> None:
    record = RuleRecord.model_validate(
        {
            **_body(rule_id="eawf.craft-role", zone="retrievable", scope={"roles": ["executor"]}),
            "source": {**_SOURCES["builtin"], "locator": "eawf.craft.python@1"},
        }
    )
    with pytest.raises(RuleRoleUndeliveredError, match="catalog module"):
        _compile(record)


def test_compile_rule_records_role_must_rule_with_enforcement_is_accepted() -> None:
    record = _record(
        layer="repository",
        zone="retrievable",
        scope={"roles": ["nobody"]},
        verification={"method": "structural"},
        enforcement_ref="lint.eawf012",
    )
    assert len(_compile(record).rules) == 1


def test_compile_rule_graph_refuses_a_repository_role_must_on_the_production_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "demo"
    _write_source(repo, _body(zone="retrievable", scope={"roles": ["executor"]}))
    with pytest.raises(RuleRoleUndeliveredError):
        compile_rule_graph(repo, home=tmp_path)
    with pytest.raises(RuleRoleUndeliveredError):
        plan_rule_projections(repo, home=tmp_path)


def test_registered_agent_roles_come_from_the_agent_registry() -> None:
    roles = registered_agent_roles()
    assert roles == frozenset(spec.role for spec in AGENT_REGISTRY)
    assert {"executor", "reviewer"} <= roles
    assert "nobody" not in roles
    assert registered_agent_roles() is roles


# ---- accepted routes -------------------------------------------------------


@pytest.mark.parametrize(
    ("layer", "overrides"),
    [
        ("builtin", {"zone": "constitution", "scope": {}}),
        ("repository", {"zone": "constitution", "scope": {}}),
        ("repository", {}),
        ("builtin", {"scope": {"paths": ["src/**"]}}),
        (
            "repository",
            {
                "zone": "retrievable",
                "verification": {"method": "structural"},
                "enforcement_ref": "lint.eawf012",
            },
        ),
        ("builtin", {"zone": "retrievable", "scope": {"roles": ["executor"]}}),
        ("builtin", {"zone": "retrievable", "scope": {"roles": ["nobody", "reviewer"]}}),
        ("repository", {"zone": "retrievable", "force": "should", "scope": {"roles": ["nobody"]}}),
        ("repository", {"zone": "retrievable", "force": "should"}),
        ("workspace", {"force": "information"}),
    ],
    ids=[
        "builtin-constitution",
        "repository-constitution",
        "committed-activity-scoped",
        "committed-path-scoped",
        "enforcement-backed",
        "role-embedded",
        "role-embedded-one-of-two",
        "role-scoped-should",
        "retrievable-should",
        "workspace-information",
    ],
)
def test_compile_rule_records_accepts_a_delivered_rule(
    layer: str, overrides: dict[str, Any]
) -> None:
    record = _record(layer=layer, **overrides)
    assert [rule.record.rule_id for rule in _compile(record).rules] == [record.rule_id]


def test_compile_rule_records_ignores_a_superseded_undelivered_must() -> None:
    prior = _record(layer="builtin", zone="retrievable")
    replacement = _record(
        force="should",
        revision=2,
        supersedes=[{"rule_id": prior.rule_id, "revision": 1, "digest": rule_digest(prior)}],
    )
    graph = _compile(prior, replacement)
    assert [rule.record.rule_id for rule in graph.rules] == [replacement.rule_id]


# ---- boundaries of the reader set ------------------------------------------


def test_compile_rule_records_empty_readers_raises() -> None:
    with pytest.raises(ValueError, match="names no runtime"):
        _compile(_record(), readers={})


def test_compile_rule_records_single_policy_reader_accepts_workspace_must() -> None:
    graph = _compile(_record(layer="workspace"), readers={"claude": frozenset({"policy"})})
    assert len(graph.rules) == 1


def test_compile_rule_records_names_only_the_runtime_that_misses() -> None:
    readers: ProjectionReaders = {
        "claude": frozenset({"policy"}),
        "codex": frozenset({"policy"}),
        "opencode": frozenset({"card"}),
    }
    with pytest.raises(RuleMustUndeliveredError) as excinfo:
        _compile(_record(layer="workspace"), readers=readers)
    message = str(excinfo.value)
    assert "which opencode never loads" in message
    assert "claude" not in message
    assert "codex" not in message


def test_compile_rule_records_card_reader_accepts_committed_must() -> None:
    graph = _compile(_record(), readers={"opencode": frozenset({"card"})})
    assert len(graph.rules) == 1


def test_registered_projection_readers_come_from_the_host_facts() -> None:
    readers = registered_projection_readers()
    assert set(readers) == set(HOST_RUNTIMES)
    for record in load_host_facts().records:
        assert readers[record.runtime] == frozenset(record.reads)


# ---- the routes the compiler relies on are real ----------------------------


def test_plan_rule_projections_renders_a_committed_must_for_every_reader(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _write_source(repo, _body())
    plan = plan_rule_projections(repo, home=tmp_path)
    texts = {rendered.record.kind: rendered.text for rendered in plan.projections}
    assert {rendered.record.target for rendered in plan.projections} == {
        CARD_TARGET,
        POLICY_TARGET,
    }
    for runtime, kinds in registered_projection_readers().items():
        assert any(_INSTRUCTION in texts[kind] for kind in kinds), runtime


# ---- this repository compiles with no exemption ----------------------------


def test_compile_rule_graph_this_repository_delivers_every_must_rule() -> None:
    selection = select_rule_modules(load_rule_source(_REPO_ROOT))
    graph = compile_rule_graph(_REPO_ROOT, builtin_rules=builtin_rule_provider(selection))
    musts = [rule for rule in graph.rules if rule.record.force == "must"]
    assert musts
    assert {rule.record.source.kind for rule in musts} == {"builtin", "repository"}


def test_compile_conduct_graph_every_conduct_must_rule_is_delivered() -> None:
    graph = compile_conduct_graph()
    assert any(rule.record.force == "must" for rule in graph.rules)


def test_builtin_rule_modules_carry_no_retrievable_must_rule() -> None:
    catalog = builtin_rule_modules()
    records = [record for module in catalog.values() for record in module.records]
    retrievable_must = [
        record.rule_id
        for record in records
        if record.force == "must" and record.zone == "retrievable" and not record.scope.roles
    ]
    assert retrievable_must == []
    graph = compile_rule_records(
        records,
        modules=catalog.keys(),
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers(),
    )
    assert len(graph.rules) == len(records)
