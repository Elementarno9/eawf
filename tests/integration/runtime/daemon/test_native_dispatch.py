"""``runtime.run.dispatch`` at the daemon door: four steps, one identity.

The suite drives the dispatcher against a provisioned canary with a real
git repository under it, so the lease it takes is a real worktree and the
records it leaves are the ones a caller on the socket would produce.

Order is asserted by the launcher itself. The injected launcher reads the
canary's own run ledger at the moment it is called and reports the attempt
stages it found there, so "compile and lease were durable before the
spawn" is a fact the spawning side observed rather than an inference drawn
from the answer after everything finished. The same device drives the
restart: a launcher that reads the ledger and then dies never records a
spawn, and the dispatch that follows it runs on a daemon context built
from scratch, so nothing in process memory carries over and the ledger is
all there is to resume from.

Nothing here sleeps and nothing polls. Every stamp comes from the ``now``
the driver is handed, and the lost daemon is a raised exception at an
exact point rather than a race.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

import eawf.runtime.daemon.methods.run  # noqa: F401  -- registers runtime.run.*
from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.runtime.handshake import HandshakeDisposition
from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_dispatch import (
    RUN_DISPATCH_METHOD,
    DispatchAttempt,
    DispatchParams,
    DispatchRefusal,
    DispatchStage,
    compile_launchers,
    run_binding_of,
    run_ledger,
    standing_attempt,
)
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    compose_worker_hello,
)
from eawf.runtime.runtimes.claude.adapter import ClaudeNativeLauncher
from eawf.runtime.runtimes.codex.adapter import CodexNativeLauncher
from eawf.runtime.workspace.lease import root_leases
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row

pytestmark = pytest.mark.integration


ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
SUCCESSOR_KEY: Final = "RUN-00000011"
RUN_URN: Final = fx.RUN_URN
TASK_URN: Final = fx.TASK_URN
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

#: What the fixture profile compiles to, which is the provider a dispatch
#: routes to unless a test says otherwise.
PROVIDER_KIND: Final = "codex"

#: The write set the seeded Run declares, which is also what its lease is
#: allowed to write under.
WRITE_SET: Final[tuple[str, ...]] = ("src",)


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def run_urn(key: str = RUN_KEY) -> QualifiedUrn:
    """Return the parsed Run URN of *key*."""
    return parse_qualified_urn(str(RUN_URN).replace(RUN_KEY, key))


# ---- the canary the dispatch lands in ---------------------------------------


def make_repo(root: Path) -> None:
    """Initialise a one-commit git repository at *root*."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=root, check=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "module.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)


def run_row(status: str = "QUEUED", *, key: str = RUN_KEY) -> dict[str, Any]:
    """Return one seeded Run payload keyed *key*, bounded to the write set."""
    row = seed_row("run", status)
    row["key"] = key
    row["urn"] = str(run_urn(key))
    row["scope"]["write_set"] = list(WRITE_SET)
    return row


def make_canary(root: Path, *, rows: dict[str, dict[str, Any]] | None = None) -> CanaryProvision:
    """Provision a canary over a fresh repository holding the seeded Runs."""
    make_repo(root)
    provisioned = provision_canary(repo_root=root, ref=canary_ref("DSP"), provisioned_at=AT)
    seed(provisioned, {"run": rows if rows is not None else {RUN_KEY: run_row()}})
    return provisioned


def method_ctx(runtime_root: Path) -> MethodContext:
    """Return a daemon context with a WAL directory of its own.

    A fresh context is what a restarted daemon has: no native root cache,
    no idempotency cache and no attached session, so everything it knows
    about a Run it has to read off the tree.
    """
    return MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )


def root_ctx(canary: CanaryProvision, runtime_root: Path) -> Epoch2RootContext:
    """Return the native context of a provisioned canary."""
    return method_ctx(runtime_root).native_root_context(canary.root / ".ea")


def ledger_records(canary: CanaryProvision, runtime_root: Path) -> tuple[LedgerRecord, ...]:
    """Return every line the canary's run ledger holds."""
    context = root_ctx(canary, runtime_root)
    with context.session([RUN_URN]) as session:
        return read_ledger_records(run_ledger(session))


def compact_run(canary: CanaryProvision, runtime_root: Path, row: dict[str, Any]) -> None:
    """File one Run into the run ledger the way terminal compaction does."""
    context = root_ctx(canary, runtime_root)
    with context.session([row["urn"]]) as session:
        append_ledger_record(
            run_ledger(session),
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=str(row["key"]),
                status=str(row["status"]),
                recorded_at=AT,
                payload=row,
            ),
        )


def attempts_of(canary: CanaryProvision, runtime_root: Path) -> tuple[DispatchAttempt, ...]:
    """Return every dispatch-attempt line on the canary, in ledger order."""
    return tuple(
        DispatchAttempt.model_validate(item.payload)
        for item in ledger_records(canary, runtime_root)
        if item.payload.get("payload_kind") == "dispatch_attempt"
    )


# ---- the request ------------------------------------------------------------


def capsule_request(**overrides: Any) -> dict[str, Any]:
    """Return the capsule fields the compiled spec cannot supply."""
    fields: dict[str, Any] = {
        "criteria_digest": digest("b"),
        "report_schema_ref": "schema://executor-report/v1",
        "tool_grants": ["budget_status", "submit_candidate"],
        "stop_conditions": ["budget_exhausted"],
    }
    fields.update(overrides)
    return fields


def dispatch_params(
    canary: CanaryProvision,
    *,
    key: str = "dispatch-01",
    urn: str | None = None,
    bindings: list[Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return the wire params of one dispatch of the seeded Run."""
    addressed = urn if urn is not None else str(RUN_URN)
    request = fx.task_request(
        run_ref=addressed,
        run_scope={
            "scope_kind": "task",
            "purpose": "implement",
            "task_ref": TASK_URN,
            "write_set": list(WRITE_SET),
        },
    )
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": addressed,
        "actor": ACTOR,
        "idempotency_key": key,
        "compile_request": request.model_dump(mode="json"),
        "provider_documents": fx.documents(),
        "provider_registry": fx.registry().model_dump(mode="json"),
        "bindings": [
            binding.model_dump(mode="json")
            for binding in (bindings if bindings is not None else [fx.binding()])
        ],
        "capsule": capsule_request(),
        "base": "main",
        "lease_ttl_seconds": 600,
        "prompt": "implement the task",
    }
    params.update(overrides)
    return params


def call_verb(method: str, ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one daemon verb the way the socket listener does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


# ---- launchers that report what the ledger said when they ran ---------------


class DaemonDiedError(Exception):
    """The daemon process ended between two steps of one dispatch."""


class LedgerReadingLauncher:
    """A launcher that reads the Run's ledger before it answers.

    The point is the ordering claim: what the launcher saw when it was
    called is what was durable at the moment of the spawn, so a dispatch
    that leased after spawning, or that never wrote the attempt, fails
    here rather than passing on a happy-looking answer.

    Attributes:
        provider_kind: Which provider this launcher stands in for.
        calls: One record per launch, carrying the request it was handed
            and the attempt stages the ledger held at that instant.
    """

    def __init__(self, canary: CanaryProvision, runtime_root: Path, *, die: bool = False) -> None:
        """Bind the launcher to the canary whose ledger it reads.

        Args:
            canary: The tree the dispatch is landing in.
            runtime_root: Where this reader's own WAL namespace lives.
            die: Whether to raise instead of answering, which is how a
                daemon lost between the lease and the spawn is driven.
        """
        self.provider_kind = PROVIDER_KIND
        self._canary = canary
        self._runtime_root = runtime_root
        self._die = die
        self.calls: list[dict[str, Any]] = []

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Record what the ledger held, then answer or die.

        Raises:
            DaemonDiedError: The launcher was built to model a lost daemon.
        """
        records = ledger_records(self._canary, self._runtime_root)
        seen = tuple(
            DispatchAttempt.model_validate(item.payload)
            for item in records
            if item.payload.get("payload_kind") == "dispatch_attempt"
        )
        binding = run_binding_of(records, request.spec.run_ref)
        self.calls.append(
            {
                "stages": [attempt.stage for attempt in seen],
                "attempt_refs": sorted({attempt.attempt_ref for attempt in seen}),
                "workspace": request.workspace,
                "workspace_handle": request.workspace_handle,
                "spec": request.spec,
                "capsule": request.capsule,
                "binding_digest": None if binding is None else binding.compiled_spec_digest,
                "hello_sequence": request.hello_sequence,
            }
        )
        if self._die:
            raise DaemonDiedError("the daemon ended before the spawn was recorded")
        session_ref = f"codex-session-{request.hello_sequence}"
        return NativeLaunchOutcome(
            provider_session_ref=session_ref,
            subprocess_pid=4242,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session_ref,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
        )


class DriftingLauncher(LedgerReadingLauncher):
    """A launcher whose worker announces a contract nobody bound."""

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Answer with an announcement carrying a foreign capsule digest."""
        outcome = await super().launch(request)
        drifted = outcome.hello.model_copy(update={"authority_capsule_digest": digest("f")})
        return outcome.model_copy(update={"hello": drifted})


def dispatch(
    ctx: MethodContext,
    canary: CanaryProvision,
    launcher: Any,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive one dispatch through the driver with *launcher* installed."""
    supplied = params if params is not None else dispatch_params(canary)
    args = DispatchParams.model_validate(
        {key: value for key, value in supplied.items() if key != "repo_root"}
    )
    context = ctx.native_root_context(canary.root / ".ea")
    answer = asyncio.run(
        native_dispatch.dispatch_run(
            context, args, now=AT, launchers=dict(compile_launchers((launcher,)))
        )
    )
    return answer.model_dump(mode="json")


# ---- the registered verb ----------------------------------------------------


def test_run_dispatch_is_registered_behind_the_native_fence() -> None:
    """The verb a caller on the socket reaches is the dispatcher's."""
    assert RUN_DISPATCH_METHOD in methods.registered_methods()


def test_run_dispatch_refuses_an_epoch_one_tree(tmp_path: Path) -> None:
    """A tree with no epoch-2 authority never reaches the compiler."""
    repo = tmp_path / "plain"
    make_repo(repo)
    (repo / ".ea").mkdir(exist_ok=True)

    with pytest.raises(DaemonValidationError, match="native_authority_required"):
        call_verb(
            RUN_DISPATCH_METHOD,
            method_ctx(tmp_path / "runtime"),
            {"repo_root": str(repo), "urn": RUN_URN, "actor": ACTOR, "idempotency_key": "k"},
        )


def test_run_dispatch_refuses_params_it_does_not_declare(tmp_path: Path) -> None:
    """An unknown field is refused by the strict parameter model."""
    canary = make_canary(tmp_path / "repo")
    params = dispatch_params(canary)
    params["unexpected"] = True

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(RUN_DISPATCH_METHOD, method_ctx(tmp_path / "runtime"), params)


def test_run_dispatch_refuses_a_compile_addressed_at_another_run(tmp_path: Path) -> None:
    """The verb and the compile request name one Run or neither runs."""
    canary = make_canary(tmp_path / "repo")
    params = dispatch_params(canary)
    params["compile_request"]["run_ref"] = str(run_urn(SUCCESSOR_KEY))

    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(RUN_DISPATCH_METHOD, method_ctx(tmp_path / "runtime"), params)


def test_run_dispatch_refuses_an_empty_idempotency_key(tmp_path: Path) -> None:
    """A request with no name of its own cannot be resumed after a loss."""
    canary = make_canary(tmp_path / "repo")

    with pytest.raises(DaemonValidationError, match="idempotency_key"):
        call_verb(
            RUN_DISPATCH_METHOD,
            method_ctx(tmp_path / "runtime"),
            dispatch_params(canary, key=""),
        )


# ---- the four steps, in order -----------------------------------------------


def test_dispatch_runs_compile_then_lease_then_spawn_then_hello(tmp_path: Path) -> None:
    """The launcher sees a compiled, leased attempt and a bound contract."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)

    answer = dispatch(method_ctx(runtime), canary, launcher)

    assert len(launcher.calls) == 1
    seen = launcher.calls[0]
    assert seen["stages"] == [DispatchStage.COMPILED, DispatchStage.LEASED]
    assert seen["binding_digest"] == answer["compiled_spec_digest"]
    assert answer["stage"] == DispatchStage.ANNOUNCED.value
    assert answer["handshake_disposition"] == HandshakeDisposition.ACCEPTED.value
    assert answer["refusal_code"] is None
    assert answer["resumed"] is False


def test_dispatch_hands_the_launcher_the_compiled_spec_and_sealed_capsule(
    tmp_path: Path,
) -> None:
    """The launcher receives typed records, never a layered document."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)

    answer = dispatch(method_ctx(runtime), canary, launcher)

    seen = launcher.calls[0]
    assert isinstance(seen["spec"], CompiledRunSpec)
    assert isinstance(seen["capsule"], AuthorityCapsule)
    assert seen["spec"].contract_digest == answer["compiled_spec_digest"]
    assert seen["capsule"].compiled_spec_digest == seen["spec"].contract_digest
    assert seen["capsule"].contract_digest == answer["authority_capsule_digest"]


def test_dispatch_derives_the_capsule_from_the_compiled_spec(tmp_path: Path) -> None:
    """Enforcement fields come from the compile, not from the caller."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)

    dispatch(method_ctx(runtime), canary, launcher)

    spec = launcher.calls[0]["spec"]
    capsule = launcher.calls[0]["capsule"]
    assert capsule.authority == spec.authority
    assert capsule.policy_digest == spec.policy_digest
    assert capsule.scope_digest == spec.scope_digest
    assert capsule.budget.wall_seconds == spec.limits.wall_seconds
    assert capsule.budget.output_bytes == spec.limits.output_bytes
    assert str(capsule.scope_ref) == TASK_URN


def test_dispatch_starts_the_child_inside_the_leased_workspace(tmp_path: Path) -> None:
    """The daemon resolves the handle; the worker is only told the handle."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    launcher = LedgerReadingLauncher(canary, runtime)

    answer = dispatch(method_ctx(runtime), canary, launcher)

    seen = launcher.calls[0]
    assert seen["workspace_handle"] == answer["lease"]["workspace_handle"]
    assert seen["workspace"].name == seen["workspace_handle"]
    assert seen["workspace"].is_dir()
    assert "path" not in answer["lease"]


def test_dispatch_leaves_one_active_lease_for_the_run(tmp_path: Path) -> None:
    """Exactly one workspace is borrowed, and it is the answered one."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    answer = dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    leases = root_leases(root_ctx(canary, runtime))
    assert [lease.lease_id for lease in leases] == [answer["lease"]["lease_id"]]
    assert leases[0].status is LeaseStatus.ACTIVE


def test_dispatch_records_one_attempt_line_per_step(tmp_path: Path) -> None:
    """Each step it completed is on the ledger under one identity."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    attempts = attempts_of(canary, runtime)
    assert [attempt.stage for attempt in attempts] == [
        DispatchStage.COMPILED,
        DispatchStage.LEASED,
        DispatchStage.SPAWNED,
        DispatchStage.ANNOUNCED,
    ]
    assert len({attempt.attempt_ref for attempt in attempts}) == 1


def test_dispatch_refuses_a_mismatched_announcement_without_moving_on(
    tmp_path: Path,
) -> None:
    """A worker announcing another capsule earns no announced stage."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    answer = dispatch(method_ctx(runtime), canary, DriftingLauncher(canary, runtime))

    assert answer["handshake_disposition"] == HandshakeDisposition.MISMATCHED.value
    assert answer["refusal_code"] == "runtime_handshake_mismatch"
    assert answer["mismatched_fields"] == ["authority_capsule_digest"]
    assert answer["stage"] == DispatchStage.SPAWNED.value


def test_dispatch_refuses_a_provider_no_launcher_serves(tmp_path: Path) -> None:
    """A compiled provider with no launcher reaches no process at all."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    stranger = LedgerReadingLauncher(canary, runtime)
    stranger.provider_kind = "opencode"

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.LAUNCHER_ABSENT.value):
        dispatch(method_ctx(runtime), canary, stranger)
    assert stranger.calls == []


# ---- one attempt identity across a restart ----------------------------------


def test_a_daemon_lost_between_lease_and_spawn_keeps_one_attempt(tmp_path: Path) -> None:
    """A restart resumes the recorded attempt instead of minting another."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    before = attempts_of(canary, runtime)
    assert [attempt.stage for attempt in before] == [
        DispatchStage.COMPILED,
        DispatchStage.LEASED,
    ]

    answer = dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    after = attempts_of(canary, runtime)
    assert len({attempt.attempt_ref for attempt in after}) == 1
    assert after[0].attempt_ref == before[0].attempt_ref
    assert answer["resumed"] is True
    assert answer["attempt"]["attempt_ref"] == before[0].attempt_ref
    assert answer["stage"] == DispatchStage.ANNOUNCED.value


def test_a_restart_reuses_the_lease_the_lost_attempt_held(tmp_path: Path) -> None:
    """One attempt borrows one workspace, whatever killed the daemon."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))
    answer = dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    leases = root_leases(root_ctx(canary, runtime))
    assert len(leases) == 1
    assert leases[0].lease_id == answer["lease"]["lease_id"]


def test_a_restart_binds_the_contract_exactly_once(tmp_path: Path) -> None:
    """The contract the handshake is judged against is written once."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    bindings = [
        item
        for item in ledger_records(canary, runtime)
        if item.payload.get("payload_kind") == "run_binding"
    ]
    assert len(bindings) == 1


def test_a_completed_dispatch_starts_no_second_worker(tmp_path: Path) -> None:
    """An attempt already past acceptance is never launched again."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime))

    second = LedgerReadingLauncher(canary, runtime)
    answer = dispatch(method_ctx(runtime), canary, second)

    assert second.calls == []
    assert answer["resumed"] is True
    assert answer["stage"] == DispatchStage.ANNOUNCED.value


def test_a_second_dispatcher_cannot_take_over_an_attempt_in_flight(
    tmp_path: Path,
) -> None:
    """Only the request that opened an attempt resumes it."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    with pytest.raises(DaemonDiedError):
        dispatch(method_ctx(runtime), canary, LedgerReadingLauncher(canary, runtime, die=True))

    intruder = LedgerReadingLauncher(canary, runtime)
    with pytest.raises(DaemonValidationError, match=DispatchRefusal.ATTEMPT_IN_FLIGHT.value):
        dispatch(
            method_ctx(runtime),
            canary,
            intruder,
            params=dispatch_params(canary, key="dispatch-02"),
        )
    assert intruder.calls == []


def test_dispatch_reads_a_compacted_run_from_its_ledger(tmp_path: Path) -> None:
    """A terminal Run is out of the document and still has nothing to run."""
    canary = make_canary(tmp_path / "repo", rows={})
    runtime = tmp_path / "runtime"
    compact_run(canary, runtime, run_row("COMPLETED"))
    launcher = LedgerReadingLauncher(canary, runtime)

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.RUN_NOT_DISPATCHABLE.value):
        dispatch(method_ctx(runtime), canary, launcher)
    assert launcher.calls == []
    assert root_leases(root_ctx(canary, runtime)) == ()


def test_dispatch_refuses_a_run_no_tier_holds(tmp_path: Path) -> None:
    """A Run nothing recorded is refused before anything is written."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        dispatch(
            method_ctx(runtime),
            canary,
            LedgerReadingLauncher(canary, runtime),
            params=dispatch_params(canary, urn=str(run_urn(SUCCESSOR_KEY))),
        )
    assert root_leases(root_ctx(canary, runtime)) == ()


def test_standing_attempt_answers_none_before_any_dispatch(tmp_path: Path) -> None:
    """A Run nobody dispatched carries no attempt to resume."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    assert standing_attempt(ledger_records(canary, runtime), run_urn()) is None


# ---- the launcher table itself ----------------------------------------------


def test_compile_launchers_refuses_an_empty_table() -> None:
    """A dispatch table with no launcher starts nothing at all."""
    with pytest.raises(ValueError, match="at least one launcher"):
        compile_launchers(())


def test_compile_launchers_refuses_two_claims_on_one_provider() -> None:
    """Which process a dispatch starts never depends on iteration order."""
    with pytest.raises(ValueError, match="two launchers claim"):
        compile_launchers((CodexNativeLauncher(), CodexNativeLauncher()))


def test_the_shipped_table_serves_the_two_installed_providers() -> None:
    """The compiled default routes the providers the repo actually drives."""
    assert sorted(native_dispatch.NATIVE_LAUNCHERS) == ["claude", "codex"]
    assert isinstance(native_dispatch.NATIVE_LAUNCHERS["claude"], ClaudeNativeLauncher)
    assert isinstance(native_dispatch.NATIVE_LAUNCHERS["codex"], CodexNativeLauncher)


# ---- the attempt record's own rules ------------------------------------------


def attempt_fields(**overrides: Any) -> dict[str, Any]:
    """Return a valid attempt payload with *overrides* applied."""
    fields: dict[str, Any] = {
        "attempt_ref": f"ATT-{'a' * 32}",
        "run_ref": RUN_URN,
        "task_ref": TASK_URN,
        "dispatch_key": "dispatch-01",
        "stage": DispatchStage.COMPILED.value,
        "compiled_spec_digest": digest("1"),
        "authority_capsule_digest": digest("2"),
        "route_policy_revision": 3,
        "provider_kind": PROVIDER_KIND,
        "started_at": AT.isoformat(),
        "recorded_at": AT.isoformat(),
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    "stage",
    [DispatchStage.LEASED.value, DispatchStage.SPAWNED.value, DispatchStage.ANNOUNCED.value],
)
def test_attempt_stage_requires_what_reaching_it_produced(stage: str) -> None:
    """A stage that names none of its facts cannot be validated at all."""
    with pytest.raises(ValueError, match="lease_id, workspace_handle"):
        DispatchAttempt.model_validate(attempt_fields(stage=stage))


def test_attempt_refuses_a_retry_lineage_pointing_at_itself() -> None:
    """A Run is never recorded as its own predecessor."""
    with pytest.raises(ValueError, match="another Run"):
        DispatchAttempt.model_validate(attempt_fields(retry_of_run_ref=RUN_URN))


def test_attempt_refuses_an_unknown_field() -> None:
    """The record is strict, so a drifted writer is refused at the line."""
    with pytest.raises(ValueError, match="Extra inputs"):
        DispatchAttempt.model_validate(attempt_fields(surprise=1))


def test_attempt_refuses_an_identity_outside_its_grammar() -> None:
    """An attempt identity is a minted one or it is not an identity."""
    with pytest.raises(ValueError, match="attempt_ref"):
        DispatchAttempt.model_validate(attempt_fields(attempt_ref="ATT-nothex"))


def test_attempt_refuses_a_dispatch_key_longer_than_the_bound() -> None:
    """The key is bounded, so a caller cannot file prose in the ledger."""
    with pytest.raises(ValueError, match="dispatch_key"):
        DispatchAttempt.model_validate(attempt_fields(dispatch_key="k" * 129))
