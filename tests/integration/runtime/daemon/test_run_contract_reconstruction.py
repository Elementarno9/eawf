"""A Run contract survives the process that wrote it being killed outright.

The loss here is real. A child interpreter drives the live
``runtime.run.*`` verbs against a provisioned canary, announces on stdout
that it is done, and then blocks; the parent reads that line -- a
barrier, not a sleep -- and sends it ``SIGKILL``. Nothing is flushed,
nothing is closed, no shutdown hook runs. What is left on disk is what
the reconstruction gets.

Two kills are taken. The first lands after the whole walk, so the
canonical record has moved and the contract rebuilds the state the
daemon had. The second lands between the confirmed effect and the
canonical transition, which is the torn state the append order exists to
make survivable: the ledger says the cancellation was observed and the
record has not caught up, and the reducer reports the effect's truth
rather than the record's. Replaying the same idempotent effect afterwards
finishes the job and writes no second fact.

The contract is rebuilt from durable records alone. The last case proves
it by doing the read in a *third* interpreter that has driven nothing,
holds no session, and has never seen the conversation that asked for the
cancellation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_BIND_METHOD,
    RUN_CONTRACT_READ_METHOD,
    RUN_CONTROL_EFFECT_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
TASK_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
REQUEST_REF: Final = "CTL-0000000a"
EFFECT_REF: Final = "EFF-0000000b"
IDEMPOTENCY_KEY: Final = "idem-kill"
ROUTE_REVISION: Final = 3

#: What the child prints once its work is durable. The parent blocks on
#: this line, so the kill lands at a known point rather than after a wait.
READY: Final = "ready"

#: How long the parent waits for a killed child to be reaped.
REAP_SECONDS: Final = 30.0

#: The child that drives the verbs and then parks until it is killed.
#: ``torn`` replaces the canonical transition with the park, so the kill
#: lands between the confirmed effect and the record that follows it.
DRIVER = """
import asyncio
import sys
from pathlib import Path

from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
import eawf.runtime.daemon.methods.run as run_methods

repo_root, urn, spec_digest, capsule_digest, runtime_root, mode = sys.argv[1:7]

ctx = MethodContext(
    started_at="2026-09-18T12:00:00+00:00",
    pid=1,
    protocol_version="1",
    version="test",
    wal_dir=Path(runtime_root) / "wal",
)


def call(method, **params):
    return asyncio.run(methods.dispatch(method, ctx, params))


def park(*_args, **_kwargs):
    sys.stdout.write("ready\\n")
    sys.stdout.flush()
    sys.stdin.read()
    raise SystemExit(0)


if mode == "torn":
    run_methods.run_transaction = park

call(
    "runtime.run.bind",
    repo_root=repo_root,
    urn=urn,
    compiled_spec_digest=spec_digest,
    authority_capsule_digest=capsule_digest,
    route_policy_revision=3,
)
call(
    "runtime.run.control.request",
    repo_root=repo_root,
    urn=urn,
    control_request_ref="CTL-0000000a",
    control="cancel",
    actor="OP-0001",
)
call(
    "runtime.run.control.acknowledge",
    repo_root=repo_root,
    urn=urn,
    control_request_ref="CTL-0000000a",
    actor="OP-0001",
    decision="accepted",
)
call(
    "runtime.run.control.effect",
    repo_root=repo_root,
    urn=urn,
    control_request_ref="CTL-0000000a",
    actor="OP-0001",
    disposition="confirmed",
    effect_ref="EFF-0000000b",
    idempotency_key="idem-kill",
)
park()
"""

#: A reader that has driven nothing: it only rebuilds and prints. It
#: reaches the verb through the server import alone, so a fresh process
#: proves the registration as well as the reconstruction.
READER = """
import asyncio
import json
import sys
from pathlib import Path

import eawf.runtime.daemon.server
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext

repo_root, urn, runtime_root = sys.argv[1:4]

ctx = MethodContext(
    started_at="2026-09-18T12:00:00+00:00",
    pid=1,
    protocol_version="1",
    version="test",
    wal_dir=Path(runtime_root) / "wal",
)
answer = asyncio.run(
    methods.dispatch("runtime.run.contract.read", ctx, {"repo_root": repo_root, "urn": urn})
)
sys.stdout.write(json.dumps(answer))
"""


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context sharing the child's WAL namespace."""
    return method_context(runtime_root)


@pytest.fixture
def capsule() -> AuthorityCapsule:
    """A real sealed capsule, so the recorded digests are real digests."""
    return AuthorityCapsule.seal(
        {
            "run_ref": RUN_URN,
            "scope_ref": TASK_URN,
            "scope_digest": f"sha256:{'a' * 64}",
            "agent_role": "executor",
            "purpose": "implement",
            "authority": {
                "state": "proposal_only",
                "workspace": "scoped_write",
                "git": "read_metadata",
            },
            "tool_grants": ["repo_read", "workspace_apply_patch", "submit_report"],
            "filesystem_policy_ref": "policy://filesystem/task-workspace",
            "budget": {"wall_seconds": 2400, "output_bytes": 1_048_576},
            "criteria_digest": f"sha256:{'b' * 64}",
            "policy_digest": f"sha256:{'c' * 64}",
            "compiled_spec_digest": f"sha256:{'d' * 64}",
            "report_schema_ref": "schema://report/executor/v1",
            "stop_conditions": ["scope_ambiguity", "approval_required"],
        }
    )


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def drive_and_kill(
    canary: CanaryProvision,
    runtime_root: Path,
    capsule: AuthorityCapsule,
    *,
    mode: str,
) -> int:
    """Run the driver in a child, block on its ready line, then kill it.

    Args:
        canary: The provisioned tree the child writes to.
        runtime_root: The runtime directory the child's WAL lives under.
        capsule: The sealed capsule whose digests the child binds.
        mode: ``full`` to drive the whole walk, ``torn`` to park between
            the confirmed effect and the canonical transition.

    Returns:
        The child's exit status, which is negative for a signalled death.
    """
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            DRIVER,
            str(canary.root),
            RUN_URN,
            capsule.compiled_spec_digest,
            capsule.contract_digest,
            str(runtime_root),
            mode,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    announced = child.stdout.readline()
    if announced.strip() != READY:
        child.kill()
        stderr = child.stderr.read() if child.stderr is not None else ""
        raise AssertionError(f"driver never became ready: {stderr}")
    child.kill()
    return child.wait(timeout=REAP_SECONDS)


def stored_run(canary: CanaryProvision) -> dict[str, Any]:
    """Return the canonical Run row, from whichever tier holds it.

    A Run that reaches a terminal state is compacted out of the document
    and into the run ledger, beside the control and event lines the same
    ledger carries. The Run's own line is the one with no payload
    discriminator, which is how the daemon's own reader tells it apart.
    """
    rows = read_document(document_path(canary)).get("run", {})
    if RUN_KEY in rows:
        row: dict[str, Any] = rows[RUN_KEY]
        return row
    ledger = ledger_path(document_path(canary), Epoch2Collection.RUN)
    for item in effective_records(read_ledger_records(ledger)):
        if item.record_key == RUN_KEY and "payload_kind" not in item.payload:
            return item.payload
    raise AssertionError(f"neither tier holds a run record keyed {RUN_KEY!r}")


# ---------------------------------------------------------------------------
# RUN-006: the whole contract rebuilds after the writing process is killed
# ---------------------------------------------------------------------------


def test_the_contract_rebuilds_after_the_writing_process_is_killed(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    status = drive_and_kill(canary, runtime_root, capsule, mode="full")
    assert status != 0

    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert contract["run_ref"] == RUN_URN
    assert contract["status"] == "CANCELLED"
    assert contract["revision"] == 2
    assert contract["compiled_spec_digest"] == capsule.compiled_spec_digest
    assert contract["authority_capsule_digest"] == capsule.contract_digest
    assert contract["route_policy_revision"] == ROUTE_REVISION
    assert contract["control_cursor"] == 3
    assert contract["rows"] == [
        {
            "control_request_ref": REQUEST_REF,
            "control": "cancel",
            "disposition": "confirmed",
        }
    ]


def test_a_kill_between_the_effect_and_the_transition_keeps_the_effect_truth(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    """The ledger says cancelled and the record has not caught up."""
    status = drive_and_kill(canary, runtime_root, capsule, mode="torn")
    assert status != 0
    assert stored_run(canary)["status"] == "RUNNING"

    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    assert contract["status"] == "CANCELLED"
    assert contract["revision"] == 1
    assert contract["control_cursor"] == 3


def test_replaying_the_torn_effect_finishes_the_record_and_appends_nothing(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    drive_and_kill(canary, runtime_root, capsule, mode="torn")

    answer = call(
        RUN_CONTROL_EFFECT_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        control_request_ref=REQUEST_REF,
        actor=ACTOR,
        disposition="confirmed",
        effect_ref=EFFECT_REF,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert answer["control_cursor"] == 3
    assert answer["run_status"] == "CANCELLED"
    assert stored_run(canary)["status"] == "CANCELLED"
    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert contract["revision"] == 2
    assert contract["control_cursor"] == 3


def test_a_third_process_that_drove_nothing_rebuilds_the_same_contract(
    canary: CanaryProvision, runtime_root: Path, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    """No conversation history, no session, no in-process state: just records."""
    drive_and_kill(canary, runtime_root, capsule, mode="full")
    in_process = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)

    reader = subprocess.run(
        [sys.executable, "-c", READER, str(canary.root), RUN_URN, str(runtime_root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=REAP_SECONDS,
    )

    assert json.loads(reader.stdout) == in_process


# ---------------------------------------------------------------------------
# The binding is written once, and a contract without one is not invented
# ---------------------------------------------------------------------------


def test_binding_a_run_twice_answers_with_the_first_binding(
    canary: CanaryProvision, ctx: MethodContext, capsule: AuthorityCapsule
) -> None:
    first = call(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=capsule.compiled_spec_digest,
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=ROUTE_REVISION,
    )
    second = call(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=f"sha256:{'9' * 64}",
        authority_capsule_digest=f"sha256:{'8' * 64}",
        route_policy_revision=ROUTE_REVISION + 1,
    )

    assert second == first


def test_a_run_with_no_binding_has_no_contract_to_report(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="has no contract binding"):
        call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)


def test_a_contract_read_naming_an_unheld_run_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            RUN_CONTRACT_READ_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00009999",
        )


def test_a_contract_read_with_an_unknown_parameter_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            RUN_CONTRACT_READ_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            include_facts=True,
        )


def test_binding_a_malformed_digest_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="compiled_spec_digest"):
        call(
            RUN_BIND_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            compiled_spec_digest="not-a-digest",
            authority_capsule_digest=f"sha256:{'8' * 64}",
            route_policy_revision=ROUTE_REVISION,
        )


def test_binding_a_route_revision_of_zero_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="route_policy_revision"):
        call(
            RUN_BIND_METHOD,
            ctx,
            repo_root=str(canary.root),
            urn=RUN_URN,
            compiled_spec_digest=f"sha256:{'9' * 64}",
            authority_capsule_digest=f"sha256:{'8' * 64}",
            route_policy_revision=0,
        )
