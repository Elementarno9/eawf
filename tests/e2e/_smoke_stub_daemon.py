"""Run the real ``eawfd`` with the claude launcher replaced by a deterministic stub.

Started by path (``python tests/e2e/_smoke_stub_daemon.py``) by the Milestone
smoke, so the daemon is the shipped one in every respect but the process a
dispatch starts: the supervisor, the Run lifecycle, the workspace lease, the
per-Run MCP binding, the semantic gateway, ``commit_worker_edits`` and the
report binding all run as shipped. The stub launcher writes the Run's real MCP
configuration through :func:`native_run_server_config`, then starts
``_smoke_worker.py`` in the leased workspace; that worker edits one file and
files its report through the real ``eawf mcp serve`` server, exactly as a
provider process reaches the daemon.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Final

from eawf.runtime.daemon import native_dispatch, runtime_certifier
from eawf.runtime.daemon.main import main
from eawf.runtime.mcp.native_launch import native_run_server_config
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    TurnOutcome,
    compose_worker_hello,
)
from eawf.runtime.workspace.lease import MCP_ARTIFACT_DIRNAME

#: The worker the stub starts, beside this file.
WORKER: Final = Path(__file__).resolve().with_name("_smoke_worker.py")

#: The lane the per-Run MCP configuration is rendered for.
RUNTIME_ID: Final = "claude-code"


class StubClaudeLauncher:
    """Stand in for the claude launcher: start the stub worker, await its exit.

    Attributes:
        provider_kind: The provider the stub answers for.
    """

    provider_kind = "claude"

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Write the Run's MCP binding, start the worker and hand its turn back."""
        native_run_server_config(request, runtime_id=RUNTIME_ID)
        binding = request.workspace / MCP_ARTIFACT_DIRNAME / "binding.json"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER),
            "--binding",
            str(binding),
            cwd=str(request.workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(request.prompt.encode("utf-8"))
        process.stdin.close()
        session_ref = f"smoke-stub-{request.spec.run_ref.entity_key}"
        return NativeLaunchOutcome(
            provider_session_ref=session_ref,
            subprocess_pid=process.pid,
            hello=compose_worker_hello(
                spec=request.spec,
                capsule=request.capsule,
                provider_session_ref=session_ref,
                sdk_version="1.0.0",
                worker_protocol_version="1.0.0",
                event_codec_version="1.0.0",
                hello_sequence=request.hello_sequence,
            ),
            turn=asyncio.get_running_loop().create_task(self._turn(process)),
        )

    async def _turn(self, process: asyncio.subprocess.Process) -> TurnOutcome:
        """Await the worker and read its exit as the turn's ending."""
        assert process.stderr is not None
        stderr = await process.stderr.read()
        code = await process.wait()
        if code == 0:
            return TurnOutcome(exit_kind="completed")
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        return TurnOutcome(exit_kind="failed", error_text=f"stub worker exited {code}: {tail[0]}")


def install() -> None:
    """Route every claude dispatch to the stub and keep the harness probe off."""
    native_dispatch.NATIVE_LAUNCHERS = native_dispatch.compile_launchers(  # type: ignore[misc]
        (StubClaudeLauncher(),)
    )
    # A Run started on a reported harness version would probe the real binary
    # on PATH from a background thread; the stub has none to probe.
    runtime_certifier.start_auto_certification = lambda *_a, **_k: None


if __name__ == "__main__":
    install()
    main()
