"""Contract: skills are generated from the catalog, joined to the verb catalog, and published.

- SURF-021: a skill whose frontmatter forbids model invocation is operator-only by
  design and its page says so; every other skill is model-invocable.
- SURF-079 / SURF-087: one command emits every verb with its entity, parameters,
  typed errors and effect class, and every lifecycle route joins it.
- SURF-092: sweep-shaped reports carry a machine-checked covered / not-covered block.
- SURF-100 / SURF-108: every page names its epoch-2 subject and routes, and no shipped
  skill, manifest or hook says phase, iter or wave.
- SURF-101: a missing or incompatible route fails generation.
- SURF-105 / SURF-109: a bundle records the epoch it targets and refuses, with
  migration guidance, to operate in a repository at another epoch.
- SURF-112 / SURF-114: one argument schema generates help, machine schema,
  completion and validation, and one lane map filters every surface.
- SURF-113: catalog, prompt, argument, allowlist, output-schema and file digests are
  checked as one publication unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import click
import orjson
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.runtime.daemon.methods import registered_methods
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.runtime.runtimes.codex.plugin_package import package_plugin as package_codex
from eawf.surfaces.cli import verb_contract
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.verb_catalog import (
    CLI_VERB_EFFECTS,
    VerbCatalogError,
    build_verb_catalog,
    verb_catalog,
)
from eawf.surfaces.cli.verb_closure import ROOT_ENTRY_EXCEPTIONS
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills.arguments import (
    InvocationRefusedError,
    argument_schema,
    check_invocation,
)
from eawf.workflow.skills.bodies.chassis import chassis_findings
from eawf.workflow.skills.bodies.prompts import skill_prompt
from eawf.workflow.skills.catalog import (
    SKILL_CATALOG,
    EffectsBoundary,
    Lane,
    SkillCatalog,
    SkillCatalogEntry,
    resolve_skill,
    shipped_skill_specs,
    skill_lanes,
    skills_for_lane,
)
from eawf.workflow.skills.catalog_join import join_findings, require_joined
from eawf.workflow.skills.census import bundle_census, epoch1_nouns
from eawf.workflow.skills.engine import SkillContext
from eawf.workflow.skills.publication import (
    BUNDLE_TARGET_EPOCH,
    PUBLICATION_FILENAME,
    BundleEpochMismatchError,
    publication_findings,
    read_publication,
    require_bundle_epoch,
)

runner = CliRunner()

_SPECS: Final = {spec.skill_name: spec for spec in shipped_skill_specs()}
_ENTRIES: Final = {entry.skill_id: entry for entry in SKILL_CATALOG.entries}
_COVERAGE_SKILLS: Final = frozenset({"campaign", "research", "spike", "test", "why"})


def _entry(**overrides: Any) -> SkillCatalogEntry:
    base: dict[str, Any] = {
        "skill_id": "demo",
        "skill_class": "lifecycle",
        "subject": "Task",
        "budget_class": "steering_zone2",
        "audience": "both",
        "description": "Demo skill.",
        "grammar": {"usage": "/demo <go|stop> [--from-spec <path>]", "actions": ("go", "stop")},
        "effects": {"summary": "s", "canonical_mutates": True, "rpcs": ("domain.task.create",)},
        "output": {"schema_name": "DemoReport", "terminal_outcomes": ("done",)},
    }
    base.update(overrides)
    return SkillCatalogEntry.model_validate(base)


def _join(entry: SkillCatalogEntry) -> tuple[str, ...]:
    return join_findings(SkillCatalog(entries=(entry,)), verb_catalog(), registered_methods())


@pytest.fixture(scope="module")
def claude_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bundle") / "claude"
    package_plugin(root, extra_tools={})
    return root


# ---- SURF-021 ----------------------------------------------------------------


@pytest.mark.parametrize("skill_id", sorted(_ENTRIES))
def test_surf_021_model_barred_frontmatter_is_operator_only_by_design(skill_id: str) -> None:
    spec, entry = _SPECS[skill_id], _ENTRIES[skill_id]
    assert spec.disable_model_invocation is ("agent" not in skill_lanes(entry))
    if spec.disable_model_invocation:
        assert entry.audience == "user_only"
        assert "Only an authenticated operator initiates this skill, by design" in spec.body


# ---- SURF-079 / SURF-087 -----------------------------------------------------


def test_surf_087_verbs_emits_every_verb_with_entity_parameters_errors_and_effect() -> None:
    result = runner.invoke(app, ["--json", "verbs"])
    assert result.exit_code == 0, result.stdout
    payload = orjson.loads(result.stdout)
    groups = {*verb_contract.ENTITY_GROUPS, *(row.name for row in ROOT_ENTRY_EXCEPTIONS)}
    by_verb = {row["verb"]: row for row in payload["entries"]}
    assert set(CLI_VERB_EFFECTS) <= set(by_verb)
    for row in payload["entries"]:
        assert row["entity"] in groups
        assert row["effect_class"] in {"read", "mutate", "create"}
        assert row["errors"]
    seal = by_verb["task seal"]
    assert seal["routes"] == ["runtime.candidate.report.bind"]
    assert any("--expected-revision" in p["flags"] for p in seal["parameters"])
    assert by_verb["task create"]["effect_class"] == "create"
    assert by_verb["reflect run"]["effect_class"] == "read"
    dispatch = by_verb["runtime.run.dispatch"]
    assert dispatch["surface"] == "rpc"
    assert {"urn", "idempotency_key"} <= {p["name"] for p in dispatch["parameters"]}
    assert "revision_conflict" in payload["refusal_codes"]


def test_surf_087_verbs_text_carries_every_verb() -> None:
    result = runner.invoke(app, ["verbs"])
    assert result.exit_code == 0
    assert "task seal  [task] mutate cli -> runtime.candidate.report.bind" in result.stdout


def typer_root() -> click.Group:
    import typer

    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


def _tree(verbs: dict[str, dict[str, click.Command]]) -> click.Group:
    groups: dict[str, click.Command] = {
        name: click.Group(name, commands=children) for name, children in verbs.items()
    }
    return click.Group("eawf", commands=groups)


def test_surf_087_an_unclassified_verb_refuses_the_catalog() -> None:
    root = _tree({"task": {"teleport": click.Command("teleport")}})
    with pytest.raises(VerbCatalogError, match="'task teleport' declares no effect class"):
        build_verb_catalog(root, registered_methods())


def test_surf_087_a_declared_verb_missing_from_the_tree_refuses_the_catalog() -> None:
    with pytest.raises(VerbCatalogError, match="are not on the command tree"):
        build_verb_catalog(_tree({}), registered_methods())


def test_surf_087_a_route_the_daemon_does_not_register_refuses_the_catalog() -> None:
    with pytest.raises(VerbCatalogError, match="unregistered routes"):
        build_verb_catalog(typer_root(), ())


def test_surf_079_every_lifecycle_route_joins_the_verb_catalog() -> None:
    assert join_findings(SKILL_CATALOG, verb_catalog(), registered_methods()) == ()
    require_joined(SKILL_CATALOG)
    lifecycle = [e for e in SKILL_CATALOG.entries if e.skill_class == "lifecycle"]
    for entry in lifecycle:
        for rpc in entry.effects.rpcs:
            assert verb_catalog().route_verbs(rpc), f"{entry.skill_id} route {rpc} has no verb"


def test_surf_079_hand_edited_bundle_fails_the_render_check(claude_bundle: Path) -> None:
    assert publication_findings(claude_bundle) == ()
    page = claude_bundle / "skills" / "why" / "SKILL.md"
    original = page.read_bytes()
    page.write_bytes(original + b"\nhand edit\n")
    try:
        assert publication_findings(claude_bundle) == (
            "skills/why/SKILL.md was edited after it was rendered",
        )
    finally:
        page.write_bytes(original)


# ---- SURF-101 ----------------------------------------------------------------


def test_surf_101_generation_joins_a_well_formed_lifecycle_skill() -> None:
    assert _join(_entry()) == ()


@pytest.mark.parametrize(
    ("effects", "finding"),
    [
        ({"rpcs": ("domain.task.teleport",)}, "names unregistered route 'domain.task.teleport'"),
        ({"rpcs": ("runtime.run.control.request",)}, "which no verb carries"),
        ({"rpcs": (), "verbs": ()}, "is a lifecycle skill that names no route"),
        ({"rpcs": (), "verbs": ("task teleport",)}, "which the verb catalog does not carry"),
    ],
)
def test_surf_101_a_missing_route_fails_generation(effects: dict[str, Any], finding: str) -> None:
    entry = _entry(effects={"summary": "s", "canonical_mutates": True, **effects})
    assert any(finding in row for row in _join(entry)), _join(entry)


def test_surf_101_a_read_only_skill_driving_a_mutating_route_is_incompatible() -> None:
    entry = _entry(
        effects={"summary": "s", "canonical_mutates": False, "rpcs": ("domain.task.promote",)}
    )
    assert any("read-only but names mutating route" in row for row in _join(entry))
    by_verb = _entry(effects={"summary": "s", "canonical_mutates": False, "verbs": ("memory add",)})
    assert any("read-only but drives create verb" in row for row in _join(by_verb))


def test_surf_101_a_forwarded_argument_no_verb_accepts_fails_generation() -> None:
    entry = _entry(
        effects={"summary": "s", "canonical_mutates": True, "rpcs": ("projection.track.read",)}
    )
    assert any("forwards --from-spec" in row for row in _join(entry))


def test_surf_101_prompt_bodies_come_from_the_prompt_records_not_cli_metadata() -> None:
    for skill_id, spec in _SPECS.items():
        prompt = skill_prompt(skill_id)
        assert prompt.method[0] in spec.body
        assert prompt.task.splitlines()[0] in spec.body


# ---- SURF-100 / SURF-108 -----------------------------------------------------


@pytest.mark.parametrize("skill_id", sorted(_ENTRIES))
def test_surf_100_every_page_names_its_epoch2_subject_and_route(skill_id: str) -> None:
    entry, body = _ENTRIES[skill_id], _SPECS[skill_id].body
    assert f"- Operates on: {entry.subject}, through " in body
    if entry.skill_class == "lifecycle":
        assert f"through `{(entry.effects.rpcs or entry.effects.verbs)[0]}" in body or any(
            f"`eawf {verb}`" in body for verb in entry.effects.verbs
        )
    assert epoch1_nouns(body) == ()


def test_surf_100_a_page_naming_an_epoch1_noun_is_refused() -> None:
    entry = _ENTRIES["plan"]
    page = _SPECS["plan"].body.replace("PlanRevision", "wave plan", 1)
    assert "epoch-1 noun 'wave' is on the page" in chassis_findings(page, entry)


def test_surf_108_census_finds_no_epoch1_noun_in_shipped_skills_manifests_or_hooks(
    claude_bundle: Path, tmp_path: Path
) -> None:
    package_codex(tmp_path / "codex")
    codex_root = tmp_path / "codex" / "plugins" / "eawf"
    for root in (claude_bundle, codex_root):
        publication = read_publication(root)
        shipped = {row.path for row in publication.files if row.kind != "agent"}
        hits = [hit for hit in bundle_census(root) if hit.path in shipped]
        assert hits == []
        assert {"skill", "manifest", "hook"} <= {row.kind for row in publication.files}


def test_surf_108_census_reports_each_epoch1_noun_with_its_line(tmp_path: Path) -> None:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "x.sh").write_text("ok\nrun the wave\nP01-I02-W03\n")
    hits = bundle_census(tmp_path)
    assert [(h.path, h.line, h.noun) for h in hits] == [
        ("hooks/x.sh", 2, "wave"),
        ("hooks/x.sh", 3, "P01-I02-W03"),
    ]
    assert bundle_census(tmp_path / "hooks" / "absent") == ()


# ---- SURF-105 / SURF-109 -----------------------------------------------------


def test_surf_105_a_bundle_records_the_epoch_it_targets(claude_bundle: Path) -> None:
    assert read_publication(claude_bundle).target_epoch == BUNDLE_TARGET_EPOCH == 2
    for hook in (claude_bundle / "hooks").glob("*.sh"):
        assert f"--target-epoch {BUNDLE_TARGET_EPOCH}" in hook.read_text()


def test_surf_109_a_mismatched_epoch_refuses_with_migration_guidance(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "hook",
            "run",
            "session_start",
            "--runtime",
            "claude",
            "--target-epoch",
            "2",
        ],
        input="{}",
    )
    assert result.exit_code == 0
    assert "targets schema epoch 2 but the repository is at epoch 1" in result.stderr
    assert "eawf migrate status" in result.stderr
    assert result.stdout == ""


def test_surf_109_the_matching_epoch_passes_and_a_newer_repo_names_the_update(
    tmp_path: Path,
) -> None:
    require_bundle_epoch(1, tmp_path)
    newer = BundleEpochMismatchError(bundle_epoch=1, repository_epoch=2)
    assert "install the bundle this eawf version renders (`eawf plugin update`)" in str(newer)
    with pytest.raises(BundleEpochMismatchError, match="migrate the repository to epoch 2"):
        require_bundle_epoch(2, tmp_path)


# ---- SURF-112 / SURF-114 -----------------------------------------------------


@pytest.mark.parametrize("skill_id", sorted(_ENTRIES))
def test_surf_112_one_schema_generates_help_schema_completion_and_validation(
    skill_id: str,
) -> None:
    entry, spec = _ENTRIES[skill_id], _SPECS[skill_id]
    schema = argument_schema(entry)
    assert spec.argument_hint == schema.usage.partition(" ")[2]
    machine = schema.json_schema()
    assert machine["additionalProperties"] is False
    assert {o.field for o in schema.options} <= set(machine["properties"])
    assert schema.completion()["options"] == [o.flag for o in schema.options]
    lane: Lane = "operator" if "operator" in skill_lanes(entry) else "agent"
    args: dict[str, Any] = {schema.subject_field: "x"}
    if schema.actions:
        args["action"] = schema.actions[0]
    check_invocation(entry, args, lane=lane)
    with pytest.raises(InvocationRefusedError, match="unknown_argument"):
        check_invocation(entry, {**args, "not_an_option": 1}, lane=lane)


def test_surf_112_an_action_incompatible_argument_fails_before_a_run(tmp_path: Path) -> None:
    entry = resolve_skill("/integrate")
    with pytest.raises(InvocationRefusedError, match="action_incompatible"):
        check_invocation(entry, {"action": "seal", "subject_ref": "c", "base": {}}, lane="operator")
    with pytest.raises(InvocationRefusedError, match="action_undeclared"):
        check_invocation(entry, {"action": "fold", "subject_ref": "c"}, lane="operator")
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "skill", "run", "/integrate"],
        input='{"action": "seal", "subject_ref": "c", "base": {}}',
    )
    assert result.exit_code == 1
    assert "action_incompatible" in result.stdout + result.stderr


def test_surf_114_one_lane_map_filters_help_agent_catalog_and_host_menu() -> None:
    agent = {e.skill_id for e in skills_for_lane("agent")}
    operator = {e.skill_id for e in skills_for_lane("operator")}
    assert "release" not in agent and "release" in operator
    for skill_id, spec in _SPECS.items():
        assert (skill_id in agent) is (not spec.disable_model_invocation)
        assert (skill_id in operator) is spec.user_invocable
    listed = runner.invoke(
        app, ["--json", "skill", "list", "--scope", "builtin", "--lane", "agent"]
    )
    assert listed.exit_code == 0, listed.stdout
    rows = orjson.loads(listed.stdout)["skills"]
    assert {row["name"].removeprefix("/") for row in rows} == agent
    integrate = next(row for row in rows if row["name"] == "/integrate")
    assert integrate["operator_only_actions"] == ["apply", "retry"]
    assert integrate["lanes"] == ["agent", "operator"]


def test_surf_114_operator_only_actions_reject_the_agent_lane_before_dispatch() -> None:
    entry = resolve_skill("/integrate")
    with pytest.raises(InvocationRefusedError, match="operator_only_action"):
        check_invocation(entry, {"action": "apply", "subject_ref": "b"}, lane="agent")
    check_invocation(entry, {"action": "apply", "subject_ref": "b"}, lane="operator")
    with pytest.raises(InvocationRefusedError, match="lane_refused"):
        check_invocation(resolve_skill("/release"), {"action": "show"}, lane="agent")
    result = runner.invoke(app, ["skill", "run", "/integrate", "--lane", "robot"], input="{}")
    assert result.exit_code == 1


# ---- SURF-113 ----------------------------------------------------------------


def test_surf_113_every_digest_is_checked_as_one_publication_unit(claude_bundle: Path) -> None:
    path = claude_bundle / PUBLICATION_FILENAME
    original = path.read_bytes()
    document = orjson.loads(original)
    document["catalog"] = "sha256:" + "0" * 64
    document["skills"][0]["prompt"] = "sha256:" + "1" * 64
    document["skills"][0]["rpc_allowlist"] = "sha256:" + "2" * 64
    document["target_epoch"] = 1
    path.write_bytes(orjson.dumps(document))
    try:
        findings = publication_findings(claude_bundle)
    finally:
        path.write_bytes(original)
    first = document["skills"][0]["skill_id"]
    assert "the skill catalog changed since the bundle was rendered" in findings
    assert f"skill {first} prompt digest no longer matches" in findings
    assert f"skill {first} rpc_allowlist digest no longer matches" in findings
    assert "the bundle targets epoch 1, this build epoch 2" in findings


def test_surf_113_a_missing_or_unparseable_publication_is_a_finding(tmp_path: Path) -> None:
    assert publication_findings(tmp_path)[0].endswith("the bundle was not rendered by eawf")
    (tmp_path / PUBLICATION_FILENAME).write_text('{"catalog": 1}')
    assert publication_findings(tmp_path)[0].startswith(f"{PUBLICATION_FILENAME} does not parse")


@pytest.mark.parametrize("token", ["TODO", "TBD", "lorem ipsum", "body deferred"])
def test_surf_113_no_placeholder_prompt_body_may_ship(token: str) -> None:
    entry = _ENTRIES["why"]
    page = _SPECS["why"].body.replace("Bind the exact subject", f"{token} Bind the exact subject")
    assert any("placeholder" in finding for finding in chassis_findings(page, entry))


# ---- SURF-092 ----------------------------------------------------------------


@pytest.mark.parametrize("skill_id", sorted(_ENTRIES))
def test_surf_092_sweep_reports_carry_a_covered_and_not_covered_block(skill_id: str) -> None:
    entry = _ENTRIES[skill_id]
    outcome = entry.output.terminal_outcomes[0]
    bare = {"skill_id": skill_id, "outcome": outcome}
    block = {"covered": ["src/eawf"], "not_covered": [{"item": "docs", "reason": "out of scope"}]}
    assert entry.output.coverage is (skill_id in _COVERAGE_SKILLS)
    if entry.output.coverage:
        with pytest.raises(ValidationError):
            entry.validate_report(bare)
        with pytest.raises(ValidationError):
            entry.validate_report({**bare, "coverage": {"covered": [], "not_covered": []}})
        report = entry.validate_report({**bare, "coverage": block})
        assert report.model_dump()["coverage"]["covered"] == ("src/eawf",)
        assert "`coverage` block" in _SPECS[skill_id].body
    else:
        entry.validate_report(bare)


def test_surf_092_check_report_is_the_machine_check_a_skill_runs() -> None:
    good = {
        "skill_id": "research",
        "outcome": "answered",
        "coverage": {"covered": ["src"], "not_covered": []},
    }
    ok = runner.invoke(app, ["skill", "check-report", "/research"], input=orjson.dumps(good))
    assert ok.exit_code == 0, ok.stdout
    bad = runner.invoke(
        app,
        ["skill", "check-report", "research", "-"],
        input=orjson.dumps({"skill_id": "research", "outcome": "answered"}),
    )
    assert bad.exit_code == 2
    assert "check coverage" in bad.stdout + bad.stderr


# ---- revision anchors, /spike provider tuple, /reflect grammar ---------------


def _seal(**args: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def caller(method: str, params: dict[str, Any]) -> dict[str, Any]:
        sent.append(params)
        return {"sealed": True}

    skill = integrate_skill.IntegrateSkill(caller=caller)
    base = {"action": "seal", "subject_ref": "cand-1", "run": "run-1", "resulting_tree_digest": "d"}
    result = skill.action(SkillContext(scope="s", session="s", args={**base, **args}))
    assert isinstance(result.body, dict)
    return result.body, sent


def test_integrate_seal_sends_the_revision_anchor_and_stops_without_one() -> None:
    body, sent = _seal(expected_revision=7)
    assert body["outcome"] == "sealed"
    assert sent[0]["expected_revision"] == 7
    body, sent = _seal()
    assert sent == []
    assert body["unresolved_request_fields"] == ["expected_revision"]


def test_spike_sets_the_provider_tuple_of_a_provider_scoped_measurement() -> None:
    assert "--provider" in _ENTRIES["spike"].grammar.options
    assert "set each contract environment's `provider_tuple`" in _SPECS["spike"].body


def test_reflect_grammar_is_exactly_the_reflect_verbs() -> None:
    entry = _ENTRIES["reflect"]
    catalog = verb_catalog()
    assert {f"reflect {action}" for action in entry.grammar.actions} == set(entry.effects.verbs)
    for action in entry.grammar.actions:
        verb = catalog.entry(f"reflect {action}")
        assert verb is not None
        flags = {flag for p in verb.parameters for flag in p.flags}
        assert set(entry.grammar.options_for(action)) <= flags
    assert not {"--window", "--scope", "--cohort", "--format"} & set(entry.grammar.options)


def test_effects_boundary_refuses_a_repeated_verb() -> None:
    with pytest.raises(ValidationError, match="duplicate verb"):
        EffectsBoundary(summary="s", canonical_mutates=True, verbs=("memory add", "memory add"))
