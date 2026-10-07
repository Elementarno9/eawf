"""A Campaign round's worker calls the daemon under the contract its Run is bound to.

A research round seals a capsule and hands it to the per-Run MCP server, and the
semantic gateway admits a call only when the Run recorded a binding naming that very
capsule. These cases drive ``runtime.campaign.start`` for one round with a fake provider
that starts the round's real ``eawf mcp serve`` child, reads a file of the tree and files
its report through it, against an in-process daemon on a private socket. No model and no
daemon outside the test are reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.certification import CapabilityCertification
from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.epoch2.campaign import Campaign
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.candidate.seal import accepted_report_of
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext, campaign_run
from eawf.runtime.daemon.methods.campaign_run import (
    CAMPAIGN_START_METHOD,
    HostResearchAgent,
    campaign_drive_in_flight,
)
from eawf.runtime.daemon.native_dispatch import run_binding_of, run_ledger
from eawf.runtime.daemon.semantic_gateway import run_receipts
from eawf.runtime.mcp.native_launch import native_run_server_config
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.workflow.evidence.machine_certification import append_machine_certification
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import _start
from tests.integration.runtime.daemon.test_native_dispatch import make_canary
from tests.integration.runtime.daemon.test_task_dispatch import CLAUDE_VERSION, certification
from tests.integration.runtime.mcp.test_report_content_end_to_end import (
    CHILD_TIMEOUT_SECONDS,
    daemon_ctx,
    daemon_socket,
    serve_command,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the daemon listens on a Unix socket"),
]

#: The file of the tree the round reads.
READ_PATH: Final = "src/module.py"

#: What the canary's repository holds at :data:`READ_PATH`.
MODULE_TEXT: Final = "x = 1\n"

#: The request ids the read and the report are sent under.
READ_ID: Final = 2
REPORT_ID: Final = 3

#: The round's final answer, in the closed shape the scheduler parses.
ANSWER: Final = (
    "```json\n"
    '{"report": "# Survey\\n\\n- one module", "outcome": "one module found", "sources": 1}'
    "\n```"
)


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A runtime directory short enough for an AF_UNIX socket path, pinned for the binding."""
    path = Path(tempfile.mkdtemp(prefix="eawf-rt-"))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_host_homes: Path, runtime_dir: Path
) -> Iterator[None]:
    """Keep the global layer, every host home and each round checkout under tmp.

    The socket directory is made first, under the system temp root, because a
    socket path under the test's own tmp tree is too long for ``AF_UNIX``.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    yield


def semantic_certification() -> MachineCertification:
    """Return a Claude Code certification that also attests the semantic tools."""
    base = certification("claude-code", CLAUDE_VERSION)
    [first, *_] = base.capabilities
    carried = CapabilityCertification(
        capability_id="semantic_tools",
        level=True,
        status="verified",
        basis="native",
        evidence_ref=first.evidence_ref,
        verified_at=first.verified_at,
        expires_at=first.verified_at + timedelta(days=90),
    )
    return base.model_copy(update={"capabilities": (*base.capabilities, carried)})


def campaign_tree(tmp_path: Path) -> CanaryProvision:
    """Return a git-backed canary with one live Track, certified to carry semantic tools."""
    canary = make_canary(tmp_path / "repo", rows={})
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    append_machine_certification(canary.root / ".ea" / "state.json", semantic_certification())
    return canary


class ReadingWorker:
    """A fake provider that reads one file and files its report through the real server.

    Attributes:
        provider_kind: The provider the fake stands in for.
        capsules: The capsule each round's launch carried, by the Run it was sealed for.
        answers: Each round's server answers by request id, by the Run.
    """

    def __init__(self, home: Path) -> None:
        """Bind the fake to the home its server child runs under."""
        self.provider_kind = "claude"
        self.capsules: dict[str, AuthorityCapsule] = {}
        self.answers: dict[str, dict[int, dict[str, Any]]] = {}
        self.stderr = ""
        self._home = home

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Render the per-Run server configuration and start the turn on it."""
        self.capsules[str(request.capsule.run_ref)] = request.capsule
        config = native_run_server_config(request, runtime_id="claude-code")
        session_ref = "claude-round-session"
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
                self._turn(serve_command(config.files), request)
            ),
            runtime_tuple=RunRuntimeTuple(harness="claude-code", harness_version=CLAUDE_VERSION),
        )

    async def _turn(self, argv: list[str], request: NativeLaunchRequest) -> TurnOutcome:
        """Read the file and file the report through the server child, then answer."""
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": READ_ID,
                "method": "tools/call",
                "params": {
                    "name": "repo_read",
                    "arguments": {
                        "resource_handle": request.workspace_handle,
                        "path": READ_PATH,
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": REPORT_ID,
                "method": "tools/call",
                "params": {
                    "name": "submit_report",
                    "arguments": {"verdict": "pass", "summary": "one module found"},
                },
            },
        ]
        child = await asyncio.create_subprocess_exec(
            *argv,
            cwd=request.workspace,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self._home),
                "EAWF_REGISTRY_PATH": str(self._home / "registry.json"),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        body = b"".join(json.dumps(item).encode() + b"\n" for item in frames)
        stdout, stderr = await asyncio.wait_for(
            child.communicate(body), timeout=CHILD_TIMEOUT_SECONDS
        )
        self.stderr = stderr.decode()
        self.answers[str(request.capsule.run_ref)] = {
            item["id"]: item for item in map(json.loads, stdout.decode().splitlines())
        }
        return TurnOutcome(exit_kind="completed", answer_text=ANSWER)

    def result(self, run_ref: str, request_id: int) -> dict[str, Any]:
        """Return the server's whole answer to one tool call of one round."""
        assert request_id in self.answers.get(run_ref, {}), self.stderr
        answer: dict[str, Any] = self.answers[run_ref][request_id]["result"]
        return answer


def drive_one_round(
    ctx: MethodContext,
    canary: CanaryProvision,
    worker: ReadingWorker,
    runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Campaign:
    """Start a Campaign one round wide, wait for its drive to end and return the Campaign."""

    def agent_for(runtime: str, context: Epoch2RootContext, _depth: Any) -> HostResearchAgent:
        return HostResearchAgent(runtime, context, EffortBucket.S, launchers={"claude": worker})

    monkeypatch.setattr(campaign_run, "research_agent_for", agent_for)

    async def scenario() -> str:
        async with daemon_socket(ctx, runtime_dir):
            started = await methods.dispatch(
                CAMPAIGN_START_METHOD, ctx, _start(canary.root, depth="shallow", agents=1)
            )
            for _ in range(2400):
                if not campaign_drive_in_flight():
                    break
                await asyncio.sleep(0.05)
            return str(started["record"]["key"])

    methods.ensure_all_methods_registered()
    key = asyncio.run(scenario())
    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.CAMPAIGN)
    return Campaign.model_validate(rows[key])


def test_every_campaign_round_is_bound_to_the_capsule_its_worker_holds(
    tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = campaign_tree(tmp_path)
    ctx = daemon_ctx(tmp_path / "runtime")
    worker = ReadingWorker(tmp_path / "home")

    campaign = drive_one_round(ctx, canary, worker, runtime_dir, monkeypatch)

    assert campaign.plan_steps
    assert len(worker.capsules) == len(campaign.plan_steps)
    context = ctx.native_root_context(canary.root / ".ea")
    for step in campaign.plan_steps:
        assert step.state.value == "done", campaign
        [run_urn] = step.run_refs
        run_ref = str(run_urn)
        capsule = worker.capsules[run_ref]
        assert set(capsule.tool_grants) == {"repo_read", "repo_search", "submit_report"}
        with context.session([run_urn]) as session:
            records = read_ledger_records(run_ledger(session))
            receipts = {item.tool_id.value: item for item in run_receipts(session, run_urn)}
        binding = run_binding_of(records, parse_qualified_urn(run_ref))
        assert binding is not None, "the round's Run records a contract binding"
        assert binding.authority_capsule_digest == capsule.contract_digest
        assert binding.capsule == capsule
        report = worker.result(run_ref, REPORT_ID)
        assert report.get("isError") is not True, report
        assert report["structuredContent"]["bounded_output"]["accepted"] is True
        accepted = accepted_report_of(records, run_ref)
        assert accepted is not None
        assert accepted.contract_digest == capsule.contract_digest
        read = worker.result(run_ref, READ_ID)
        assert read["structuredContent"]["status"] == "succeeded", read
        assert {key: item.result.status for key, item in receipts.items()} == {
            "repo_read": "succeeded",
            "submit_report": "succeeded",
        }


def test_a_campaign_round_reads_a_file_of_the_tree(
    tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = ReadingWorker(tmp_path / "home")
    ctx = daemon_ctx(tmp_path / "runtime")

    drive_one_round(ctx, campaign_tree(tmp_path), worker, runtime_dir, monkeypatch)

    assert worker.answers
    for run_ref in worker.answers:
        read = worker.result(run_ref, READ_ID)
        assert read.get("isError") is not True, read
        output = read["structuredContent"]["bounded_output"]
        assert output["text"] == MODULE_TEXT.rstrip("\n")
        assert output["byte_count"] == len(MODULE_TEXT.encode())
        assert output["truncated"] is False
