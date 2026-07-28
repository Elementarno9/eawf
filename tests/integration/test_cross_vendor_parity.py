"""Integration: cross-vendor spawn parity gates (P30-I10-W05).

Pins the binary wave criterion: the three runtime adapters (claude / codex /
opencode) emit an IDENTICAL metered / priced / sandboxed
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` for an equivalent spawn,
and codex + opencode honour a denied tool.

The shape parity is proven field-by-field over the three vendors' SpawnResult
(same Pydantic field set, same metering keys, same sandbox-enforcement
sequence). The per-call tool-deny (B081) is owned here for codex + opencode:

* codex maps the deny-list to its inverted ``-c tools.<name>=<bool>`` allowlist
  so a denied tool is absent from the ``true`` grant AND pinned ``false`` -- it
  can never reach the child's effective grant;
* opencode has no per-call deny flag, so its deny is honoured by the FS-jail
  floor (which confines the child's writes) and recorded as an ``argv-deny``
  enforcement event -- the denied tool never reaches a positive argv grant, so
  the deny cannot silently pass through.

The edge journeys (deny / cancel / fallback / overrun / egress-block) drive
each adapter through its failure terminal and assert the same terminal shape
across vendors.

Every test drives async coroutines via ``asyncio.run`` inside plain sync
``def test_`` bodies (the suite has no ``pytest-asyncio`` dep) and NEVER
touches the real network / subprocess -- the subprocess factory + the outbound
connector are always fakes.

The claude adapter (W04) is read-only here -- this wave imports it to assert
parity but mutates only the codex + opencode lanes.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.runtimes.adapter import RuntimeSpawnError, SpawnResult
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.codex.adapter import CodexAdapter
from eawf.runtime.runtimes.codex.adapter import (
    ConcurrentSpawnCapError as CodexConcurrentSpawnCapError,
)
from eawf.runtime.runtimes.opencode import adapter as opencode_adapter
from eawf.runtime.runtimes.opencode.adapter import (
    ConcurrentSpawnCapError as OpenCodeConcurrentSpawnCapError,
)
from eawf.runtime.runtimes.opencode.adapter import OpenCodeAdapter
from eawf.runtime.sandbox.egress_proxy import (
    SandboxEnforcementEvent,
    start_egress_proxy,
)

pytestmark = pytest.mark.integration

_SESSION_ID = "SES-executor"
_DENIED = ["Edit", "Bash"]


# ---------------------------------------------------------------------------
# Per-vendor well-formed result envelopes (the same shape each test asserts)
# ---------------------------------------------------------------------------

#: A claude single-result envelope with the full token classes + cost.
_CLAUDE_ENVELOPE: dict[str, object] = {
    "type": "result",
    "is_error": False,
    "session_id": "claude-sess",
    "result": "the answer text",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 80,
        "cache_read_input_tokens": 200,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 50,
            "ephemeral_1h_input_tokens": 30,
        },
    },
}

#: A codex ``--json`` event stream (newline-delimited JSON objects). Codex's
#: ``input_tokens`` is GROSS (includes cached), so it carries 300 = 100
#: non-cached + 200 cached; the parser splits them to the same 100 input /
#: 200 cache-read the other two vendors report directly.
_CODEX_EVENTS: list[dict[str, object]] = [
    {"type": "thread.started", "thread_id": "codex-thread"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "the answer text"}},
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 42},
    },
]

#: An opencode ``run --format json`` NDJSON event stream.
_OPENCODE_EVENTS: list[dict[str, object]] = [
    {
        "type": "step_start",
        "sessionID": "opencode-sess",
        "part": {"type": "step-start"},
    },
    {
        "type": "text",
        "sessionID": "opencode-sess",
        "part": {"type": "text", "text": "the answer text"},
    },
    {
        "type": "step_finish",
        "sessionID": "opencode-sess",
        "part": {
            "type": "step-finish",
            "tokens": {"input": 100, "output": 42, "cache": {"write": 80, "read": 200}},
            "cost": 0.0123,
        },
    },
]


def _claude_stdout() -> bytes:
    return json.dumps(_CLAUDE_ENVELOPE).encode("utf-8")


def _codex_stdout() -> bytes:
    return ("\n".join(json.dumps(row) for row in _CODEX_EVENTS) + "\n").encode("utf-8")


def _opencode_stdout() -> bytes:
    return ("\n".join(json.dumps(row) for row in _OPENCODE_EVENTS) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Fake subprocess (NEVER a real CLI) + per-vendor patch helper
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for :class:`asyncio.subprocess.Process`.

    The claude / codex lanes drain ``.stdout`` / ``.stderr`` incrementally;
    the opencode lane still buffers via ``communicate``. The fake serves both:
    :meth:`open_streams` (called from the in-loop factory) populates the
    StreamReaders, and ``communicate`` replays the same canned bytes. A hung
    child never feeds EOF (its drain / communicate blocks until the spawn's
    wait_for ceiling fires).
    """

    def __init__(
        self, *, stdout: bytes, stderr: bytes, returncode: int, hang: bool = False
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = returncode
        self.pid = 4321
        self._hang = hang
        self.killed = False
        self.waited = False
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None

    def open_streams(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        if not self._hang:
            self.stdout.feed_data(self._stdout)
            self.stdout.feed_eof()
            self.stderr.feed_data(self._stderr)
            self.stderr.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode if self.returncode is not None else -1


def _patch_vendor(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    proc: _FakeProcess,
) -> list[list[str]]:
    """Patch a vendor adapter module's subprocess factory + jail passthrough.

    Neutralises the OS jail to a passthrough so the inner argv assertions stay
    deterministic across hosts (a box with bwrap / sandbox-exec on PATH would
    otherwise prefix the wrapper), and pins ``build_child_env`` to a fixed
    scrubbed env so the env-scrub recorder is deterministic. Returns the
    per-spawn captured argv list.
    """
    calls: list[list[str]] = []

    async def _fake_exec(*argv: str, **_kwargs: object) -> _FakeProcess:
        calls.append(list(argv))
        proc.open_streams()
        return proc

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(
        module, "_maybe_jail_argv", lambda argv, *, runtime, cwd, session="", sink=None: argv
    )
    monkeypatch.setattr(module, "build_child_env", lambda *_a, **_k: {"PATH": "/usr/bin"})
    return calls


def _spawn_each_vendor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    denied_tools: list[str] = (),  # type: ignore[assignment]
) -> dict[str, tuple[SpawnResult, list[SandboxEnforcementEvent], list[list[str]]]]:
    """Spawn one equivalent call on each vendor, returning per-vendor outcomes.

    Each spawn is driven through the SAME vendor-neutral seam
    (``spawn_session(prompt, model=..., denied_tools=..., session=...,
    enforcement_sink=...)``) with a fake subprocess emitting that vendor's
    well-formed envelope; the returned map carries the parsed
    :class:`SpawnResult`, the recorded enforcement events, and the captured
    argv for each vendor.
    """
    outcomes: dict[str, tuple[SpawnResult, list[SandboxEnforcementEvent], list[list[str]]]] = {}
    vendors: list[tuple[str, Any, Any, bytes]] = [
        ("claude-code", claude_adapter, ClaudeAdapter(), _claude_stdout()),
        ("codex", codex_adapter, CodexAdapter(), _codex_stdout()),
        ("opencode", opencode_adapter, OpenCodeAdapter(), _opencode_stdout()),
    ]
    for key, module, adapter, stdout in vendors:
        proc = _FakeProcess(stdout=stdout, stderr=b"", returncode=0)
        calls = _patch_vendor(monkeypatch, module, proc)
        recorded: list[SandboxEnforcementEvent] = []
        result = asyncio.run(
            adapter.spawn_session(
                "solve it",
                model="m",
                denied_tools=list(denied_tools),
                session=_SESSION_ID,
                enforcement_sink=recorded.append,
            )
        )
        outcomes[key] = (result, recorded, calls)
    return outcomes


# ---------------------------------------------------------------------------
# C1: identical metered / priced / sandboxed SpawnResult SHAPE across vendors
# ---------------------------------------------------------------------------


def test_three_vendors_emit_identical_spawn_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three adapters emit a SpawnResult with the same field set + types."""
    outcomes = _spawn_each_vendor(monkeypatch)

    results = {key: outcome[0] for key, outcome in outcomes.items()}
    # Every vendor returns the same typed model.
    for result in results.values():
        assert isinstance(result, SpawnResult)

    # The field SET is identical across the three vendors (shape parity).
    field_sets = {key: set(result.model_dump().keys()) for key, result in results.items()}
    assert field_sets["claude-code"] == field_sets["codex"] == field_sets["opencode"]
    # The expected metering + sandbox field names are present on each.
    expected_fields = {
        "session_id",
        "runtime",
        "model",
        "resolved_model",
        "subprocess_pid",
        "exit_status",
        "text",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_read_input_tokens",
        "cost_usd_reported",
        "measurement_quality",
        "measurement_status",
        "measurement_reason",
        "started_at",
        "ended_at",
    }
    assert field_sets["claude-code"] == expected_fields


def test_three_vendors_metering_fields_match_for_equivalent_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metering fields carry equivalent values for an equivalent spawn.

    The three well-formed envelopes encode the same per-call usage (100 input /
    42 output / 200 cache-read), so the metered SpawnResult fields match across
    vendors -- proving the parse normalises to one metering shape.
    """
    outcomes = _spawn_each_vendor(monkeypatch)
    results = {key: outcome[0] for key, outcome in outcomes.items()}

    # input / output / cache-read are reported identically by all three
    # runtimes (the parse normalises each vendor's envelope to one metering
    # shape -- including codex's gross-input split).
    assert {r.input_tokens for r in results.values()} == {100}
    assert {r.output_tokens for r in results.values()} == {42}
    assert {r.cache_read_input_tokens for r in results.values()} == {200}
    # The answer text is the same across vendors (one normalised text field).
    assert {r.text for r in results.values()} == {"the answer text"}
    # Each vendor stamps its own runtime id (the one field that legitimately
    # differs -- it is the vendor identifier).
    assert {r.runtime for r in results.values()} == {"claude-code", "codex", "opencode"}


def test_three_vendors_record_same_sandbox_enforcement_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each vendor records the SAME sandbox-enforcement kinds for the spawn.

    With a denied tool wired, every vendor records both an ``env-scrub`` (the
    floor ran) and an ``argv-deny`` (the deny was honoured) event -- proving
    the sandbox-enforcement seam is at parity across the three lanes.
    """
    outcomes = _spawn_each_vendor(monkeypatch, denied_tools=_DENIED)

    for key, (_result, recorded, _calls) in outcomes.items():
        kinds = {event.kind for event in recorded}
        assert "env-scrub" in kinds, f"{key} did not record env-scrub"
        assert "argv-deny" in kinds, f"{key} did not record argv-deny"
        # Every recorded event is stamped with the spawning session.
        assert all(event.session == _SESSION_ID for event in recorded)


# ---------------------------------------------------------------------------
# C2: codex + opencode honour a denied tool (B081)
# ---------------------------------------------------------------------------


def _codex_tool_grants(argv: list[str]) -> dict[str, bool]:
    """Parse the ``-c tools.<name>=<bool>`` grant map from a codex argv."""
    grants: dict[str, bool] = {}
    for index, token in enumerate(argv):
        if token != "-c" or index + 1 >= len(argv):
            continue
        key_value = argv[index + 1]
        if not key_value.startswith("tools."):
            continue
        key, _, value = key_value.partition("=")
        grants[key.removeprefix("tools.")] = value.strip().lower() == "true"
    return grants


def test_codex_denied_tool_absent_from_effective_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A codex-denied tool is absent from the ``true`` grant + pinned ``false``."""
    proc = _FakeProcess(stdout=_codex_stdout(), stderr=b"", returncode=0)
    calls = _patch_vendor(monkeypatch, codex_adapter, proc)
    recorded: list[SandboxEnforcementEvent] = []

    asyncio.run(
        CodexAdapter().spawn_session(
            "x",
            model="m",
            denied_tools=["Edit"],
            session=_SESSION_ID,
            enforcement_sink=recorded.append,
        )
    )
    grants = _codex_tool_grants(calls[0])

    # The denied tool can never reach the child's effective grant: absent from
    # the ``true`` set and pinned ``false``.
    granted_true = {tool for tool, granted in grants.items() if granted}
    assert "edit" not in granted_true
    assert grants.get("edit") is False
    # The deny is recorded on the denial timeline (auditable).
    deny_events = [e for e in recorded if e.kind == "argv-deny"]
    assert len(deny_events) == 1
    assert deny_events[0].target == "Edit"
    assert deny_events[0].severity == "block"


def test_opencode_denied_tool_never_reaches_positive_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An opencode deny is jail-backed: the tool never reaches a positive grant.

    opencode has no per-call deny flag, so its deny is honoured by the FS-jail
    floor (which this test neutralises) and recorded as a ``warn`` argv-deny.
    The load-bearing parity check: the denied tool name never appears as a
    positive argv grant, so a deny cannot silently pass through.
    """
    proc = _FakeProcess(stdout=_opencode_stdout(), stderr=b"", returncode=0)
    calls = _patch_vendor(monkeypatch, opencode_adapter, proc)
    recorded: list[SandboxEnforcementEvent] = []

    asyncio.run(
        OpenCodeAdapter().spawn_session(
            "x",
            model="m",
            denied_tools=["Edit", "Bash"],
            session=_SESSION_ID,
            enforcement_sink=recorded.append,
        )
    )

    argv = calls[0]
    # No fictional allow/deny flag: the denied tool names never appear on argv.
    assert not any(tool in argv for tool in ("Edit", "Bash"))
    assert "--disallowedTools" not in argv
    # The deny is recorded (auditable) at warn severity (jail-backed, not a hard
    # argv-deny like claude / codex).
    deny_events = [e for e in recorded if e.kind == "argv-deny"]
    assert len(deny_events) == 1
    assert deny_events[0].target == "Bash Edit"
    assert deny_events[0].severity == "warn"


def test_empty_deny_records_no_argv_deny_on_codex_or_opencode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty deny-list records no argv-deny on either codex or opencode."""
    for module, adapter, stdout in (
        (codex_adapter, CodexAdapter(), _codex_stdout()),
        (opencode_adapter, OpenCodeAdapter(), _opencode_stdout()),
    ):
        proc = _FakeProcess(stdout=stdout, stderr=b"", returncode=0)
        _patch_vendor(monkeypatch, module, proc)
        recorded: list[SandboxEnforcementEvent] = []
        asyncio.run(
            adapter.spawn_session(
                "x",
                model="m",
                denied_tools=(),
                session=_SESSION_ID,
                enforcement_sink=recorded.append,
            )
        )
        assert not any(e.kind == "argv-deny" for e in recorded)


# ---------------------------------------------------------------------------
# Edge journeys: cancel / overrun (timeout), fallback (nonzero exit), pgid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "adapter_factory", "stdout"),
    [
        (codex_adapter, CodexAdapter, _codex_stdout()),
        (opencode_adapter, OpenCodeAdapter, _opencode_stdout()),
    ],
)
def test_overrun_timeout_raises_and_kills_child(
    monkeypatch: pytest.MonkeyPatch, module: Any, adapter_factory: Any, stdout: bytes
) -> None:
    """A wall-clock overrun kills the child and raises a typed timeout error.

    The cancel / overrun edge journey is at parity across vendors: a hung child
    past the timeout is killed and surfaces as a typed
    :class:`RuntimeSpawnError` with the ``timed out`` message.
    """
    proc = _FakeProcess(stdout=stdout, stderr=b"", returncode=0, hang=True)
    _patch_vendor(monkeypatch, module, proc)

    with pytest.raises(RuntimeSpawnError, match="timed out"):
        asyncio.run(
            adapter_factory().spawn_session("x", model="m", timeout=0.01, session=_SESSION_ID)
        )
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.parametrize(
    ("module", "adapter_factory"),
    [
        (codex_adapter, CodexAdapter),
        (opencode_adapter, OpenCodeAdapter),
    ],
)
def test_fallback_nonzero_exit_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch, module: Any, adapter_factory: Any
) -> None:
    """A non-zero exit surfaces as a typed error (the fallback switch signal).

    The fallback edge journey: a runtime that exits non-zero raises
    :class:`RuntimeSpawnError` carrying the exit status so the V5 reactive
    switch ladder can classify + fall over to another runtime.
    """
    proc = _FakeProcess(stdout=b"", stderr=b"500 internal_server_error", returncode=1)
    _patch_vendor(monkeypatch, module, proc)

    with pytest.raises(RuntimeSpawnError, match="exited nonzero"):
        asyncio.run(adapter_factory().spawn_session("x", model="m", session=_SESSION_ID))


@pytest.mark.parametrize(
    ("module", "adapter_factory", "stdout"),
    [
        (codex_adapter, CodexAdapter, _codex_stdout()),
        (opencode_adapter, OpenCodeAdapter, _opencode_stdout()),
    ],
)
def test_on_pgid_fires_with_child_group_for_budget_halt(
    monkeypatch: pytest.MonkeyPatch, module: Any, adapter_factory: Any, stdout: bytes
) -> None:
    """on_pgid fires with the child's process group at parity with claude.

    The budget-HALT interlock needs the child's pgid to ``killpg`` the whole
    group on a hard cap breach; this seam fires the callback right after spawn.
    ``os.getpgid`` is faked so no real process is inspected.
    """
    proc = _FakeProcess(stdout=stdout, stderr=b"", returncode=0)
    _patch_vendor(monkeypatch, module, proc)
    monkeypatch.setattr(module.os, "getpgid", lambda pid: pid + 1000)
    seen: list[int] = []

    asyncio.run(
        adapter_factory().spawn_session("x", model="m", session=_SESSION_ID, on_pgid=seen.append)
    )
    # The resolved group (faked pid+1000 for pid 4321) reached the callback.
    assert seen == [5321]


@pytest.mark.parametrize(
    ("module", "cap_error"),
    [
        (codex_adapter, CodexConcurrentSpawnCapError),
        (opencode_adapter, OpenCodeConcurrentSpawnCapError),
    ],
)
def test_concurrent_spawn_cap_fails_fast_at_ceiling(
    monkeypatch: pytest.MonkeyPatch, module: Any, cap_error: type[Exception]
) -> None:
    """A spawn past the concurrent cap fails fast before any subprocess forks.

    The fleet-overrun guard is at parity with claude: saturating the in-flight
    counter at the cap makes the next acquire raise rather than fork an
    unbounded fleet.
    """
    monkeypatch.setattr(module, "_spawn_inflight", module._CONCURRENT_SPAWN_CAP)
    adapter = CodexAdapter() if module is codex_adapter else OpenCodeAdapter()

    with pytest.raises(cap_error, match="concurrent spawn cap"):
        asyncio.run(adapter.spawn_session("x", model="m", session=_SESSION_ID))


# ---------------------------------------------------------------------------
# Edge journey: egress-block is recorded at parity on every vendor lane
# ---------------------------------------------------------------------------


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Yield a short-path 0700 dir for AF_UNIX binds (macOS sun_path cap)."""
    import shutil

    socket_dir = Path(tempfile.mkdtemp(prefix="egr", dir="/tmp"))
    socket_dir.chmod(0o700)
    try:
        yield socket_dir
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
@pytest.mark.parametrize("lane", ["claude", "codex"])
def test_egress_block_recorded_per_lane(short_socket_dir: Path, lane: str) -> None:
    """A denied host is refused + recorded for every auth lane (egress parity).

    The egress proxy is the shared enforcement seam each vendor's jailed child
    routes outbound through; a denied host is refused (never opens outbound) and
    recorded as an ``egress-block`` event regardless of the auth lane.
    """
    socket_path = short_socket_dir / "egress.sock"
    recorded: list[SandboxEnforcementEvent] = []

    async def _connect(host: str, port: int) -> Any:  # pragma: no cover - never called
        raise AssertionError("denied host must never reach the network")

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=lane,
            connector=_connect,
            session=_SESSION_ID,
            sink=recorded.append,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b"CONNECT evil.test:443\n")
            await writer.drain()
            status = await reader.readline()
            writer.close()
            return status
        finally:
            server.close()
            await server.wait_closed()

    status = asyncio.run(_run())

    assert status.startswith(b"DENY")
    assert len(recorded) == 1
    event = recorded[0]
    assert event.kind == "egress-block"
    assert event.target == "evil.test:443"
    assert event.session == _SESSION_ID
    assert event.severity == "block"


# ---------------------------------------------------------------------------
# Sanity: the claude lane (W04) is at parity with the two lanes this wave wires
# ---------------------------------------------------------------------------


def test_claude_records_same_enforcement_seam_for_parity_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude (W04) records env-scrub + argv-deny -- the parity baseline.

    Read-only over the claude lane: proves the seam this wave brings codex +
    opencode to is the SAME seam claude already ships, so the cross-vendor
    parity assertion is anchored to a real baseline, not an invented one.
    """
    proc = _FakeProcess(stdout=_claude_stdout(), stderr=b"", returncode=0)
    _patch_vendor(monkeypatch, claude_adapter, proc)
    recorded: list[SandboxEnforcementEvent] = []

    asyncio.run(
        ClaudeAdapter().spawn_session(
            "x",
            model="m",
            denied_tools=_DENIED,
            session=_SESSION_ID,
            enforcement_sink=recorded.append,
        )
    )

    kinds = {event.kind for event in recorded}
    assert "env-scrub" in kinds
    assert "argv-deny" in kinds
