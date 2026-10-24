"""``repo_read`` answers a window the Run already holds by reference, not by text again.

Driven through the registered ``semantic.call`` verb, as the repo_read suite is. A
window whose digest and byte count match an earlier succeeded read of the same Run
comes back naming that call and carrying no text, unless the call sets ``reread``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import (
    TOOL_SCHEMA_VERSION,
    RepoReadInput,
    RepoReadOutput,
    SemanticCall,
    SemanticToolId,
    tool_input_schema,
)
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.native_dispatch import DispatchAttempt, DispatchStage, append_attempt
from eawf.runtime.daemon.semantic_gateway import run_receipts
from eawf.runtime.mcp.semantic_stdio import _tool_descriptor
from eawf.runtime.workspace.lease import workspace_path
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    TASK_URN,
    method_context,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_native_dispatch import make_repo
from tests.integration.runtime.daemon.test_semantic_repo_read import OTHER_HANDLE, output_of
from tests.integration.runtime.daemon.test_semantic_submit_candidate import (
    AT,
    ROUTE_REVISION,
    RUN_KEY,
    RUN_URN,
    SLOT,
    WORKSPACE_HANDLE,
    bind,
    call_verb,
    digest,
    executor_capsule,
    invoke,
    seed_lease,
    task_scope,
)

pytestmark = pytest.mark.integration

#: A second Run of the same canary, reading the same repository.
OTHER_RUN_KEY: Final = "RUN-00000011"
OTHER_RUN_URN: Final = f"{SLOT}/run/{OTHER_RUN_KEY}"

#: The file every read in this module names.
PATH: Final = "src/window.txt"

_ordinal = iter(range(1, 10_000))


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(runtime_root)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding two running, task-scoped Runs and no lease."""
    root = tmp_path / "repo"
    make_repo(root)
    provisioned = provision_canary(repo_root=root, ref=canary_ref("CND"), provisioned_at=AT)
    rows: dict[str, Any] = {}
    for key in (RUN_KEY, OTHER_RUN_KEY):
        row = seed_row("run", "RUNNING")
        row["scope"] = task_scope()
        row["started_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        rows[key] = row
    seed(provisioned, {"run": rows})
    return provisioned


@pytest.fixture
def capsule(ctx: MethodContext, canary: CanaryProvision) -> AuthorityCapsule:
    """The bound capsule of the first Run, granted the read tool."""
    sealed = executor_capsule(tool_grants=("repo_read", "submit_report"))
    bind(ctx, canary, sealed)
    return sealed


@pytest.fixture
def tree(canary: CanaryProvision, runtime_root: Path, capsule: AuthorityCapsule) -> Path:
    """The leased worktree the first Run reads, holding the file every read names."""
    seed_lease(canary, runtime_root)
    target = workspace_path(root_context(canary, runtime_root), handle=WORKSPACE_HANDLE)
    (target / PATH).write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return target


def seal(
    capsule: AuthorityCapsule,
    *,
    run_ref: str = RUN_URN,
    handle: str = WORKSPACE_HANDLE,
    contract_digest: str | None = None,
    **payload: Any,
) -> SemanticCall:
    """Return a sealed ``repo_read`` of *run_ref* under a fresh call id and key."""
    ordinal = next(_ordinal)
    return SemanticCall.seal(
        {
            "call_id": f"call-{ordinal:016x}",
            "run_ref": run_ref,
            "contract_digest": contract_digest or capsule.contract_digest,
            "idempotency_key": f"repeat-{ordinal}",
            "tool_id": "repo_read",
            "tool_schema_version": "1.0.0",
            "payload": {"tool_id": "repo_read", "resource_handle": handle, **payload},
            "requested_at": AT,
        }
    )


def read(
    ctx: MethodContext, canary: CanaryProvision, capsule: AuthorityCapsule, **payload: Any
) -> tuple[str, dict[str, Any]]:
    """Send one ``repo_read`` of :data:`PATH` and return its call id and bounded output."""
    call = seal(capsule, path=PATH, **payload)
    return call.call_id, output_of(invoke(ctx, canary, call, capsule))


# ---- repeat answers by reference -------------------------------------------------


def test_repeat_answers_by_reference(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    first_id, first = read(ctx, canary, capsule)
    _, second = read(ctx, canary, capsule)

    assert first["text"] == "alpha\nbeta\ngamma"
    assert first["same_as_call"] is None
    assert second["same_as_call"] == first_id
    assert second["text"] == ""
    for field in ("content_ref", "content_digest", "byte_count", "truncated"):
        assert second[field] == first[field], field


def test_repeat_answers_by_reference_to_the_call_that_carried_the_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    first_id, _ = read(ctx, canary, capsule)
    read(ctx, canary, capsule)
    _, third = read(ctx, canary, capsule)

    assert (third["same_as_call"], third["text"]) == (first_id, "")


def test_repeat_answers_by_reference_for_a_window_of_a_truncated_file(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    first_id, first = read(ctx, canary, capsule, byte_cap=6)
    _, second = read(ctx, canary, capsule, byte_cap=6)

    assert (first["text"], first["truncated"]) == ("alpha", True)
    assert (second["same_as_call"], second["text"], second["truncated"]) == (first_id, "", True)


def test_repeat_answers_by_reference_after_a_reread_names_the_latest_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    read(ctx, canary, capsule)
    reread_id, _ = read(ctx, canary, capsule, reread=True)
    _, third = read(ctx, canary, capsule)

    assert third["same_as_call"] == reread_id


# ---- reread or a distinct window returns the text --------------------------------


def test_reread_or_distinct_reread_returns_the_full_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    _, first = read(ctx, canary, capsule)
    _, again = read(ctx, canary, capsule, reread=True)

    assert again["same_as_call"] is None
    assert again["text"] == first["text"] == "alpha\nbeta\ngamma"


def test_reread_or_distinct_window_returns_the_full_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    read(ctx, canary, capsule)
    _, later = read(ctx, canary, capsule, start_byte=6)
    _, shorter = read(ctx, canary, capsule, byte_cap=11)

    assert (later["same_as_call"], later["text"]) == (None, "beta\ngamma")
    assert (shorter["same_as_call"], shorter["text"]) == (None, "alpha\nbeta")


def test_reread_or_distinct_changed_file_returns_the_full_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    read(ctx, canary, capsule)
    (tree / PATH).write_text("alpha\nbeta\ndelta\n", encoding="utf-8")
    _, changed = read(ctx, canary, capsule)

    assert (changed["same_as_call"], changed["text"]) == (None, "alpha\nbeta\ndelta")


def test_reread_or_distinct_run_returns_the_full_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    # Neither Run holds a lease, so both read the repository root.
    (canary.root / PATH).write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    other = executor_capsule(run_ref=OTHER_RUN_URN, tool_grants=("repo_read", "submit_report"))
    bind_other(ctx, canary, other)

    _, mine = read(ctx, canary, capsule)
    theirs = output_of(invoke(ctx, canary, seal(other, run_ref=OTHER_RUN_URN, path=PATH), other))

    assert mine["same_as_call"] is None
    assert (theirs["same_as_call"], theirs["text"]) == (None, mine["text"])


def test_reread_or_distinct_repeat_of_a_refused_call_returns_the_full_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    with pytest.raises(DaemonValidationError):
        invoke(ctx, canary, seal(capsule, handle=OTHER_HANDLE, path=PATH), capsule)
    denied = invoke(ctx, canary, seal(capsule, contract_digest=digest("e"), path=PATH), capsule)
    _, served = read(ctx, canary, capsule)

    assert denied["result"]["status"] == "denied"
    assert (served["same_as_call"], served["text"]) == (None, "alpha\nbeta\ngamma")


def test_reread_or_distinct_fields_default_to_off_and_null() -> None:
    reread = RepoReadInput.model_fields["reread"]
    same_as_call = RepoReadOutput.model_fields["same_as_call"]

    assert (reread.default, same_as_call.default) == (False, None)
    with pytest.raises(ValueError, match="bool"):
        RepoReadInput.model_validate(
            {
                "tool_id": "repo_read",
                "resource_handle": WORKSPACE_HANDLE,
                "path": PATH,
                "reread": "yes",
            }
        )


# ---- published schema ------------------------------------------------------------


def test_published_schema_lists_reread_and_names_both_fields() -> None:
    schema = tool_input_schema(SemanticToolId.REPO_READ)
    description = _tool_descriptor(SemanticToolId.REPO_READ)["description"]

    assert "reread" in schema["properties"]
    assert "reread" in description
    assert "same_as_call" in description


def test_published_schema_version_moves_to_a_minor_for_the_repeat_fields() -> None:
    assert TOOL_SCHEMA_VERSION == "1.1.0"


# ---- the receipt ledger keeps a read by digest -----------------------------------


def test_receipt_ledger_keeps_a_read_by_digest_not_by_text(
    canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    tree: Path,
    runtime_root: Path,
) -> None:
    first_id, first = read(ctx, canary, capsule)

    kept = held_reads(canary, runtime_root)

    assert first["text"] == "alpha\nbeta\ngamma"
    assert [(output.text, output.same_as_call) for output in kept] == [("", first_id)]
    assert kept[0].content_digest == first["content_digest"]
    assert kept[0].byte_count == first["byte_count"]


def test_receipt_ledger_replay_of_a_read_names_the_call_itself(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    call = seal(capsule, path=PATH)
    first = output_of(invoke(ctx, canary, call, capsule))
    replayed = output_of(invoke(ctx, canary, call, capsule))

    assert first["text"] == "alpha\nbeta\ngamma"
    assert (replayed["same_as_call"], replayed["text"]) == (call.call_id, "")
    assert replayed["content_digest"] == first["content_digest"]


def test_receipt_ledger_still_answers_a_repeat_by_reference(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    first_id, _ = read(ctx, canary, capsule)
    _, second = read(ctx, canary, capsule)
    _, third = read(ctx, canary, capsule)

    assert (second["same_as_call"], third["same_as_call"]) == (first_id, first_id)


# ---- the repeat matches launch, path and window ----------------------------------


def test_repeat_match_misses_another_file_with_the_same_text(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / "src/twin.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    read(ctx, canary, capsule)
    twin = output_of(invoke(ctx, canary, seal(capsule, path="src/twin.txt"), capsule))

    assert (twin["same_as_call"], twin["text"]) == (None, "alpha\nbeta\ngamma")


def test_repeat_match_misses_a_read_of_an_earlier_launch(
    canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    tree: Path,
    runtime_root: Path,
) -> None:
    launched(canary, runtime_root, continuity_attempts=0)
    read(ctx, canary, capsule)
    launched(canary, runtime_root, continuity_attempts=1)
    _, resumed = read(ctx, canary, capsule)

    assert (resumed["same_as_call"], resumed["text"]) == (None, "alpha\nbeta\ngamma")


def test_repeat_match_holds_within_one_launch(
    canary: CanaryProvision,
    ctx: MethodContext,
    capsule: AuthorityCapsule,
    tree: Path,
    runtime_root: Path,
) -> None:
    launched(canary, runtime_root, continuity_attempts=0)
    first_id, _ = read(ctx, canary, capsule)
    _, second = read(ctx, canary, capsule)

    assert second["same_as_call"] == first_id


def test_repeat_match_reports_the_current_truncation(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule, tree: Path
) -> None:
    (tree / PATH).write_text("alpha\n", encoding="utf-8")
    first_id, first = read(ctx, canary, capsule, byte_cap=6)
    (tree / PATH).write_text("alpha\nbeta\n", encoding="utf-8")
    _, grown = read(ctx, canary, capsule, byte_cap=6)

    assert (first["text"], first["truncated"]) == ("alpha", False)
    assert (grown["same_as_call"], grown["truncated"]) == (first_id, True)


def held_reads(canary: CanaryProvision, runtime_root: Path) -> list[RepoReadOutput]:
    """Return the read outputs the receipt ledger keeps for the first Run."""
    with root_context(canary, runtime_root).session([RUN_URN]) as session:
        receipts = run_receipts(session, RUN_URN)
    return [
        output
        for receipt in receipts
        if isinstance(output := receipt.result.bounded_output, RepoReadOutput)
    ]


def launched(canary: CanaryProvision, runtime_root: Path, *, continuity_attempts: int) -> None:
    """Record that the first Run's attempt launched, or resumed, now."""
    now = datetime.now(UTC)
    attempt = DispatchAttempt(
        attempt_ref=f"ATT-{'c' * 32}",
        run_ref=RUN_URN,
        task_ref=TASK_URN,
        dispatch_key="dispatch-repeat",
        stage=DispatchStage.COMPILED,
        compiled_spec_digest=digest("d"),
        authority_capsule_digest=digest("a"),
        route_policy_revision=ROUTE_REVISION,
        provider_kind="claude-code",
        continuity_attempts=continuity_attempts,
        started_at=now,
        recorded_at=now,
    )
    with root_context(canary, runtime_root).session([RUN_URN]) as session:
        append_attempt(session, attempt)


def bind_other(ctx: MethodContext, canary: CanaryProvision, other: AuthorityCapsule) -> None:
    """Record the contract the second Run's calls must echo."""
    call_verb(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=OTHER_RUN_URN,
        compiled_spec_digest=digest("d"),
        authority_capsule_digest=other.contract_digest,
        route_policy_revision=ROUTE_REVISION,
    )
