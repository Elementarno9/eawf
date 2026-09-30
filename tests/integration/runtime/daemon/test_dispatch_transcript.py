"""A Run Eawf dispatches itself states its worker's messages and its spawn failure.

RUN-062 names the runtime adapters the producers of a Run's transcript lines, and
CON-170 draws a message block for each thing the runner says. A Run the host harness
spawned gets its lines from the host's hooks; a Run Eawf starts itself has no hook, so
the dispatch path is the producer: the launcher reads the worker's stream-json stdout
as it arrives and hands each message to the dispatch's sink, which states it on the
Run's stream, and a worker that fails to start leaves an ``error_observed`` line
saying why.

The launcher cases drive the real
:class:`~eawf.runtime.runtimes.claude.adapter.ClaudeNativeLauncher` against a fake
``claude`` CLI printing a scripted stream-json turn; the dispatch cases drive the real
dispatch driver into a disposable canary and read the Run's lines back off its ledger.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.projection.transcript import build_transcript_blocks
from eawf.kernel.runtime.events import (
    ErrorPayload,
    MessageSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import DispatchRefusal
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.runtimes.adapter import (
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
)
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from eawf.runtime.runtimes.stream_json import stream_message_payloads
from eawf.surfaces.tui.console.renderers.transcript import NATIVE_GLYPH, native_word
from tests.integration.runtime.daemon.test_native_dispatch import (
    LedgerReadingLauncher,
    dispatch,
    ledger_records,
    make_canary,
    method_ctx,
    run_urn,
)
from tests.integration.runtime.test_run_meter_claude_usage import (
    _bypass_jail,
    _claude_spec,
    _request,
    _write_fake_claude_cli,
)

pytestmark = pytest.mark.integration

#: What the worker says, in order.
SAID: Final = (
    "Reading the cache module to see where entries expire.",
    "The expiry check runs before the write, so a stale entry is never served.",
)

#: One headless turn as ``claude -p --output-format stream-json`` prints it: a system
#: line, the two things the worker says around a tool call and its result, and the
#: result envelope. Only the two sayings are messages.
STREAM: Final[tuple[str, ...]] = (
    json.dumps({"type": "system", "subtype": "init", "session_id": "sess-dispatch"}),
    json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "text", "text": SAID[0]},
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
                ],
            },
        }
    ),
    json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
            },
        }
    ),
    "not-json-at-all",
    json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m2", "content": [{"type": "text", "text": SAID[1]}]},
        }
    ),
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-dispatch",
            "result": "done",
        }
    ),
)


class StreamingLauncher(LedgerReadingLauncher):
    """A launcher whose worker prints :data:`STREAM`, relayed as the Claude launcher does."""

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Hand each message the stream carries to the dispatch's sink, then announce."""
        assert request.message_sink is not None, "a dispatch always records its messages"
        for line in STREAM:
            for message in stream_message_payloads(line):
                await request.message_sink(message)
        return await super().launch(request)


class FailingLauncher(LedgerReadingLauncher):
    """A launcher whose worker exits before it announces itself."""

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Fail the spawn the way a non-zero exit does."""
        raise RuntimeSpawnError("claude exited 1 before announcing itself: auth expired")


def _stream(canary: CanaryProvision, runtime: Path) -> tuple[RunEventRecord, ...]:
    """Return the dispatched Run's own stream lines, in ledger order."""
    return run_events_of(ledger_records(canary, runtime), run_urn())


def test_stream_message_payloads_reads_what_the_worker_says_and_nothing_else() -> None:
    """A tool call, its result, the envelope and a non-JSON line are not messages."""
    said = [message.summary for line in STREAM for message in stream_message_payloads(line)]

    assert said == list(SAID)
    assert stream_message_payloads("") == ()
    assert stream_message_payloads("[1, 2]") == ()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_run_062_the_claude_launcher_relays_each_message_as_it_arrives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RUN-062: the real launcher hands the sink the worker's messages, in order."""
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_fake_claude_cli(workspace, STREAM)
    relayed: list[MessageSummaryPayload] = []

    async def sink(message: MessageSummaryPayload) -> None:
        relayed.append(message)

    request = _request(spec=_claude_spec(), workspace=workspace, usage_sink=None)
    request = request.model_copy(update={"message_sink": sink})
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(workspace / "claude")

    outcome = asyncio.run(ClaudeNativeLauncher(adapter=adapter).launch(request))

    assert outcome.provider_session_ref == "sess-dispatch"
    assert [(m.message_role, m.summary) for m in relayed] == [("assistant", s) for s in SAID]


def test_run_062_con_170_a_dispatched_turn_draws_message_blocks(tmp_path: Path) -> None:
    """RUN-062, CON-170: each message of the dispatched turn is a ``¶ message`` block."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    dispatch(method_ctx(runtime), canary, StreamingLauncher(canary, runtime))

    events = _stream(canary, runtime)
    assert [e.event_kind for e in events] == [RunEventKind.MESSAGE_SUMMARIZED] * len(SAID)
    assert [e.run_sequence for e in events] == [1, 2]
    blocks, _purged = build_transcript_blocks(events)
    assert [block.text.value for block in blocks] == list(SAID)
    assert {f"{NATIVE_GLYPH[native_word(block)]} {native_word(block)}" for block in blocks} == {
        "¶ message"
    }


def test_run_062_a_redispatch_states_no_message_twice(tmp_path: Path) -> None:
    """A dispatch whose attempt already spawned starts no worker, so says nothing again."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"
    dispatch(method_ctx(runtime), canary, StreamingLauncher(canary, runtime))

    dispatch(method_ctx(runtime), canary, StreamingLauncher(canary, runtime))

    assert len(_stream(canary, runtime)) == len(SAID)


def test_con_170_a_worker_that_fails_to_start_leaves_an_error_line(tmp_path: Path) -> None:
    """CON-170: the spawn failure is an ``error_observed`` line naming the refusal."""
    canary = make_canary(tmp_path / "repo")
    runtime = tmp_path / "runtime"

    with pytest.raises(DaemonValidationError, match=DispatchRefusal.SPAWN_FAILED.value):
        dispatch(method_ctx(runtime), canary, FailingLauncher(canary, runtime))

    (line,) = _stream(canary, runtime)
    assert line.event_kind is RunEventKind.ERROR_OBSERVED
    assert isinstance(line.payload, ErrorPayload)
    assert line.payload.code == DispatchRefusal.SPAWN_FAILED.value
    assert line.payload.retry_class == "transient_same_run"
    assert "auth expired" in line.payload.message
    assert line.payload.diagnostic_ref is not None
    (block,) = build_transcript_blocks((line,))[0]
    assert f"{NATIVE_GLYPH[native_word(block)]} {native_word(block)}" == "! error"
    assert dict(block.body)["CODE"] == DispatchRefusal.SPAWN_FAILED.value
