"""RUN-013 and SURF-102: a host compaction cannot change what the Run is bound to.

The pre-compaction hook snapshots the Run's contract anchors onto its stream through
the real daemon verb; the post-compaction session start reads them back from the store,
names every anchor that moved, and hands the host's model a restatement in place of
whatever the summary kept. A session with no live Run gets the daemon's typed refusal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.transcript import block_text
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.events import ContextBoundaryPayload, RunEventRecord
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_context import HOST_CONTEXT_BOUNDARY_METHOD
from eawf.runtime.daemon.methods.host_tool import HOST_TOOL_OBSERVE_METHOD
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_lane import HOST_CONTEXT_HOOK, host_context_restatement
from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    root_context,
    seed,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    RUN_URN,
    SESSION,
    _running_run,
)
from tests.unit.kernel.runtime.test_authority_capsule import review_fields

pytestmark = pytest.mark.integration

_AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

_CAPSULE: Final = AuthorityCapsule.seal(review_fields(run_ref=RUN_URN))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run on the host session, bound to a sealed capsule."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: _running_run(SESSION)}})
    binding = RunBinding(
        run_ref=parse_qualified_urn(RUN_URN),
        compiled_spec_digest=_CAPSULE.compiled_spec_digest,
        authority_capsule_digest=_CAPSULE.contract_digest,
        route_policy_revision=1,
        bound_at=_AT,
        capsule=_CAPSULE,
    )
    append_ledger_record(
        ledger_path(document_path(provisioned), Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"BND-{RUN_KEY}",
            status="bound",
            recorded_at=_AT,
            payload=binding.model_dump(mode="json"),
        ),
    )
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> Any:
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def boundary(ctx: MethodContext, canary: CanaryProvision, which: str, **extra: Any) -> Any:
    return call(
        HOST_CONTEXT_BOUNDARY_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        boundary=which,
        **extra,
    )


def stream(canary: CanaryProvision, tmp_path: Path) -> tuple[RunEventRecord, ...]:
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return run_events_of(records, parse_qualified_urn(RUN_URN))


def test_run_013_compaction_keeps_authority_criteria_scope_and_receipts(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    before = boundary(ctx, canary, "compacting", trigger="auto")
    after = boundary(ctx, canary, "resumed")

    assert before["anchors"] == after["anchors"]
    anchors = after["anchors"]
    assert anchors["authority_capsule_digest"] == _CAPSULE.contract_digest
    assert anchors["compiled_spec_digest"] == _CAPSULE.compiled_spec_digest
    assert anchors["criteria_digest"] == _CAPSULE.criteria_digest
    assert anchors["scope_digest"] == _CAPSULE.scope_digest
    assert after["drift"] == []
    assert _CAPSULE.contract_digest in after["restatement"]
    assert "changed none of them" in after["restatement"]
    lines = stream(canary, tmp_path)
    payloads = [line.payload for line in lines]
    assert all(isinstance(p, ContextBoundaryPayload) for p in payloads)
    assert [p.boundary for p in payloads] == ["compacting", "resumed"]  # type: ignore[union-attr]
    assert payloads[0].trigger == "auto"  # type: ignore[union-attr]
    assert block_text(lines[1]).value == "context resumed · anchors unchanged"


def test_run_013_a_receipt_changed_across_the_compaction_is_a_contract_mismatch(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    boundary(ctx, canary, "compacting")
    call(
        HOST_TOOL_OBSERVE_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        tool_name="Read",
        host_call_key="toolu_01read",
        phase="result",
        output="file text",
    )

    after = boundary(ctx, canary, "resumed")

    assert after["drift"] == ["receipts_digest"]
    assert after["anchors"]["receipt_count"] == 1
    assert "contract_mismatch: receipts_digest" in after["restatement"]
    final = stream(canary, tmp_path)[-1]
    assert block_text(final).value == "context resumed · contract mismatch: receipts_digest"


def test_surf_102_a_compaction_on_a_session_with_no_run_is_refused_typed(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            HOST_CONTEXT_BOUNDARY_METHOD,
            ctx,
            canary,
            harness="claude-code",
            host_session_id="another-session",
            boundary="compacting",
        )


class _InProcessClient:
    """A daemon client that dispatches each call to the daemon's verbs in process."""

    def __init__(self, ctx: MethodContext, canary: CanaryProvision) -> None:
        self._ctx = ctx
        self._canary = canary

    def call(self, method: str, params: dict[str, Any]) -> Any:
        return call(method, self._ctx, self._canary, **params)


def _fire(
    event_type: HookEventType, payload: dict[str, Any], ctx: MethodContext, canary: CanaryProvision
) -> list[Any]:
    @contextmanager
    def factory() -> Iterator[_InProcessClient]:
        yield _InProcessClient(ctx, canary)

    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=factory, repo_root=canary.root)
    event = HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={"claude_code": payload},
    )
    return [r for r in runner.run_event(event) if r.name == HOST_CONTEXT_HOOK]


def test_surf_102_the_precompact_and_session_start_hooks_write_through_the_daemon(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    compacting = _fire(
        HookEventType.PRE_COMPACT,
        {"session_id": SESSION, "hook_event_name": "PreCompact", "trigger": "manual"},
        ctx,
        canary,
    )
    resumed = _fire(
        HookEventType.SESSION_START,
        {"session_id": SESSION, "hook_event_name": "SessionStart", "source": "compact"},
        ctx,
        canary,
    )

    assert compacting[0].output.startswith(f"{HOST_CONTEXT_HOOK} ok run=")
    restatement = host_context_restatement(resumed)
    assert restatement is not None and _CAPSULE.contract_digest in restatement
    assert [line.payload.boundary for line in stream(canary, tmp_path)] == [  # type: ignore[union-attr]
        "compacting",
        "resumed",
    ]
