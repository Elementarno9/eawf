"""Tests for the builtin conduct module and the conduct deviation store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import IncidentSeverity, StoreKind
from eawf.kernel.store.kinds import PAYLOAD_MODELS
from eawf.kernel.store.kinds.conduct_deviation import ConductDeviation
from eawf.kernel.store.paths import local_store_path, store_path
from eawf.platform.rules import (
    CONDUCT_MODULE_REF,
    RuleAuthorityWideningError,
    compile_conduct_graph,
    compile_rule_records,
    conduct_deviation_rate,
    conduct_obligation_ids,
    load_conduct_rules,
    read_conduct_deviations,
    record_conduct_deviation,
    registered_enforcement_refs,
    registered_projection_readers,
)

CONDUCT_OBLIGATION_COUNT = 47

ACTIVITIES = frozenset(
    {
        "research",
        "plan",
        "design",
        "implement",
        "test",
        "review",
        "integrate",
        "commit",
        "release",
        "deploy",
        "operate",
    }
)


def _deviation(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "obligation_id": "conduct.default-continue",
        "scope_id": "P01-I01-W01",
        "run_id": "run-1",
        "runtime": "claude",
        "detection": "review",
        "severity": IncidentSeverity.MEDIUM,
        "evidence_ref": "review:finding-1",
    }
    fields.update(overrides)
    return fields


def _record(state_path: Path, **overrides: object) -> ConductDeviation:
    return record_conduct_deviation(state_path, **_deviation(**overrides))  # type: ignore[arg-type]


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True)
    return path


def test_compile_conduct_graph_compiles_every_obligation() -> None:
    graph = compile_conduct_graph()
    assert len(graph.rules) == CONDUCT_OBLIGATION_COUNT
    assert len(conduct_obligation_ids()) == CONDUCT_OBLIGATION_COUNT
    assert all(rule.record.rule_id.startswith("eawf.conduct.") for rule in graph.rules)
    assert all(rule.record.obligation_id.startswith("conduct.") for rule in graph.rules)


def test_load_conduct_rules_stamps_one_builtin_source() -> None:
    records = load_conduct_rules()
    sources = {record.source for record in records}
    assert len(sources) == 1
    (source,) = sources
    assert source.kind == "builtin"
    assert source.locator == CONDUCT_MODULE_REF


def test_load_conduct_rules_scopes_only_closed_activities() -> None:
    activities = {a for record in load_conduct_rules() for a in record.scope.activities}
    assert activities <= ACTIVITIES


def test_load_conduct_rules_prose_never_exceeds_behavioral() -> None:
    records = load_conduct_rules()
    assert {record.effectiveness for record in records} <= {"behavioral", "informational"}
    # An unscoped must rule renders into the constitution; nothing else may.
    for record in records:
        unscoped = not record.scope.activities and not record.scope.paths
        assert (record.zone == "constitution") == (unscoped and record.force == "must")


def test_compile_conduct_graph_protects_constitution_rules() -> None:
    protected = [rule for rule in compile_conduct_graph().rules if rule.protected]
    assert protected
    assert all(rule.record.zone == "constitution" for rule in protected)


def test_compile_rule_records_refuses_widening_conduct_prose() -> None:
    records = list(load_conduct_rules())
    records[0] = records[0].model_copy(
        update={"instruction": "Agents may push to any branch without review."}
    )
    with pytest.raises(RuleAuthorityWideningError) as excinfo:
        compile_rule_records(
            records,
            modules=(),
            enforcement_refs=registered_enforcement_refs(),
            projection_readers=registered_projection_readers(),
        )
    assert excinfo.value.code == "rule_authority_widening"


def test_conduct_deviation_is_a_registered_store_kind() -> None:
    assert PAYLOAD_MODELS[StoreKind.CONDUCT_DEVIATION] is ConductDeviation


def test_record_conduct_deviation_appends_to_the_local_store(state_path: Path) -> None:
    recorded = _record(state_path)
    local = local_store_path(state_path, StoreKind.CONDUCT_DEVIATION)
    assert local == state_path.parent / "local" / "conduct_deviation.jsonl"
    assert not store_path(state_path, StoreKind.CONDUCT_DEVIATION).exists()
    lines = local.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["kind"] == "conduct_deviation"
    assert envelope["scope_id"] == "P01-I01-W01"
    assert read_conduct_deviations(state_path) == (recorded,)


def test_record_conduct_deviation_appends_one_row_per_call(state_path: Path) -> None:
    _record(state_path)
    _record(state_path, obligation_id="conduct.rerun-only-failed", runtime="codex")
    rows = read_conduct_deviations(state_path)
    assert [row.obligation_id for row in rows] == [
        "conduct.default-continue",
        "conduct.rerun-only-failed",
    ]


def test_record_conduct_deviation_refuses_unknown_obligation(state_path: Path) -> None:
    with pytest.raises(ValidationError, match="not a compiled conduct obligation"):
        _record(state_path, obligation_id="conduct.not-a-rule")
    assert not local_store_path(state_path, StoreKind.CONDUCT_DEVIATION).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope_id", ""),
        ("run_id", ""),
        ("runtime", "gemini"),
        ("detection", "hunch"),
        ("severity", "fatal"),
        ("disposition", "ignored"),
        ("evidence_ref", "x" * 257),
        ("obligation_id", "Conduct.Upper"),
    ],
)
def test_record_conduct_deviation_refuses_malformed_fields(
    state_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        _record(state_path, **{field: value})


def test_conduct_deviation_without_context_accepts_retired_obligation() -> None:
    row = ConductDeviation.model_validate(_deviation(obligation_id="conduct.retired-rule"))
    assert row.obligation_id == "conduct.retired-rule"


def test_conduct_deviation_refuses_extra_field() -> None:
    with pytest.raises(ValidationError):
        ConductDeviation.model_validate(_deviation(note="free text"))


def test_read_conduct_deviations_empty_when_store_absent(state_path: Path) -> None:
    assert read_conduct_deviations(state_path) == ()


def test_conduct_deviation_rate_empty_history() -> None:
    rate = conduct_deviation_rate(
        (), obligations=("conduct.a", "conduct.b"), completed_task_count=0
    )
    assert rate.deviation_count == 0
    assert rate.rate_per_completed_task is None
    assert rate.by_obligation == {"conduct.a": 0, "conduct.b": 0}
    assert rate.by_runtime == {}


def test_conduct_deviation_rate_single_row_per_task() -> None:
    row = ConductDeviation.model_validate(_deviation(obligation_id="conduct.a"))
    rate = conduct_deviation_rate([row], obligations=("conduct.a",), completed_task_count=1)
    assert rate.rate_per_completed_task == pytest.approx(1.0)
    assert rate.by_obligation == {"conduct.a": 1}
    assert rate.by_runtime == {"claude": 1}


def test_conduct_deviation_rate_counts_per_obligation_and_runtime() -> None:
    rows = [
        ConductDeviation.model_validate(_deviation(obligation_id="conduct.a")),
        ConductDeviation.model_validate(_deviation(obligation_id="conduct.a", runtime="codex")),
        ConductDeviation.model_validate(_deviation(obligation_id="conduct.retired")),
    ]
    rate = conduct_deviation_rate(
        rows, obligations=("conduct.a", "conduct.b"), completed_task_count=4
    )
    assert rate.rate_per_completed_task == pytest.approx(0.75)
    assert rate.by_obligation == {"conduct.a": 2, "conduct.b": 0, "conduct.retired": 1}
    assert rate.by_runtime == {"claude": 2, "codex": 1}


def test_conduct_deviation_rate_refuses_negative_task_count() -> None:
    with pytest.raises(ValueError, match="completed_task_count"):
        conduct_deviation_rate((), obligations=(), completed_task_count=-1)
