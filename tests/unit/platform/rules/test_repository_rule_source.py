"""The committed rule source of this repository compiles and covers every current rule."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from eawf.platform.rules import (
    RuleGraph,
    builtin_rule_modules,
    compile_rule_graph,
    compile_rule_records,
    load_rule_source,
    registered_enforcement_refs,
    registered_projection_readers,
    select_rule_modules,
)
from eawf.platform.rules.carriers import carrier_target
from eawf.platform.rules.host_facts import ProjectionKind
from eawf.platform.rules.records import RuleRecord
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    builtin_rule_provider,
    plan_rule_projections,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCS_RULES = _REPO_ROOT / "docs" / "rules"

# Every numbered rule of the hand-assembled AGENTS.md list this repository
# carried before its card was rendered from the rule graph, by number, to the
# one obligation that now carries it. Pinned here because the rendered card no
# longer numbers its rules.
_NUMBERED_RULE_OWNERS: dict[int, str] = {
    1: "architecture.cli-dispatch",
    2: "config.strict-validation",
    3: "state.ea-directory-commit-policy",
    4: "state.canonical-mutator",
    5: "writing.symbol-conventions",
    6: "integrity.deletion",
    7: "state.state-vs-specs",
    8: "integrity.verify-before-claim",
    9: "craft.python.string-interpolation",
    10: "craft.python.project-runner",
    11: "vcs.worktree-discipline",
    12: "vcs.branch-currency",
    13: "vcs.pre-commit",
    14: "vcs.commit-scope-carrier",
    15: "vcs.branch-naming",
    16: "integrity.secrets-hygiene",
    17: "naming.canonical-names",
    18: "writing.artifact-chassis",
    19: "integrity.agent-report",
    20: "state.scope-revisability",
    21: "lifecycle.roadmap-procedure",
    22: "lifecycle.spike",
    23: "engineering.simplicity",
    24: "engineering.least-surprise",
    25: "writing.source-provenance",
    26: "lifecycle.prep-plan-mode",
    27: "lifecycle.close-timing",
    28: "writing.markdown-no-manual-wrap",
    29: "lifecycle.release-process",
    30: "lifecycle.ship-process",
    31: "writing.comment-economy",
    32: "integrity.decision-surface",
    33: "integrity.gate-fire-proof",
    34: "vcs.commit-granularity",
}

# Every docs/rules page, by file stem, to the obligation whose rule points at
# it as its procedure.
_DOCS_RULE_OWNERS: dict[str, str] = {
    "agent-driven-cadence-adr-pointer": "delivery.cadence-provenance",
    "agent-driven-large-phase-pr": "delivery.phase-pr-size",
    "agent-driven-phase-equals-release": "release.phase-cadence",
    "artifact-chassis": "writing.artifact-chassis",
    "clarity-contract": "writing.newcomer-clarity",
    "code-craft-dry": "craft.code.duplication",
    "code-craft-explicit-over-implicit": "craft.code.explicit-over-implicit",
    "code-craft-fail-fast": "craft.code.fail-fast",
    "code-craft-single-responsibility": "craft.code.single-responsibility",
    "comment-economy": "writing.comment-economy",
    "commit-granularity": "vcs.commit-granularity",
    "commit-prefix": "vcs.commit-scope-carrier",
    "engineering-practice": "engineering.least-surprise",
    "engineering-principles": "engineering.simplicity",
    "entity-title-naming": "writing.entity-title",
    "gate-fire-proof-sunset": "integrity.gate-fire-proof",
    "lean-wave-verification": "lifecycle.wave-verification-scope",
    "memory-hygiene": "state.memory-hygiene",
    "naming-conventions": "naming.canonical-names",
    "orchestrator-decision-surface": "integrity.decision-surface",
    "planned-scope-revisability": "state.scope-revisability",
    "release-process": "lifecycle.release-process",
    "ship-process": "lifecycle.ship-process",
    "spike-workflow": "lifecycle.spike",
    "workflow-lifecycle": "lifecycle.flow",
}

# Legacy AGENTS.md sections outside the numbered list and the docs pages.
_SECTION_OWNERS: dict[str, tuple[str, ...]] = {
    "PR template": ("vcs.pr-body",),
    "Agent tool discipline": ("integrity.agent-tool-discipline",),
    "Python style (python profile)": (
        "craft.python.string-interpolation",
        "craft.python.future-annotations",
        "craft.python.module-logger",
        "craft.python.project-runner",
        "craft.python.dependency-addition",
    ),
    "Test discipline (python profile)": (
        "craft.test.float-comparison",
        "craft.test.boundary-and-error-paths",
        "craft.test.naming",
        "craft.test.run-scope",
    ),
    "Research workflow (research profile)": ("research.evidence-chain",),
}


def _repository_records() -> tuple[tuple[RuleRecord, ...], tuple[str, ...]]:
    loaded = load_rule_source(_REPO_ROOT)
    selection = select_rule_modules(loaded)
    unavailable = [entry.reference for entry in selection.entries if entry.module is None]
    assert unavailable == [], f"selected modules with no registered module: {unavailable}"
    return (*selection.records, *loaded.rules), loaded.modules


def _compile(records: Iterable[RuleRecord], modules: Iterable[str]) -> RuleGraph:
    return compile_rule_records(
        records,
        modules=modules,
        enforcement_refs=registered_enforcement_refs(),
        projection_readers=registered_projection_readers(),
    )


@pytest.fixture(scope="module")
def repository_graph() -> RuleGraph:
    records, modules = _repository_records()
    return _compile(records, modules)


def _owners(graph: RuleGraph) -> dict[str, RuleRecord]:
    owners: dict[str, RuleRecord] = {}
    for rule in graph.rules:
        assert rule.record.obligation_id not in owners, rule.record.obligation_id
        owners[rule.record.obligation_id] = rule.record
    return owners


def _uncovered_numbered_rules(graph: RuleGraph) -> list[int]:
    owned = {rule.record.obligation_id for rule in graph.rules}
    return [number for number, owner in _NUMBERED_RULE_OWNERS.items() if owner not in owned]


@pytest.fixture(scope="module")
def production_graph() -> RuleGraph:
    selection = select_rule_modules(load_rule_source(_REPO_ROOT))
    return compile_rule_graph(_REPO_ROOT, builtin_rules=builtin_rule_provider(selection))


@pytest.fixture(scope="module")
def projections() -> dict[str, str]:
    return dict(plan_rule_projections(_REPO_ROOT).outputs)


def test_repository_rule_source_compiles_clean(repository_graph: RuleGraph) -> None:
    records, modules = _repository_records()
    assert len(repository_graph.rules) == len(records)
    assert repository_graph.modules == tuple(sorted(modules))
    repository_only = compile_rule_graph(_REPO_ROOT)
    assert {rule.record.source.kind for rule in repository_only.rules} <= {
        "builtin",
        "repository",
    }


def test_repository_rule_source_covers_every_numbered_agents_rule(
    repository_graph: RuleGraph,
) -> None:
    assert sorted(_NUMBERED_RULE_OWNERS) == list(range(1, 35))
    owners = _owners(repository_graph)
    assert _uncovered_numbered_rules(repository_graph) == []
    assert len(set(_NUMBERED_RULE_OWNERS.values())) == len(_NUMBERED_RULE_OWNERS)
    for obligation in _NUMBERED_RULE_OWNERS.values():
        assert obligation in owners


def test_repository_rule_source_covers_every_docs_rule_page(
    repository_graph: RuleGraph,
) -> None:
    pages = {path.stem for path in _DOCS_RULES.glob("*.md")}
    assert pages == set(_DOCS_RULE_OWNERS)
    owners = _owners(repository_graph)
    for stem, obligation in _DOCS_RULE_OWNERS.items():
        assert owners[obligation].procedure_ref == f"docs/rules/{stem}.md"


@pytest.mark.parametrize("number", sorted(_NUMBERED_RULE_OWNERS))
def test_repository_rule_source_dropping_a_rule_reds_coverage(number: int) -> None:
    records, modules = _repository_records()
    dropped = _NUMBERED_RULE_OWNERS[number]
    kept = [record for record in records if record.obligation_id != dropped]
    assert len(kept) == len(records) - 1
    assert _uncovered_numbered_rules(_compile(kept, modules)) == [number]


def test_repository_rule_source_procedure_refs_resolve(repository_graph: RuleGraph) -> None:
    missing = [
        rule.record.rule_id
        for rule in repository_graph.rules
        if rule.record.procedure_ref is not None
        and not (_REPO_ROOT / rule.record.procedure_ref).is_file()
    ]
    assert missing == []


def test_repository_rule_source_must_rules_have_a_delivery(
    production_graph: RuleGraph, projections: dict[str, str]
) -> None:
    targets: dict[ProjectionKind, str] = {"card": CARD_TARGET, "policy": POLICY_TARGET}
    readers = registered_projection_readers()
    undelivered = []
    for rule in production_graph.rules:
        record = rule.record
        if record.force != "must" or record.enforcement_ref is not None:
            continue
        if record.scope.roles:
            for role in record.scope.roles:
                if record.instruction not in projections.get(carrier_target(role), ""):
                    undelivered.append(f"{record.rule_id} -> {role} carrier")
            continue
        for runtime, kinds in readers.items():
            if not any(record.instruction in projections[targets[kind]] for kind in kinds):
                undelivered.append(f"{record.rule_id} -> {runtime}")
    assert undelivered == []


def test_repository_rule_source_splits_generic_and_repository_rules(
    repository_graph: RuleGraph,
) -> None:
    for rule in repository_graph.rules:
        namespace = rule.record.rule_id.split(".", 1)[0]
        expected = "repository" if namespace == "repo" else "builtin"
        assert rule.record.source.kind == expected, rule.record.rule_id
    core = [
        module
        for reference, module in builtin_rule_modules().items()
        if reference.startswith("eawf.core.")
    ]
    assert core
    assert all(module.document.kind == "core" for module in core)


def test_repository_rule_source_every_legacy_obligation_reaches_a_projection(
    production_graph: RuleGraph, projections: dict[str, str]
) -> None:
    owners = _owners(production_graph)
    legacy = {
        *_NUMBERED_RULE_OWNERS.values(),
        *_DOCS_RULE_OWNERS.values(),
        *(obligation for section in _SECTION_OWNERS.values() for obligation in section),
    }
    unreached = []
    for obligation in sorted(legacy):
        record = owners[obligation]
        if record.instruction in projections[POLICY_TARGET]:
            continue
        module = record.source.locator.split("@", 1)[0]
        indexed = f"`{module}`" in projections[CARD_TARGET]
        if not (indexed and record.zone == "retrievable" and record.force != "must"):
            unreached.append(obligation)
    assert unreached == []


def test_repository_rule_source_card_carries_every_constitution_rule(
    production_graph: RuleGraph, projections: dict[str, str]
) -> None:
    card = projections[CARD_TARGET]
    constitution = [
        rule.record for rule in production_graph.rules if rule.record.zone == "constitution"
    ]
    assert constitution
    assert [record.rule_id for record in constitution if record.instruction not in card] == []
