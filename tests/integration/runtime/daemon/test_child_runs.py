"""Child Runs: the subtree ceiling, a fresh capsule under the parent, and Activity.

RUN-022: a delegated Run is refused at create once its subtree would pass a
``child_runs`` ceiling of any Run above it, counting children the ledger already holds;
a host subagent, which is running before it is adopted, is admitted and the breach is
filed on the run ledger instead of being refused.

RUN-023: a dispatched child is sealed a capsule of its own that names its parent and
holds no tool its parent's sealed capsule did not, and an adopted child's stream carries
only its own transcript.

SURF-097: a finished Run stays in the Activity view after it is compacted out of the
document, so the recently finished bucket counts it.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.activity import ActivityExceptionBucket, group_runs
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.delegation import ChildCeilingBreach
from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind
from eawf.kernel.state.epoch2.measurement import CounterName
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.measurement.fold import CounterFold
from eawf.observability.reflect.runs import read_tree_runs
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_create import CreateRequest, run_create
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.host_subagent import (
    HOST_SUBAGENT_START_METHOD,
    HOST_SUBAGENT_STOP_METHOD,
)
from eawf.runtime.daemon.native_dispatch import run_binding_of
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from tests.integration.runtime.daemon import test_native_dispatch as nd
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)
from tests.unit.kernel.runtime.test_authority_capsule import review_fields

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
HOST_SESSION: Final = "30f683b4-388a-4c04-89f4-612b7fe60362"
ROOT_KEY: Final = "RUN-00000010"


def urn(key: str) -> str:
    return f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{key}"


def repository_row() -> dict[str, Any]:
    return {
        "key": "REP-EAWF",
        "urn": REPOSITORY_URN,
        "revision": 1,
        "head_sha": "a" * 40,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }


def observing_scope() -> dict[str, Any]:
    return {"scope_kind": "repository", "repository_ref": REPOSITORY_URN, "purpose": "observe"}


def run_row(key: str, *, status: str = "RUNNING", **fields: Any) -> dict[str, Any]:
    """Return a Run row keyed *key*, repository-scoped unless *fields* say otherwise."""
    row = seed_row("run", status)
    row.update({"key": key, "urn": urn(key), "scope": observing_scope(), **fields})
    return row


@pytest.fixture(autouse=True)
def hermetic_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))


def canary_with(tmp_path: Path, runs: dict[str, dict[str, Any]]) -> CanaryProvision:
    provisioned = provision(tmp_path / "repo", code="KIDS")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.RUN.value: runs,
        },
    )
    return provisioned


def run_records(canary: CanaryProvision) -> tuple[LedgerRecord, ...]:
    return read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))


def append_run_line(canary: CanaryProvision, record: LedgerRecord) -> None:
    append_ledger_record(ledger_path(document_path(canary), Epoch2Collection.RUN), record)


def bind_ceiling(canary: CanaryProvision, key: str, *, child_runs: int) -> None:
    """File a dispatch binding for *key* whose sealed capsule resolves *child_runs*."""
    budget = {"wall_seconds": 2400, "output_bytes": 1_048_576, "child_runs": child_runs}
    capsule = AuthorityCapsule.seal(review_fields(run_ref=urn(key), budget=budget))
    binding = RunBinding(
        run_ref=parse_qualified_urn(urn(key)),
        compiled_spec_digest=capsule.compiled_spec_digest,
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=1,
        bound_at=AT,
        capsule=capsule,
    )
    append_run_line(
        canary,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"BND-{key}",
            status="bound",
            recorded_at=AT,
            payload=binding.model_dump(mode="json"),
        ),
    )


def compact(canary: CanaryProvision, row: dict[str, Any]) -> None:
    """File a finished Run into the run ledger the way terminal compaction does."""
    append_run_line(
        canary,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=row["key"],
            status=row["status"],
            recorded_at=AT,
            payload=row,
        ),
    )


def create_child(canary: CanaryProvision, tmp_path: Path, key: str, *, parent: str) -> None:
    """Create a repository-scoped Run delegated by *parent*."""
    cursor = read_document(document_path(canary)).get("canonical_sequence", 0)
    run_create(
        context=root_context(canary, tmp_path / "runtime"),
        request=CreateRequest(
            urn=parse_qualified_urn(urn(key)),
            expected_revision=cursor,
            idempotency_key=f"create-{key}",
            actor=ACTOR,
            spec={"key": key, "scope": observing_scope(), "parent_run_ref": urn(parent)},
        ),
        now=AT,
    )


def refused_create(
    canary: CanaryProvision, tmp_path: Path, key: str, *, parent: str
) -> TransactionRefusedError:
    before = document_path(canary).read_bytes()
    with pytest.raises(TransactionRefusedError) as caught:
        create_child(canary, tmp_path, key, parent=parent)
    assert document_path(canary).read_bytes() == before
    return caught.value


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def breaches(canary: CanaryProvision) -> list[ChildCeilingBreach]:
    return [
        ChildCeilingBreach.model_validate(item.payload)
        for item in run_records(canary)
        if item.payload.get("payload_kind") == "child_ceiling_breach"
    ]


def host_parent(**fields: Any) -> dict[str, Any]:
    """Return the live Run whose vendor session spawns the host's subagents."""
    session = {"harness": "claude-code", "session_digest": hash_vendor_session_id(HOST_SESSION)}
    return run_row(ROOT_KEY, vendor_session=session, **fields)


# ---- RUN-022: a delegation is refused past the subtree ceiling -----------------------


def test_run_022_task_scoped_parent_admits_no_child(tmp_path: Path) -> None:
    task_parent = seed_row("run", "RUNNING")
    canary = canary_with(tmp_path, {ROOT_KEY: task_parent})

    refusal = refused_create(canary, tmp_path, "RUN-00000011", parent=ROOT_KEY)

    assert refusal.code.value == "transition_guard_failed"
    assert refusal.guard == "child_runs_ceiling"
    assert "child_runs=0" in refusal.detail
    assert "RUN-00000011" not in document_rows(
        read_document(document_path(canary)), Epoch2Collection.RUN
    )


def test_run_022_child_within_the_sealed_ceiling_is_created(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: run_row(ROOT_KEY)})
    bind_ceiling(canary, ROOT_KEY, child_runs=1)

    create_child(canary, tmp_path, "RUN-00000011", parent=ROOT_KEY)

    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.RUN)
    assert rows["RUN-00000011"]["parent_run_ref"] == urn(ROOT_KEY)
    assert breaches(canary) == []


def test_run_022_second_child_passes_a_sealed_ceiling_of_one(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: run_row(ROOT_KEY)})
    bind_ceiling(canary, ROOT_KEY, child_runs=1)
    create_child(canary, tmp_path, "RUN-00000011", parent=ROOT_KEY)

    refusal = refused_create(canary, tmp_path, "RUN-00000012", parent=ROOT_KEY)

    assert "subtree 2 Runs" in refusal.detail


def test_run_022_grandchild_spends_the_roots_allowance(tmp_path: Path) -> None:
    # The child resolves the full read-only ceiling of its own, but the root sealed
    # two for its whole subtree: a child and one grandchild fill it.
    canary = canary_with(tmp_path, {ROOT_KEY: run_row(ROOT_KEY)})
    bind_ceiling(canary, ROOT_KEY, child_runs=2)
    create_child(canary, tmp_path, "RUN-00000011", parent=ROOT_KEY)
    create_child(canary, tmp_path, "RUN-00000012", parent="RUN-00000011")

    refusal = refused_create(canary, tmp_path, "RUN-00000013", parent="RUN-00000011")

    assert refusal.detail.startswith(f"run {urn(ROOT_KEY)!r} resolves child_runs=2")


def test_run_022_finished_children_in_the_ledger_still_count(tmp_path: Path) -> None:
    finished = run_row("RUN-00000011", status="COMPLETED", parent_run_ref=urn(ROOT_KEY))
    canary = canary_with(tmp_path, {ROOT_KEY: run_row(ROOT_KEY)})
    compact(canary, finished)
    bind_ceiling(canary, ROOT_KEY, child_runs=1)

    refusal = refused_create(canary, tmp_path, "RUN-00000012", parent=ROOT_KEY)

    assert refusal.guard == "child_runs_ceiling"


def test_run_022_root_without_a_sealed_capsule_resolves_from_its_scope(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: run_row(ROOT_KEY)})

    for ordinal in range(11, 11 + 32):
        create_child(canary, tmp_path, f"RUN-{ordinal:08d}", parent=ROOT_KEY)
    refusal = refused_create(canary, tmp_path, "RUN-00000043", parent=ROOT_KEY)

    assert "child_runs=32" in refusal.detail
    assert "subtree 33 Runs" in refusal.detail


# ---- RUN-022: a host subagent is admitted past the ceiling and the breach filed ---------


def test_run_022_host_subagent_past_the_ceiling_is_adopted_and_breach_filed(
    tmp_path: Path,
) -> None:
    task_scope = seed_row("run", "RUNNING")["scope"]
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent(scope=task_scope)})

    answer = call(
        canary,
        tmp_path,
        HOST_SUBAGENT_START_METHOD,
        harness="claude-code",
        agent_id="subagent02",
        host_session_id=HOST_SESSION,
    )

    assert answer["status"] == "RUNNING"
    assert answer["parent_run_ref"] == urn(ROOT_KEY)
    (breach,) = breaches(canary)
    assert str(breach.child_run_ref) == answer["run_ref"]
    assert str(breach.ancestor_run_ref) == urn(ROOT_KEY)
    assert (breach.ceiling, breach.descendants) == (0, 1)


def test_run_022_redelivered_host_start_files_the_breach_once(tmp_path: Path) -> None:
    task_scope = seed_row("run", "RUNNING")["scope"]
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent(scope=task_scope)})
    params = {"harness": "claude-code", "agent_id": "a1", "host_session_id": HOST_SESSION}

    call(canary, tmp_path, HOST_SUBAGENT_START_METHOD, **params)
    call(canary, tmp_path, HOST_SUBAGENT_START_METHOD, **params)

    assert len(breaches(canary)) == 1


def test_run_022_host_subagent_within_the_ceiling_files_no_breach(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent()})

    answer = call(
        canary,
        tmp_path,
        HOST_SUBAGENT_START_METHOD,
        harness="claude-code",
        agent_id="a1",
        host_session_id=HOST_SESSION,
    )

    assert answer["parent_run_ref"] == urn(ROOT_KEY)
    assert breaches(canary) == []


# ---- RUN-023: a fresh capsule, cut to the parent's grant ------------------------------


def test_run_023_dispatched_child_is_sealed_its_own_capsule_under_the_parent(
    tmp_path: Path,
) -> None:
    parent_key = "RUN-00000009"
    child = nd.run_row()
    child["parent_run_ref"] = urn(parent_key)
    canary = nd.make_canary(
        tmp_path / "repo", rows={parent_key: nd.run_row(key=parent_key), nd.RUN_KEY: child}
    )
    runtime = tmp_path / "runtime"
    ctx = nd.method_ctx(runtime)
    parent_launcher = nd.LedgerReadingLauncher(canary, runtime)
    nd.dispatch(
        ctx,
        canary,
        parent_launcher,
        params=nd.dispatch_params(
            canary,
            key="dispatch-parent",
            urn=urn(parent_key),
            capsule=nd.capsule_request(tool_grants=["budget_status"]),
        ),
    )
    child_launcher = nd.LedgerReadingLauncher(canary, runtime)

    nd.dispatch(ctx, canary, child_launcher)

    parent_capsule = parent_launcher.calls[0]["capsule"]
    capsule = child_launcher.calls[0]["capsule"]
    assert str(capsule.run_ref) == urn(nd.RUN_KEY)
    assert str(capsule.parent_run_ref) == urn(parent_key)
    assert capsule.contract_digest != parent_capsule.contract_digest
    assert parent_capsule.tool_grants == ("budget_status",)
    assert capsule.tool_grants == ("budget_status",)
    binding = run_binding_of(nd.ledger_records(canary, runtime), capsule.run_ref)
    assert binding is not None
    assert binding.capsule == capsule


def test_run_023_root_dispatch_keeps_its_full_grant_and_names_no_parent(tmp_path: Path) -> None:
    canary = nd.make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = nd.LedgerReadingLauncher(canary, runtime)

    nd.dispatch(nd.method_ctx(runtime), canary, launcher)

    capsule = launcher.calls[0]["capsule"]
    assert capsule.parent_run_ref is None
    assert capsule.tool_grants == ("budget_status", "submit_candidate")


def test_run_023_adopted_child_stream_carries_only_its_own_transcript(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent()})
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"role": "assistant", '
        '"content": [{"type": "text", "text": "child words"}]}}\n',
        encoding="utf-8",
    )

    answer = call(
        canary,
        tmp_path,
        HOST_SUBAGENT_STOP_METHOD,
        harness="claude-code",
        agent_id="a1",
        host_session_id=HOST_SESSION,
        transcript_path=str(transcript),
    )

    records = run_records(canary)
    child_events = run_events_of(records, parse_qualified_urn(answer["run_ref"]))
    parent_events = run_events_of(records, parse_qualified_urn(urn(ROOT_KEY)))
    assert [
        event.payload.summary
        for event in child_events
        if isinstance(event.payload, MessageSummaryPayload)
    ] == ["child words"]
    assert len(child_events) == 1
    # the parent states the delegation, never the child's words (PRX-065)
    assert [event.event_kind for event in parent_events] == [
        RunEventKind.CHILD_RUN_STARTED,
        RunEventKind.CHILD_RUN_TERMINAL,
    ]
    assert not any(isinstance(event.payload, MessageSummaryPayload) for event in parent_events)


# ---- SURF-097: a finished Run stays in the Activity view -----------------------------


def test_surf_097_finished_host_subagent_stays_in_activity(tmp_path: Path) -> None:
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent()})
    answer = call(canary, tmp_path, HOST_SUBAGENT_STOP_METHOD, harness="claude-code", agent_id="a1")
    key = answer["run_ref"].rsplit("/", 1)[-1]
    assert key not in document_rows(read_document(document_path(canary)), Epoch2Collection.RUN)

    served = call(canary, tmp_path, "projection.activity.read")

    assert {row["key"] for row in served["rows"]} == {ROOT_KEY, key}
    grouping = group_runs(build_register_view(RouteProjection.model_validate(served)))
    counts = {count.bucket: count.count.value for count in grouping.top_level()}
    assert counts[ActivityExceptionBucket.TERMINAL_RECENT] == "1"
    assert counts[ActivityExceptionBucket.RUNNING] == "1"


# ---- MEAS-001: an adopted subagent is measured and folded into its root ----------------


def _usage_row(second: int, ident: str, *, output: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": f"2026-09-08T01:00:{second:02d}Z",
            "message": {
                "id": ident,
                "model": "claude-opus-4-1",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": output,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "x"}],
            },
        }
    )


def test_meas_001_adopted_subagent_is_measured_and_folded_into_its_root(tmp_path: Path) -> None:
    agent = "subagent01"
    transcript = (
        tmp_path / "projects" / "-repo" / HOST_SESSION / "subagents" / f"agent-{agent}.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_usage_row(1, "m1", output=40) + "\n", encoding="utf-8")
    canary = canary_with(tmp_path, {ROOT_KEY: host_parent()})
    params = {"harness": "claude-code", "agent_id": agent, "host_session_id": HOST_SESSION}
    started = call(canary, tmp_path, HOST_SUBAGENT_START_METHOD, **params)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_usage_row(2, "m2", output=25) + "\n")

    call(canary, tmp_path, HOST_SUBAGENT_STOP_METHOD, **params)

    key = started["run_ref"].rsplit("/", 1)[-1]
    (root,) = [
        reading for reading in read_tree_runs(canary.root / ".ea") if reading.run.key == ROOT_KEY
    ]
    assert root.fold is not None
    assert root.fold.descendant_keys == (key,)
    assert root.fold.unmeasured_keys == (ROOT_KEY,)
    output = root.fold.counters[CounterName.OUTPUT_TOKENS]
    assert isinstance(output, CounterFold)
    assert (output.inherited_baseline, output.descendant_share) == (Decimal(40), Decimal(25))
    assert output.provider_total == 65
    assert output.residual == 0
