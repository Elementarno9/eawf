"""The in-flight meter sums Claude's per-call usage instead of ratcheting to its max.

Claude Code's stream-json ``assistant`` event carries what ONE API call
billed, not a running session total -- unlike codex's ``turn.completed``,
which already discloses a cumulative reading. Feeding the in-flight meter
(:class:`~eawf.runtime.runtimes.metering.InFlightMeter`) a bare per-call
figure makes it ratchet to the largest single call and never see the calls
together, so a Run whose calls TOGETHER cross its token cap runs past it
metered only at its biggest message.

These cases drive the REAL :class:`~eawf.runtime.runtimes.claude.adapter.ClaudeNativeLauncher`
against a fake ``claude`` CLI that prints a scripted stream-json transcript --
the same fixture shape :mod:`tests.integration.runtime.test_native_run_mcp_usage`
uses -- plus focused unit cases on the accumulator the launcher now folds
every reading through before relaying it.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.runtimes.adapter import NativeLaunchRequest
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import (
    ClaudeAdapter,
    ClaudeNativeLauncher,
    _ClaudeUsageAccumulator,
)
from eawf.runtime.runtimes.metering import InFlightMeter, MeterReading, UsageSample
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

pytestmark = pytest.mark.integration

#: Each of the three distinct calls below bills this many tokens: enough that
#: any ONE of them stays well under the cap, but the sum of three does not.
_PER_CALL_USAGE: Final[dict[str, int]] = {"input_tokens": 450, "output_tokens": 50}

#: The sealed ceiling: above one call (500), above two (1000), below three (1500).
_CAP_TOKENS: Final = 1_200

_CLAUDE_LINES: Final[tuple[str, ...]] = (
    json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m1", "model": "fixture-model", "usage": dict(_PER_CALL_USAGE)},
        }
    ),
    # Claude Code appends one row per content block, each repeating the SAME
    # message.usage -- the duplicate must not be double-counted.
    json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m1", "model": "fixture-model", "usage": dict(_PER_CALL_USAGE)},
        }
    ),
    "not-json-at-all",
    json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m2", "model": "fixture-model", "usage": dict(_PER_CALL_USAGE)},
        }
    ),
    json.dumps({"type": "assistant", "message": {"id": "m-none"}}),
    json.dumps(
        {
            "type": "assistant",
            "message": {"id": "m3", "model": "fixture-model", "usage": dict(_PER_CALL_USAGE)},
        }
    ),
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "sess-fake-claude",
            "result": "ok",
            "total_cost_usd": 0.0,
            "usage": dict(_PER_CALL_USAGE),
        }
    ),
)


def _bypass_jail(
    argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
) -> list[str]:
    """No-op jail stub, at parity with ``test_native_run_mcp_usage.py``."""
    return argv


def _write_fake_claude_cli(directory: Path, lines: tuple[str, ...]) -> Path:
    """Write a fake ``claude`` CLI that prints *lines* and records its argv."""
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "with open('argv.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv, handle)\n"
        f"for line in {list(lines)!r}:\n"
        "    print(line)\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _claude_spec() -> CompiledRunSpec:
    """Return a compiled spec whose winning profile is the claude lane."""
    profile = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    configuration = fx.configuration(
        profiles=[profile],
        routes=[fx.task_route_document(allowed_profiles=["fixture_claude"])],
        global_routes=[],
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=configuration,
        bindings=[fx.binding(driver_ref=fx.OTHER_DRIVER_REF)],
        compiled_at=fx.COMPILED_AT,
    )


def _request(*, spec: CompiledRunSpec, workspace: Path, usage_sink: object) -> NativeLaunchRequest:
    """Return the launch request the fake claude lane is started with."""
    capsule = seal_capsule(
        spec=spec, request=CapsuleRequest.model_validate(capsule_request(token_budget=100_000))
    )
    return NativeLaunchRequest(
        spec=spec,
        capsule=capsule,
        workspace_handle=f"wsh-{'a' * 32}",
        workspace=workspace,
        prompt="implement the fixture task",
        hello_sequence=1,
        usage_sink=usage_sink,
    )


class _CappedMeterSink:
    """A ``usage_sink`` backed by the production :class:`InFlightMeter`.

    Stands in for :class:`~eawf.runtime.daemon.methods.run_budget.InFlightRunMeter`
    without its ledger / kill-ladder plumbing -- that wiring has its own
    coverage (``test_run_meter_producer.py``). This isolates the ONE seam
    this wave fixes: whether the fold the meter is handed is a running
    total or a bare per-call figure.

    Attributes:
        cap_tokens: The ceiling a reading crosses at or above.
        readings: Every fold the meter produced, in arrival order.
        terminated_after: What this sink answered for each reading.
    """

    def __init__(self, *, cap_tokens: int) -> None:
        self._meter = InFlightMeter()
        self.cap_tokens = cap_tokens
        self.readings: list[MeterReading] = []
        self.terminated_after: list[bool] = []

    async def sink(self, sample: UsageSample, pgid: int | None) -> bool:
        """Fold *sample* and answer whether the running total crossed the cap."""
        reading = self._meter.observe(sample)
        self.readings.append(reading)
        crossed = reading.observed_tokens >= self.cap_tokens
        self.terminated_after.append(crossed)
        return crossed


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_claude_native_launch_meters_the_sum_of_its_calls_not_the_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calls that together cross the cap terminate, though each alone stays under it.

    Gate-fire proof (CR-01): reverting the launcher's accumulator so it
    relays each stream-json line's bare per-call reading straight to the
    sink -- the pre-fix shape -- reds this case, because the meter would
    then ratchet to 500 (one call) and never answer ``True``.
    """
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_fake_claude_cli(workspace, _CLAUDE_LINES)
    sink = _CappedMeterSink(cap_tokens=_CAP_TOKENS)
    request = _request(spec=_claude_spec(), workspace=workspace, usage_sink=sink.sink)
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(workspace / "claude")

    outcome = asyncio.run(ClaudeNativeLauncher(adapter=adapter).launch(request))

    assert outcome.provider_session_ref == "sess-fake-claude"
    # Three DISTINCT calls reached the meter: the duplicate m1 content-block
    # row and the usage-free m-none event contributed no new reading.
    assert [reading.observed_tokens for reading in sink.readings] == [500, 1000, 1500]
    assert [reading.sample_count for reading in sink.readings] == [1, 2, 3]
    assert not any(reading.ratcheted for reading in sink.readings)
    # Every individual call (500) stays well under the cap; only the SUM of
    # the three crosses it, at the third reading.
    assert sink.cap_tokens > 500
    assert sink.terminated_after == [False, False, True]


# ---- accumulator boundary / error-path cases --------------------------------


def test_accumulator_sums_distinct_calls_into_a_running_total() -> None:
    """Two distinct message ids fold into a running total, not the larger alone."""
    accumulator = _ClaudeUsageAccumulator()
    line_1 = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "a", "usage": {"input_tokens": 10, "output_tokens": 1}},
        }
    )
    line_2 = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "b", "usage": {"input_tokens": 20, "output_tokens": 2}},
        }
    )

    first = accumulator.observe(line_1)
    second = accumulator.observe(line_2)

    assert first is not None and (first.input_tokens, first.output_tokens) == (10, 1)
    assert second is not None and (second.input_tokens, second.output_tokens) == (30, 3)


def test_accumulator_dedupes_a_repeated_message_id() -> None:
    """A repeated message id -- the duplicate content-block row -- adds nothing."""
    accumulator = _ClaudeUsageAccumulator()
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "a", "usage": {"input_tokens": 10, "output_tokens": 1}},
        }
    )

    first = accumulator.observe(line)
    second = accumulator.observe(line)

    assert first is not None
    assert second is None


def test_accumulator_folds_unconditionally_without_a_dedupe_key() -> None:
    """No message id, requestId, or uuid: every reading is folded, none skipped.

    Boundary case -- with nothing to identify the call, treating repeats as
    distinct is the safe default (matching
    :mod:`~eawf.runtime.runtimes.claude.transcript_counters`'s own
    unconditional fold when its dedupe key is unresolved).
    """
    accumulator = _ClaudeUsageAccumulator()
    line = json.dumps(
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}}
    )

    first = accumulator.observe(line)
    second = accumulator.observe(line)

    assert first is not None and (first.input_tokens, first.output_tokens) == (10, 1)
    assert second is not None and (second.input_tokens, second.output_tokens) == (20, 2)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("not-json-at-all", id="not_json"),
        pytest.param(json.dumps(["array", "not", "object"]), id="not_an_object"),
        pytest.param(json.dumps({"type": "result"}), id="wrong_event_type"),
        pytest.param(json.dumps({"type": "assistant"}), id="no_message"),
        pytest.param(
            json.dumps({"type": "assistant", "message": {"id": "a"}}), id="no_usage_block"
        ),
        pytest.param(
            json.dumps(
                {"type": "assistant", "message": {"id": "a", "usage": {"input_tokens": -1}}}
            ),
            id="negative_token_count",
        ),
    ],
)
def test_accumulator_skips_malformed_and_usage_free_lines(line: str) -> None:
    """A line that discloses no valid usage is skipped, not raised."""
    accumulator = _ClaudeUsageAccumulator()

    assert accumulator.observe(line) is None
