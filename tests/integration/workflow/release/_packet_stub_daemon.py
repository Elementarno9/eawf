"""Run the real ``eawfd`` with the claude launcher replaced by the smoke's deterministic stub.

Started by path by the packet producer's test, so every step of a packet but
the worker process runs as shipped. On top of the smoke's stub, the launcher
hands back a transcript file, as the claude launcher does, so the packet's
transcript step reads an indexed transcript.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from eawf.runtime.daemon.main import main
from eawf.runtime.runtimes.adapter import NativeLaunchOutcome, NativeLaunchRequest
from tests.e2e import _smoke_stub_daemon as smoke


class TranscribedStubLauncher(smoke.StubClaudeLauncher):
    """The smoke's stub launcher, also writing the session's transcript."""

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Start the stub worker and point the outcome at a claude-format transcript."""
        outcome = await super().launch(request)
        transcript = request.workspace.parent / f"{outcome.provider_session_ref}.jsonl"
        lines = (
            {"type": "user", "message": {"role": "user", "content": request.prompt}},
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        )
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")
        return outcome.model_copy(update={"transcript_path": str(transcript)})


if __name__ == "__main__":
    smoke.install()
    from eawf.runtime.daemon import native_dispatch

    native_dispatch.NATIVE_LAUNCHERS = native_dispatch.compile_launchers(  # type: ignore[misc]
        (TranscribedStubLauncher(),)
    )
    main()
