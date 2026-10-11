"""A native Run in a leased worktree files its report through the real MCP server.

A leased worktree holds no generation of its own, so a per-Run binding that
routed calls to the worktree had every semantic call refused by the
daemon's native fence, and the worker's ``submit_report`` came back as a
transient "the daemon did not answer". This case drives the whole path:
the production :func:`~eawf.runtime.mcp.native_launch.native_run_server_config`
writes the binding for a Run whose workspace is a real leased git worktree,
the shipped ``eawf mcp serve`` command named by the rendered lane
configuration runs as a child process, and an in-process daemon listening on
a socket under a private runtime directory answers it. No provider is
started and no daemon outside the test is reached.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.candidate.seal import accepted_report_of
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.daemon.server import handle_connection
from eawf.runtime.mcp.native_launch import native_run_server_config
from eawf.runtime.mcp.semantic_stdio import RunServerBinding
from eawf.runtime.runtimes.adapter import NativeLaunchRequest
from eawf.runtime.workspace.lease import issue_lease, workspace_path
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row
from tests.integration.runtime.daemon.test_worklease import make_repo

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the daemon listens on a Unix socket"),
]

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
RUN_KEY: Final = "RUN-00000010"
ROUTE_REVISION: Final = 3


@pytest.fixture
def runtime_dir() -> Iterator[Path]:
    """A runtime directory short enough for an AF_UNIX socket path."""
    path = Path(tempfile.mkdtemp(prefix="eawf-rt-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def provision(root: Path) -> tuple[CanaryProvision, MethodContext, Epoch2RootContext]:
    """Return a canary over a real repository holding one RUNNING task Run."""
    make_repo(root)
    provisioned = provision_canary(repo_root=root, ref=canary_ref("BND"), provisioned_at=AT)
    row = seed_row("run", "RUNNING")
    row["scope"] = {
        "scope_kind": "task",
        "purpose": RunPurpose.IMPLEMENT.value,
        "task_ref": fx.TASK_URN,
        "write_set": ["src"],
    }
    row["started_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    seed(provisioned, {"run": {RUN_KEY: row}})
    # The ping answer is checked against this release by the serving CLI.
    ctx = MethodContext(
        started_at=AT.isoformat(),
        pid=os.getpid(),
        protocol_version="1",
        version=__version__,
        wal_dir=provisioned.runtime_dir / "wal",
    )
    methods.ensure_all_methods_registered()
    return provisioned, ctx, ctx.native_root_context(provisioned.root / ".ea")


def report_capsule(spec: CompiledRunSpec) -> AuthorityCapsule:
    """Seal a capsule granting the Run its report tool."""
    return seal_capsule(
        spec=spec,
        request=CapsuleRequest.model_validate(
            {
                "criteria_digest": f"sha256:{'b' * 64}",
                "report_schema_ref": "schema://executor-report/v1",
                "tool_grants": ["submit_report"],
                "stop_conditions": ["budget_exhausted"],
            }
        ),
    )


def serve_command(config_files: Mapping[str, str]) -> list[str]:
    """Return the server command the rendered claude configuration registers."""
    for body in config_files.values():
        document = json.loads(body)
        entry = document.get("mcpServers", {}).get("eawf-semantic")
        if entry is not None:
            return [entry["command"], *entry["args"]]
    raise AssertionError("the rendered configuration registers no eawf-semantic server")


def frames() -> bytes:
    """Return the initialize and submit_report requests a provider sends."""
    requests: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "submit_report",
                "arguments": {
                    "verdict": "pass",
                    "summary": "the leased worktree's work is done",
                    "body": "Filed through the real server.",
                },
            },
        },
    ]
    return b"".join(json.dumps(request).encode() + b"\n" for request in requests)


async def serve_one_session(
    ctx: MethodContext,
    *,
    runtime_dir: Path,
    argv: list[str],
    cwd: Path,
    stdin: bytes,
    env: dict[str, str],
) -> tuple[bytes, bytes, int]:
    """Run the MCP server child against an in-process daemon socket."""
    server = await asyncio.start_unix_server(
        lambda reader, writer: handle_connection(reader, writer, ctx),
        path=str(runtime_dir / "eawfd.sock"),
    )
    try:
        child = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(child.communicate(stdin), timeout=120)
        return stdout, stderr, child.returncode if child.returncode is not None else -1
    finally:
        server.close()
        await server.wait_closed()


def test_a_launch_request_naming_no_repository_root_is_refused(tmp_path: Path) -> None:
    """Without the root a launcher could only fall back to the workspace."""
    spec = compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )
    with pytest.raises(ValidationError, match="repo_root"):
        NativeLaunchRequest.model_validate(
            {
                "spec": spec,
                "capsule": report_capsule(spec),
                "workspace_handle": f"wsh-{'a' * 32}",
                "workspace": tmp_path,
                "prompt": "file the report",
                "hello_sequence": 1,
            }
        )


def test_a_leased_worktree_run_files_its_report_through_the_real_server(
    tmp_path: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioned, ctx, context = provision(tmp_path / "repo")
    lease = issue_lease(
        context,
        run_ref=fx.RUN_URN,
        task_ref=fx.TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base="main",
        writable_roots=("src",),
        now=datetime.now(UTC),
        ttl=timedelta(hours=1),
    )
    workspace = workspace_path(context, handle=lease.workspace_handle)
    assert (workspace / ".git").is_file(), "the workspace is a linked git worktree"
    assert not (workspace / ".ea").exists(), "the worktree carries no generation"
    spec = compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )
    capsule = report_capsule(spec)
    asyncio.run(
        methods.dispatch(
            RUN_BIND_METHOD,
            ctx,
            {
                "repo_root": str(provisioned.root),
                "urn": fx.RUN_URN,
                "compiled_spec_digest": capsule.compiled_spec_digest,
                "authority_capsule_digest": capsule.contract_digest,
                "route_policy_revision": ROUTE_REVISION,
            },
        )
    )
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime_dir))

    config = native_run_server_config(
        NativeLaunchRequest(
            spec=spec,
            capsule=capsule,
            workspace_handle=lease.workspace_handle,
            workspace=workspace,
            repo_root=context.identity.tree_root.parent,
            prompt="file the report",
            hello_sequence=1,
        ),
        runtime_id="claude-code",
    )

    binding = RunServerBinding.model_validate_json(
        (workspace / ".eawf-mcp" / "binding.json").read_text(encoding="utf-8")
    )
    assert Path(binding.repo_root) == provisioned.root.resolve()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "EAWF_REGISTRY_PATH": str(tmp_path / "home" / "registry.json"),
    }
    stdout, stderr, code = asyncio.run(
        serve_one_session(
            ctx,
            runtime_dir=runtime_dir,
            argv=serve_command(config.files),
            cwd=workspace,
            stdin=frames(),
            env=env,
        )
    )

    assert code == 0, stderr.decode()
    answers = {answer["id"]: answer for answer in map(json.loads, stdout.decode().splitlines())}
    result = answers[2]["result"]
    assert result.get("isError") is not True, result
    receipt = result["structuredContent"]
    assert receipt["status"] == "succeeded"
    assert receipt["bounded_output"]["accepted"] is True
    assert receipt["bounded_output"]["report_ref"] == fx.RUN_URN
    with context.session([fx.RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    accepted = accepted_report_of(records, fx.RUN_URN)
    assert accepted is not None
    assert accepted.contract_digest == capsule.contract_digest
