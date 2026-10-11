"""A canary and a Task Run each bind a content-only report through the real MCP server.

Real workers could not file a valid ``submit_report``: nothing told them the
contract digest the daemon insisted on, and nothing let them mint the body
reference and digest the tool asked for. The tool now takes the report's
content alone and eawf derives the rest. These cases drive both production
flows that start a worker -- ``runtime.certification.certify`` walking its
containment canary, and ``runtime.task.dispatch`` starting a Task Run after it
-- with a fake provider in place of the vendor CLI. The fake does what a
provider does with the lane configuration the launch rendered: it starts the
registered ``eawf mcp serve`` command as a child process and sends it the
report's content, and the child reaches an in-process daemon listening on a
socket under a private runtime directory. No model and no daemon outside the
test are reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

import eawf.runtime.worktree.git as git
from eawf import __version__
from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.candidate import AcceptedReport
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.events import FileChangePayload
from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.kernel.store.ledger import read_ledger_records
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.candidate.seal import accepted_report_of
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.runtime_certification import RUNTIME_CERTIFY_METHOD
from eawf.runtime.daemon.methods.task_dispatch import TASK_DISPATCH_METHOD
from eawf.runtime.daemon.native_dispatch import compile_launchers, run_ledger
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.daemon.run_supervisor import supervisor_of
from eawf.runtime.daemon.server import handle_connection
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME, native_run_server_config
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.workspace.lease import root_leases
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed
from tests.integration.runtime.daemon.test_certifier_canary import (
    CLAUDE_VERSION,
    RAN_MODEL,
    _private_harness,  # noqa: F401  (autouse: the canary reads a recorded harness)
    make_tree,
    outcomes,
    write_jail,
    write_launch,
)
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.integration.runtime.daemon.test_native_dispatch import ACTOR
from tests.integration.runtime.daemon.test_task_dispatch import BUDGET, TASK_KEY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the daemon listens on a Unix socket"),
]

#: How long one MCP server child may take to answer, generous for a cold import.
CHILD_TIMEOUT_SECONDS: Final = 120

#: The request id the first patch call is sent under; the report keeps id 2.
PATCH_FIRST_ID: Final = 10


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A runtime directory short enough for an AF_UNIX socket path, pinned for the binding."""
    path = Path(tempfile.mkdtemp(prefix="eawf-rt-"))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def daemon_ctx(runtime_root: Path) -> MethodContext:
    """Return a daemon context answering as this release, as the serving CLI checks."""
    return MethodContext(
        started_at=datetime.now(UTC).isoformat(),
        pid=os.getpid(),
        protocol_version="1",
        version=__version__,
        wal_dir=runtime_root / "wal",
    )


def serve_command(config_files: Mapping[str, str]) -> list[str]:
    """Return the server command the rendered claude configuration registers."""
    for body in config_files.values():
        document = json.loads(body)
        entry = document.get("mcpServers", {}).get("eawf-semantic")
        if entry is not None:
            return [entry["command"], *entry["args"]]
    raise AssertionError("the rendered configuration registers no eawf-semantic server")


class McpServerWorker:
    """A fake provider that files its report through the Run's real MCP server.

    It renders the lane configuration the way the claude launcher does, starts the
    server command that configuration registers, and sends it ``initialize`` and one
    ``submit_report`` call carrying only the report's content.

    Attributes:
        provider_kind: The provider the fake stands in for.
        capsule: The capsule the last launch carried.
        answers: The last server session's answers, keyed by request id.
    """

    def __init__(
        self,
        home: Path,
        *,
        report: dict[str, Any],
        answer: str = "done",
        patches: tuple[dict[str, Any], ...] = (),
    ) -> None:
        """Bind the fake to its server child's home, the patches it sends and its report."""
        self.provider_kind = "claude"
        self.capsule: AuthorityCapsule | None = None
        self.answers: dict[int, dict[str, Any]] = {}
        self.stderr = ""
        self._home = home
        self._report = report
        self._answer = answer
        self._patches = patches
        self._launches = 0

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Record the jailed wrapper, render the server configuration and run the turn."""
        self._launches += 1
        self.capsule = request.capsule
        write_launch(
            request.workspace / MCP_ARTIFACT_DIRNAME, write_jail(self._home.parent / "jail")
        )
        config = native_run_server_config(request, runtime_id="claude-code")
        session_ref = f"claude-mcp-{self._launches}"
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
            turn=asyncio.get_running_loop().create_task(
                self._turn(serve_command(config.files), request.workspace)
            ),
            runtime_tuple=RunRuntimeTuple(
                harness="claude-code", harness_version=CLAUDE_VERSION, model=RAN_MODEL
            ),
        )

    async def _turn(self, argv: list[str], workspace: Path) -> TurnOutcome:
        """Send the report through the server child and end the turn with the answer."""
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            *(
                {
                    "jsonrpc": "2.0",
                    "id": PATCH_FIRST_ID + index,
                    "method": "tools/call",
                    "params": {"name": "workspace_apply_patch", "arguments": patch},
                }
                for index, patch in enumerate(self._patches)
            ),
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "submit_report", "arguments": self._report},
            },
        ]
        child = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self._home),
                "EAWF_REGISTRY_PATH": str(self._home / "registry.json"),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        frames = b"".join(json.dumps(item).encode() + b"\n" for item in requests)
        stdout, stderr = await asyncio.wait_for(
            child.communicate(frames), timeout=CHILD_TIMEOUT_SECONDS
        )
        self.stderr = stderr.decode()
        self.answers = {item["id"]: item for item in map(json.loads, stdout.decode().splitlines())}
        return TurnOutcome(exit_kind="completed", answer_text=self._answer)

    def receipt(self) -> dict[str, Any]:
        """Return the receipt the server answered the report call with."""
        assert 2 in self.answers, self.stderr
        result: dict[str, Any] = self.answers[2]["result"]
        assert result.get("isError") is not True, result
        receipt: dict[str, Any] = result["structuredContent"]
        return receipt

    def patch_answer(self, index: int) -> dict[str, Any]:
        """Return the server's whole answer to the *index*-th patch call."""
        request_id = PATCH_FIRST_ID + index
        assert request_id in self.answers, self.stderr
        answer: dict[str, Any] = self.answers[request_id]["result"]
        return answer


@asynccontextmanager
async def daemon_socket(ctx: MethodContext, runtime_dir: Path) -> AsyncIterator[None]:
    """Serve *ctx* on the socket the per-Run server connects to."""
    server = await asyncio.start_unix_server(
        lambda reader, writer: handle_connection(reader, writer, ctx),
        path=str(runtime_dir / "eawfd.sock"),
    )
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


def certify(
    ctx: MethodContext, canary: CanaryProvision, worker: McpServerWorker, runtime_dir: Path
) -> MachineCertification:
    """Walk the canary under the seeded Task with *worker* as the only launcher."""

    async def scenario() -> dict[str, Any]:
        async with daemon_socket(ctx, runtime_dir):
            return await methods.dispatch(
                RUNTIME_CERTIFY_METHOD,
                ctx,
                {
                    "repo_root": str(canary.root),
                    "runtime": "claude-code",
                    "task": TASK_KEY,
                    "actor": ACTOR,
                },
            )

    methods.ensure_all_methods_registered()
    with mock.patch.object(native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((worker,)))):
        return MachineCertification.model_validate(asyncio.run(scenario()))


def dispatch_task(
    ctx: MethodContext, canary: CanaryProvision, worker: McpServerWorker, runtime_dir: Path
) -> QualifiedUrn:
    """Dispatch the seeded Task to Claude Code, await its turn and return its Run."""

    async def scenario() -> str:
        async with daemon_socket(ctx, runtime_dir):
            answer = await methods.dispatch(
                TASK_DISPATCH_METHOD,
                ctx,
                {
                    "repo_root": str(canary.root),
                    "actor": ACTOR,
                    "budget": BUDGET,
                    "task": TASK_KEY,
                    "provider": "claude-code",
                },
            )
            context = ctx.native_root_context(canary.root / ".ea")
            turn = supervisor_of(context).task_of(answer["run_ref"])
            if turn is not None:
                await turn
            return str(answer["run_ref"])

    with mock.patch.object(native_dispatch, "NATIVE_LAUNCHERS", dict(compile_launchers((worker,)))):
        return parse_qualified_urn(asyncio.run(scenario()))


def run_state(
    ctx: MethodContext, canary: CanaryProvision, urn: QualifiedUrn
) -> tuple[str, AcceptedReport | None, list[FileChangePayload]]:
    """Return the Run's status, its accepted report and its recorded file changes."""
    context = ctx.native_root_context(canary.root / ".ea")
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        status = native_dispatch.stored_run(session, records, urn).status.value
    changes = [
        line.payload
        for line in run_events_of(records, urn)
        if isinstance(line.payload, FileChangePayload)
    ]
    return status, accepted_report_of(records, str(urn)), changes


def canary_tree(tmp_path: Path) -> CanaryProvision:
    """Return the certify tree with the one Repository row a file change is filed under."""
    canary = make_tree(tmp_path)
    seed(canary, {"repository": {"REP-EAWF": repository_row()}})
    return canary


def test_the_canary_binds_its_content_only_report_through_the_real_server(
    tmp_path: Path, runtime_dir: Path
) -> None:
    canary = canary_tree(tmp_path)
    ctx = daemon_ctx(tmp_path / "runtime")
    answer = json.dumps({"outcomes": outcomes()})
    worker = McpServerWorker(
        tmp_path / "home",
        report={"verdict": "pass", "summary": "every attempt was denied", "body": answer},
        answer=answer,
    )

    row = certify(ctx, canary, worker, runtime_dir)

    receipt = worker.receipt()
    assert receipt["status"] == "succeeded", receipt
    assert receipt["bounded_output"]["accepted"] is True
    assert row.outcome == "certified"
    statuses = {item.capability_id: item.status for item in row.capabilities}
    assert statuses["semantic_tools"] == "verified"
    assert worker.capsule is not None
    status, accepted, changes = run_state(ctx, canary, parse_qualified_urn(receipt["run_ref"]))
    assert status == "COMPLETED"
    assert accepted is not None
    assert accepted.contract_digest == worker.capsule.contract_digest
    assert accepted.report_schema_ref == worker.capsule.report_schema_ref
    assert not any(
        path.startswith(f"{MCP_ARTIFACT_DIRNAME}/")
        for change in changes
        for path in change.changed_paths
    )


def test_a_task_run_binds_its_content_only_report_through_the_real_server(
    tmp_path: Path, runtime_dir: Path
) -> None:
    canary = canary_tree(tmp_path)
    ctx = daemon_ctx(tmp_path / "runtime")
    answer = json.dumps({"outcomes": outcomes()})
    certify(
        ctx,
        canary,
        McpServerWorker(
            tmp_path / "home",
            report={"verdict": "pass", "summary": "every attempt was denied"},
            answer=answer,
        ),
        runtime_dir,
    )
    worker = McpServerWorker(
        tmp_path / "home",
        report={
            "verdict": "pass-with-followups",
            "summary": "the task's work is done",
            "criteria": [{"criterion_id": "CR-01", "outcome": "met"}],
            "body": "## Outcome\n\nThe change landed.",
        },
    )

    run_urn = dispatch_task(ctx, canary, worker, runtime_dir)

    receipt = worker.receipt()
    assert receipt["bounded_output"]["accepted"] is True, receipt
    assert receipt["run_ref"] == str(run_urn)
    assert worker.capsule is not None
    status, accepted, changes = run_state(ctx, canary, run_urn)
    assert status == "COMPLETED"
    assert accepted is not None
    assert accepted.contract_digest == worker.capsule.contract_digest
    assert accepted.verdict.value == "pass-with-followups"
    assert not any(
        path.startswith(f"{MCP_ARTIFACT_DIRNAME}/")
        for change in changes
        for path in change.changed_paths
    )


def test_a_task_run_writes_its_workspace_and_its_work_is_committed_on_the_lease_branch(
    tmp_path: Path, runtime_dir: Path
) -> None:
    canary = canary_tree(tmp_path)
    ctx = daemon_ctx(tmp_path / "runtime")
    certify(
        ctx,
        canary,
        McpServerWorker(
            tmp_path / "home",
            report={"verdict": "pass", "summary": "every attempt was denied"},
            answer=json.dumps({"outcomes": outcomes()}),
        ),
        runtime_dir,
    )
    worker = McpServerWorker(
        tmp_path / "home",
        report={
            "verdict": "pass",
            "summary": "hello.txt holds canary",
            "criteria": [{"criterion_id": "CR-01", "outcome": "met"}],
        },
        patches=(
            {"files": [{"path": "src/hello.txt", "content": "canary\n"}]},
            {"files": [{"path": "hello.txt", "content": "outside the claims\n"}]},
        ),
    )

    run_urn = dispatch_task(ctx, canary, worker, runtime_dir)

    assert worker.capsule is not None
    assert "workspace_apply_patch" in worker.capsule.tool_grants
    written = worker.patch_answer(0)
    assert written.get("isError") is not True, written
    assert written["structuredContent"]["bounded_output"]["changed_paths"] == ["src/hello.txt"]
    refused = worker.patch_answer(1)
    assert refused["isError"] is True
    assert refused["structuredContent"]["status"] == "denied"
    assert refused["structuredContent"]["error"]["code"] == "SCOPE_DENIED"
    assert worker.receipt()["bounded_output"]["accepted"] is True
    status, accepted, changes = run_state(ctx, canary, run_urn)
    assert status == "COMPLETED"
    assert accepted is not None
    assert [change.changed_paths for change in changes] == [("src/hello.txt",)]
    context = ctx.native_root_context(canary.root / ".ea")
    (lease,) = [held for held in root_leases(context) if str(held.run_ref) == str(run_urn)]
    assert lease.status is not LeaseStatus.ACTIVE
    commit = git.commit_sha(canary.root, lease.branch)
    assert commit != lease.base_commit
    assert git.invoke(canary.root, "show", f"{commit}:src/hello.txt").stdout == "canary\n"
    committed = git.invoke(canary.root, "show", "--name-only", "--format=%B", commit).stdout
    assert f"Task: {TASK_KEY}" in committed
    assert "\nsrc/hello.txt" in committed
    assert "\nhello.txt" not in committed
    assert MCP_ARTIFACT_DIRNAME not in committed
