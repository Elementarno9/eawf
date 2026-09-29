"""The statusline renders truth fields: sourced, attributed and never a bare unknown.

MEAS-028 and MEAS-043 bind every segment to a truth field carrying a producer, a
freshness value and a measurement quality, and forbid a segment from rendering a
value it cannot source or a bare unknown token. MEAS-029 makes the context and cost
segments read the host payload directly. MEAS-030 resolves the scope segment through
the epoch-2 delivery spine: the host's session id, the Run bound to it in the
selected generation, and the record that Run is scoped to.

Every canary here is provisioned through the production provisioning path under the
test's own tmp directory.
"""

from __future__ import annotations

import ast
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.projection.truth import Freshness, Precision, TruthField, TruthKind, TruthState
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import (
    budget,
    context_tokens,
    cost,
    hooks_plugins,
    mcp_health,
    memory,
    scope,
)
from eawf.runtime.runtimes.claude.statusline_modules._host import HOST_PRODUCER
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.surfaces.render.statusline import (
    BARE_UNKNOWN_TOKENS,
    SegmentSource,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

SEED_RECORDS: Final = (
    Path(__file__).resolve().parents[4]
    / "fixtures"
    / "epoch2"
    / "transitions"
    / "seed_records.yaml"
)
PROVISIONED_AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SESSION_ID: Final = "sess-truth-0001"
SOURCE: Final = SegmentSource(producer="test-producer", provenance="test#field")

HOST_PAYLOAD: Final[dict[str, Any]] = {
    "session_id": SESSION_ID,
    "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
    "context_window": {
        "context_window_size": 200_000,
        "current_usage": {
            "input_tokens": 1_200,
            "output_tokens": 300,
            "cache_creation_input_tokens": 800,
            "cache_read_input_tokens": 40_000,
        },
    },
    "cost": {"total_cost_usd": 1.2345, "total_duration_ms": 5_000},
}


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


def run_row(key: str, *, session_id: str | None, task_key: str, updated_at: str) -> dict[str, Any]:
    """Return a RUNNING seed Run keyed ``key`` scoped to ``task_key``."""
    document = yaml.safe_load(SEED_RECORDS.read_text(encoding="utf-8"))
    row: dict[str, Any] = dict(document["records"]["run"]["RUNNING"])
    row["scope"] = dict(row["scope"])
    row["key"] = key
    row["urn"] = f"{str(row['urn']).rsplit('/', 1)[0]}/{key}"
    row["scope"]["task_ref"] = f"{str(row['scope']['task_ref']).rsplit('/', 1)[0]}/{task_key}"
    row["updated_at"] = updated_at
    if session_id is not None:
        row["vendor_session"] = {
            "harness": "claude-code",
            "session_digest": hash_vendor_session_id(session_id),
        }
    return row


def canary_state_path(tmp_path: Path, runs: dict[str, dict[str, Any]]) -> Path:
    """Provision a canary whose selected generation holds ``runs``; return its state path."""
    repo = tmp_path / "canary"
    provision_canary(repo_root=repo, ref=canary_ref("SLT"), provisioned_at=PROVISIONED_AT)
    tree = (repo / ".ea").resolve()
    authority = require_native_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(path)
    document.setdefault("run", {}).update(runs)
    write_document(path, document)
    return tree / "state.json"


def generation_document(state_path: Path) -> Path:
    """Return the selected generation's document of the canary at ``state_path``."""
    authority = require_native_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def assert_truthful(segment: StatuslineSegment) -> None:
    """Assert ``segment`` is a truth field that renders no unsourced value."""
    truth = segment.truth
    assert isinstance(truth, TruthField)
    assert truth.producer
    assert isinstance(truth.freshness, Freshness)
    assert isinstance(truth.measurement_quality, MeasurementQuality)
    tokens = segment.text.replace(":", " ").replace("/", " ").split()
    assert not BARE_UNKNOWN_TOKENS & set(tokens), segment.text
    if truth.state is TruthState.KNOWN:
        assert truth.value
        assert truth.provenance_refs
    else:
        assert f"n/a({truth.missing_reason})" in segment.text


# ---- MEAS-028: every segment is a truth field ------------------------------


def test_meas_028_every_segment_is_a_truth_field_with_producer_freshness_and_quality(
    tmp_path: Path,
) -> None:
    run = run_row(
        "RUN-00000010",
        session_id=SESSION_ID,
        task_key="EAWF-0126",
        updated_at=("2026-09-08T03:00:00Z"),
    )
    state_path = canary_state_path(tmp_path, {"RUN-00000010": run})
    payload = {**HOST_PAYLOAD, "cwd": str(tmp_path)}

    segments = orchestrator._build_segments(payload, state_path)

    assert [segment.module for segment in segments] == [
        "scope",
        "budget",
        "git",
        "model_session_cwd",
        "context_tokens",
        "cost",
        "mcp_health",
        "hooks_plugins",
        "memory",
        "token_saving",
    ]
    for segment in segments:
        assert_truthful(segment)
        assert segment.truth.freshness is Freshness.LIVE
    by_module = {segment.module: segment for segment in segments}
    assert by_module["scope"].text == "scope:EAWF-0126"
    assert by_module["context_tokens"].text == "ctx:42.0k/200.0k"
    assert by_module["token_saving"].text == "save:95%"
    assert by_module["cost"].text == "cost:$1.23"


def test_meas_028_cost_is_an_estimate_not_an_exact_figure() -> None:
    segment = cost.build(HOST_PAYLOAD, None)

    assert segment.truth.producer == HOST_PRODUCER
    assert segment.truth.measurement_quality is MeasurementQuality.ESTIMATED
    assert segment.truth.precision is Precision.APPROXIMATE
    assert segment.truth.truth_kind is TruthKind.DERIVED


def test_meas_028_known_segment_without_a_value_is_refused() -> None:
    truth = sourced_segment("probe", "probe", "x", SOURCE).truth.model_copy(update={"value": ""})

    with pytest.raises(ValueError, match="states no value"):
        StatuslineSegment(module="probe", text="probe:", truth=truth)


def test_meas_028_unavailable_segment_without_its_marker_is_refused() -> None:
    truth = unavailable_segment("probe", "probe", "no-source", SOURCE).truth

    with pytest.raises(ValueError, match=r"does not render n/a\(no-source\)"):
        StatuslineSegment(module="probe", text="probe:0", truth=truth, status="missing")


@pytest.mark.parametrize("reason", ["", "two words", "tab\there"])
def test_meas_028_unavailable_reason_must_be_one_token(reason: str) -> None:
    with pytest.raises(ValueError, match="one non-empty token"):
        unavailable_segment("probe", "probe", reason, SOURCE)


def test_meas_028_single_character_value_is_a_value() -> None:
    segment = sourced_segment("probe", "probe", "0", SOURCE)

    assert segment.text == "probe:0"
    assert segment.truth.state is TruthState.KNOWN


def test_meas_028_module_that_raises_renders_a_failed_truth_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
        raise RuntimeError("boom")

    monkeypatch.setattr(cost, "build", boom)

    segments = orchestrator._build_segments({}, None)

    failed = next(segment for segment in segments if segment.module == "cost")
    assert failed.status == "failed"
    assert failed.text == "cost:n/a(render-failed)"
    assert_truthful(failed)


# ---- MEAS-029: context and cost read the host payload ----------------------


def test_meas_029_context_segment_reads_the_host_payload_not_the_transcript(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"message": {"usage": {"input_tokens": 999_999, "output_tokens": 1}}}) + "\n",
        encoding="utf-8",
    )
    payload = {**HOST_PAYLOAD, "transcript_path": str(transcript)}

    segment = context_tokens.build(payload, None)

    assert segment.text == "ctx:42.0k/200.0k"
    assert segment.truth.producer == HOST_PRODUCER
    assert segment.truth.provenance_refs == ("statusline-payload#context_window.current_usage",)
    assert segment.truth.measurement_quality is MeasurementQuality.EXACT


def test_meas_029_context_segment_without_a_window_size_renders_the_count() -> None:
    payload = {"context_window": {"current_usage": HOST_PAYLOAD["context_window"]["current_usage"]}}

    assert context_tokens.build(payload, None).text == "ctx:42.0k"


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({}, "ctx:n/a(no-current-usage)"),
        ({"context_window": {"current_usage": None}}, "ctx:n/a(no-current-usage)"),
        (
            {"context_window": {"current_usage": {"input_tokens": 10}}},
            "ctx:n/a(partial-usage)",
        ),
    ],
)
def test_meas_029_context_without_complete_host_usage_is_unavailable_not_zero(
    payload: dict[str, Any], text: str
) -> None:
    segment = context_tokens.build(payload, None)

    assert segment.text == text
    assert segment.truth.state is TruthState.UNAVAILABLE


def test_meas_029_cost_segment_reads_the_host_cost() -> None:
    assert cost.build(HOST_PAYLOAD, None).text == "cost:$1.23"
    assert cost.build({"cost": {"total_cost_usd": 0}}, None).text == "cost:$0.00"


@pytest.mark.parametrize(
    "payload",
    [{}, {"cost": {}}, {"cost": {"total_cost_usd": -1}}, {"cost": {"total_cost_usd": "1"}}],
)
def test_meas_029_unreported_cost_is_unavailable_never_zero(payload: dict[str, Any]) -> None:
    segment = cost.build(payload, None)

    assert segment.text == "cost:n/a(no-cost-reported)"
    assert segment.truth.value is None


def test_meas_029_no_statusline_module_imports_a_transcript_reader() -> None:
    package = Path(context_tokens.__file__).parent
    imported: dict[str, set[str]] = {}
    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.setdefault(path.name, set()).add(node.module)
            elif isinstance(node, ast.Import):
                imported.setdefault(path.name, set()).update(alias.name for alias in node.names)

    readers = sorted(
        name
        for name, modules in imported.items()
        if any(
            "transcript" in module or "observability.measurement" in module for module in modules
        )
    )

    assert "context_tokens.py" in imported
    assert readers == []


# ---- MEAS-030: the scope resolves through the epoch-2 delivery spine -------


def test_meas_030_scope_resolves_the_session_run_through_the_selected_generation(
    tmp_path: Path,
) -> None:
    run = run_row(
        "RUN-00000010",
        session_id=SESSION_ID,
        task_key="EAWF-0126",
        updated_at=("2026-09-08T03:00:00Z"),
    )
    other = run_row(
        "RUN-00000011",
        session_id="another-session",
        task_key="EAWF-0999",
        updated_at=("2026-09-08T04:00:00Z"),
    )
    state_path = canary_state_path(tmp_path, {"RUN-00000010": run, "RUN-00000011": other})

    segment = scope.build({"session_id": SESSION_ID}, state_path)

    assert segment.text == "scope:EAWF-0126"
    assert segment.truth.producer == scope.SPINE_PRODUCER
    assert segment.truth.provenance_refs == (run["urn"],)
    assert segment.truth.producer_revision == run["revision"]
    assert segment.truth.truth_kind is TruthKind.STORED


def test_meas_030_scope_takes_the_most_recently_updated_run_of_the_session(
    tmp_path: Path,
) -> None:
    older = run_row(
        "RUN-00000010",
        session_id=SESSION_ID,
        task_key="EAWF-0100",
        updated_at=("2026-09-08T03:00:00Z"),
    )
    newer = run_row(
        "RUN-00000011",
        session_id=SESSION_ID,
        task_key="EAWF-0126",
        updated_at=("2026-09-08T05:00:00Z"),
    )
    state_path = canary_state_path(tmp_path, {"RUN-00000010": older, "RUN-00000011": newer})

    assert scope.build({"session_id": SESSION_ID}, state_path).text == "scope:EAWF-0126"


def test_meas_030_scope_never_reads_the_epoch1_wave_pointers(tmp_path: Path) -> None:
    state_path = canary_state_path(tmp_path, {})
    state_path.write_text(
        json.dumps({"current": {"active_wave_ids": ["P04-I01-W06"], "phase_id": "P04"}}),
        encoding="utf-8",
    )

    segment = scope.build({"session_id": SESSION_ID}, state_path)

    assert segment.text == "scope:n/a(no-run-for-session)"


def test_meas_030_scope_without_a_workspace_names_why() -> None:
    assert scope.build(HOST_PAYLOAD, None).text == "scope:n/a(no-workspace)"


def test_meas_030_scope_on_an_epoch1_tree_names_the_authority_gap(tmp_path: Path) -> None:
    tree = tmp_path / ".ea"
    tree.mkdir()
    state_path = tree / "state.json"
    state_path.write_text(json.dumps({"current": {"phase_id": "P04"}}), encoding="utf-8")

    segment = scope.build(HOST_PAYLOAD, state_path)

    assert segment.text == "scope:n/a(epoch1-undeclared)"
    assert segment.status == "missing"


@pytest.mark.parametrize("session_id", [None, "", 7])
def test_meas_030_scope_without_a_host_session_names_why(
    tmp_path: Path, session_id: object
) -> None:
    state_path = canary_state_path(tmp_path, {})

    segment = scope.build({"session_id": session_id}, state_path)

    assert segment.text == "scope:n/a(no-session)"


def test_meas_030_scope_with_an_unreadable_generation_names_why(tmp_path: Path) -> None:
    state_path = canary_state_path(tmp_path, {})
    generation_document(state_path).write_text("[]", encoding="utf-8")

    assert scope.build(HOST_PAYLOAD, state_path).text == "scope:n/a(generation-unreadable)"


def test_meas_030_scope_with_an_invalid_run_row_names_why(tmp_path: Path) -> None:
    run = run_row(
        "RUN-00000010",
        session_id=SESSION_ID,
        task_key="EAWF-0126",
        updated_at=("2026-09-08T03:00:00Z"),
    )
    run["status"] = "NOT-A-STATUS"
    state_path = canary_state_path(tmp_path, {"RUN-00000010": run})

    assert scope.build(HOST_PAYLOAD, state_path).text == "scope:n/a(run-invalid)"


def test_meas_030_a_run_without_a_vendor_session_binds_no_session(tmp_path: Path) -> None:
    run = run_row(
        "RUN-00000010", session_id=None, task_key="EAWF-0126", updated_at=("2026-09-08T03:00:00Z")
    )
    state_path = canary_state_path(tmp_path, {"RUN-00000010": run})

    assert scope.build(HOST_PAYLOAD, state_path).text == "scope:n/a(no-run-for-session)"


# ---- MEAS-043: every segment from an epoch-2 source, no bare unknown -------


@pytest.mark.parametrize("state", ["none", "epoch1-empty", "canary"])
def test_meas_043_no_segment_renders_a_bare_unknown_token(tmp_path: Path, state: str) -> None:
    state_path: Path | None = None
    if state == "epoch1-empty":
        (tmp_path / ".ea").mkdir()
        state_path = tmp_path / ".ea" / "state.json"
        state_path.write_text("{}", encoding="utf-8")
    elif state == "canary":
        state_path = canary_state_path(tmp_path, {})

    segments = orchestrator._build_segments({"cwd": str(tmp_path / "gone")}, state_path)

    assert len(segments) == len(orchestrator._MODULE_ORDER)
    for segment in segments:
        assert_truthful(segment)


def test_meas_043_document_segments_never_read_a_frozen_epoch1_document(tmp_path: Path) -> None:
    state_path = canary_state_path(tmp_path, {})
    state_path.write_text(
        json.dumps(
            {
                "mcp_servers": {"a": {"status": "up"}},
                "memory_index": {"m": {}},
                "plugins": {"p": {}},
                "current": {"active_wave_ids": ["W1"]},
                "waves": {"W1": {"token_budget": 10, "tokens_consumed": 1}},
            }
        ),
        encoding="utf-8",
    )

    assert mcp_health.build({}, state_path).text == "mcp:n/a(no-epoch2-source)"
    assert memory.build({}, state_path).text == "mem:n/a(no-epoch2-source)"
    assert hooks_plugins.build({}, state_path).text == "hooks:0 plugins:n/a(no-epoch2-source)"
    budget_segment = budget.build({}, state_path)
    assert budget_segment.text == "budget:n/a(no-epoch2-source)"
    assert budget_segment.truth.state is TruthState.UNAVAILABLE


@pytest.mark.parametrize("token", sorted(BARE_UNKNOWN_TOKENS))
def test_meas_043_a_bare_unknown_token_is_refused(token: str) -> None:
    truth = sourced_segment("probe", "probe", "x", SOURCE).truth

    with pytest.raises(ValueError, match="bare unknown token"):
        StatuslineSegment(module="probe", text=f"probe:{token}", truth=truth)
