"""Integration: spawn-safety wiring (P30-I10-W04).

Pins the three seams this wave wired together so the safety floor is no
longer idle:

C1 -- a spawned jailed child's outbound is routed through the egress
proxy (a denied host is refused and recorded), AND a hard token-cap
breach terminates the live wave through the *threaded pgid*
(``terminated=True``), proven by driving
:func:`eawf.runtime.daemon.dispatch_runner.accrue_tokens_consumed` with a
real pgid + ``enforce="hard"`` and an injected kill ladder.

C2 -- each sandbox enforcement event (argv-deny / egress-block /
env-scrub / cwd-guard) is persisted to the event feed with the five named
fields (``ts``, ``session``, ``kind``, ``target``, ``severity``) so a TUI
denial-timeline surface can read the denial sequence off the feed.

Boundary + error paths: the concurrent-spawn cap fails fast at the
ceiling; the env-scrub recorder names how many cred vars were dropped.

Every test drives async coroutines via ``asyncio.run`` inside plain sync
``def test_`` bodies (the suite has no ``pytest-asyncio`` dep) and never
touches the real network -- the outbound connector is always a fake.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.runtime.budget.policy import classify_enforcement
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon import dispatch_runner
from eawf.runtime.daemon.budget_interlock import InterlockOutcome, enforce_token_cap
from eawf.runtime.daemon.dispatch_runner import (
    DispatchTokens,
    accrue_tokens_consumed,
    enforcement_sink,
    persist_enforcement_event,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import (
    ClaudeAdapter,
    ConcurrentSpawnCapError,
)
from eawf.runtime.sandbox import egress_proxy
from eawf.runtime.sandbox.egress_proxy import (
    SandboxEnforcementEvent,
    make_enforcement_event,
    start_egress_proxy,
)

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I10-W04"
_SESSION_ID = "SES-executor"
_CLAUDE = "claude"


# ---------------------------------------------------------------------------
# State + ctx fixtures (mirrors test_dispatch_runner_budget_halt)
# ---------------------------------------------------------------------------


def _state_payload(*, token_budget: int | None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-10T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I10",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [_SESSION_ID],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I10"],
                "outcome_ids": [],
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I10": {
                "id": "P30-I10",
                "phase_id": "P30",
                "title": "Substrate",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I10",
                "title": "Spawn-safety wiring",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/runtime/sandbox/egress_proxy.py"],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": _SESSION_ID,
                "worktree_id": None,
                "token_budget": token_budget,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-10T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            _SESSION_ID: {
                "id": _SESSION_ID,
                "role": "executor",
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-06-10T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, token_budget: int | None) -> Path:
    state = State.model_validate(_state_payload(token_budget=token_budget))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl"
    return MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        bus=None,
        event_path=event_path,
        state_path=state_path,
    )


def _tokens(total: int) -> DispatchTokens:
    return DispatchTokens(
        input_tokens=total,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _read_events(event_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in event_path.read_text().splitlines() if line.strip()]


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """Yield a short-path 0700 dir for AF_UNIX binds (macOS sun_path cap)."""
    socket_dir = Path(tempfile.mkdtemp(prefix="egr", dir="/tmp"))
    socket_dir.chmod(0o700)
    try:
        yield socket_dir
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# C1a: a jailed child's outbound routes through the egress proxy
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
def test_jailed_child_egress_routed_through_proxy_records_block(
    short_socket_dir: Path,
) -> None:
    """A child's denied host is refused at the proxy + recorded for the session.

    Binds a real UDS egress proxy wired with the spawning session id + an
    enforcement sink, connects a real client (the jailed child's outbound
    seam), requests a DENIED host, and asserts the proxy replied ``DENY``,
    never opened outbound, and recorded one ``egress-block`` event naming
    the refused host for the session.
    """
    socket_path = short_socket_dir / "egress.sock"
    recorded: list[SandboxEnforcementEvent] = []
    outbound_calls: list[tuple[str, int]] = []

    async def _connect(host: str, port: int) -> Any:  # pragma: no cover - never called
        outbound_calls.append((host, port))
        raise AssertionError("denied host must never reach the network")

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=_CLAUDE,
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
    assert outbound_calls == []  # denied host never reached the network
    assert len(recorded) == 1
    event = recorded[0]
    assert event.kind == "egress-block"
    assert event.session == _SESSION_ID
    assert event.target == "evil.test:443"
    assert event.severity == "block"


@pytest.mark.skipif(sys.platform == "win32", reason="UDS proxy is POSIX-only")
def test_jailed_child_egress_allows_listed_host_no_block_recorded(
    short_socket_dir: Path,
) -> None:
    """An allowed host tunnels and records NO egress-block event."""
    socket_path = short_socket_dir / "egress.sock"
    recorded: list[SandboxEnforcementEvent] = []
    calls: list[tuple[str, int]] = []

    async def _connect(host: str, port: int) -> Any:
        calls.append((host, port))

        class _R:
            async def read(self, _n: int = -1) -> bytes:
                return b""

        class _W:
            def write(self, _data: bytes) -> None:
                return None

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

        return _R(), _W()

    async def _run() -> bytes:
        server = await start_egress_proxy(
            socket_path,
            lane=_CLAUDE,
            connector=_connect,
            session=_SESSION_ID,
            sink=recorded.append,
        )
        try:
            reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
            writer.write(b"CONNECT api.anthropic.com:443\n")
            await writer.drain()
            status = await reader.readline()
            writer.close()
            return status
        finally:
            server.close()
            await server.wait_closed()

    status = asyncio.run(_run())

    assert status.startswith(b"OK")
    assert calls == [("api.anthropic.com", 443)]
    assert recorded == []  # an allowed host is not a denial


# ---------------------------------------------------------------------------
# C1b: a hard token-cap breach terminates the live wave via the threaded pgid
# ---------------------------------------------------------------------------


def test_hard_cap_breach_terminates_via_threaded_pgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-enforce cap breach reaps the threaded pgid (terminated=True).

    Drives the accrual with a real pgid + ``enforce="hard"`` over a budget
    the burn crosses. The kill ladder is injected so no real signal is
    delivered; the load-bearing assertion is that the THREADED pgid reached
    the ladder and the interlock reports ``terminated=True``.
    """
    state_path = _write_state(tmp_path, token_budget=1000)
    ctx = _ctx(state_path)
    captured_pgid: list[int] = []

    def _fake_cancel(pgid: int) -> TerminationResult:
        captured_pgid.append(pgid)
        return TerminationResult(
            sigterm_sent=True,
            sigkill_sent=False,
            exited_on_term=True,
            waited_seconds=0.0,
        )

    def _enforce_with_fake_cancel(**kwargs: Any) -> InterlockOutcome:
        # Drive the REAL interlock so the real classifier produces the HALT
        # verdict; only the kill ladder is faked (no live signal).
        return enforce_token_cap(**kwargs, cancel=_fake_cancel)

    monkeypatch.setattr(dispatch_runner, "enforce_token_cap", _enforce_with_fake_cancel)

    outcome = accrue_tokens_consumed(
        ctx, wave_id=_WAVE_ID, tokens=_tokens(2000), pgid=4242, enforce="hard"
    )

    assert outcome is not None
    assert outcome.terminated is True
    assert outcome.decision.over_cap is True
    # The THREADED pgid is the one the ladder reaped.
    assert captured_pgid == [4242]


def test_soft_cap_breach_does_not_terminate_even_with_pgid(
    tmp_path: Path,
) -> None:
    """Under soft enforce a cap breach never reaps the group (terminated=False)."""
    state_path = _write_state(tmp_path, token_budget=1000)
    ctx = _ctx(state_path)

    outcome = accrue_tokens_consumed(
        ctx, wave_id=_WAVE_ID, tokens=_tokens(5000), pgid=4242, enforce="soft"
    )

    assert outcome is not None
    assert outcome.terminated is False


def test_hard_cap_breach_with_no_pgid_logs_but_does_not_terminate(
    tmp_path: Path,
) -> None:
    """A hard breach with no addressable pgid computes the HALT but signals nothing."""
    state_path = _write_state(tmp_path, token_budget=1000)
    ctx = _ctx(state_path)

    outcome = accrue_tokens_consumed(
        ctx, wave_id=_WAVE_ID, tokens=_tokens(2000), pgid=None, enforce="hard"
    )

    assert outcome is not None
    assert outcome.terminated is False  # no addressable group to reap
    assert outcome.decision.over_cap is True


# ---------------------------------------------------------------------------
# C2: every enforcement event is persisted to the feed with the 5 named fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "target", "severity"),
    [
        ("argv-deny", "Bash WebFetch", "block"),
        ("egress-block", "evil.test:443", "block"),
        ("env-scrub", "dropped=7", "block"),
        ("cwd-guard", "/outside/repo", "warn"),
    ],
)
def test_enforcement_event_persisted_with_five_fields(
    tmp_path: Path, kind: str, target: str, severity: str
) -> None:
    """Each enforcement kind lands on the feed with ts/session/kind/target/severity."""
    state_path = _write_state(tmp_path, token_budget=None)
    ctx = _ctx(state_path)
    event = make_enforcement_event(
        session=_SESSION_ID,
        kind=kind,  # type: ignore[arg-type]
        target=target,
        severity=severity,  # type: ignore[arg-type]
    )

    envelope_id = persist_enforcement_event(ctx, event)

    rows = _read_events(ctx.event_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == envelope_id
    assert row["kind"] == "event"
    assert row["scope_id"] == _SESSION_ID
    extras = row["payload"]["extras"]
    # The five named C2 fields ride the payload extras for the TUI timeline.
    assert set(extras) >= {"ts", "session", "kind", "target", "severity"}
    assert extras["session"] == _SESSION_ID
    assert extras["kind"] == kind
    assert extras["target"] == target
    assert extras["severity"] == severity
    # ts round-trips as an ISO-8601 string.
    assert extras["ts"] == event.ts.isoformat()


def test_enforcement_sink_persists_each_event_through_ctx(tmp_path: Path) -> None:
    """The ctx-bound sink persists every event a sandbox boundary hands it."""
    state_path = _write_state(tmp_path, token_budget=None)
    ctx = _ctx(state_path)
    sink = enforcement_sink(ctx)

    for kind, target in (
        ("argv-deny", "Bash"),
        ("egress-block", "evil.test:443"),
        ("env-scrub", "dropped=3"),
        ("cwd-guard", "/outside"),
    ):
        sink(
            make_enforcement_event(
                session=_SESSION_ID,
                kind=kind,  # type: ignore[arg-type]
                target=target,
            )
        )

    rows = _read_events(ctx.event_path)
    kinds = [row["payload"]["extras"]["kind"] for row in rows]
    assert kinds == ["argv-deny", "egress-block", "env-scrub", "cwd-guard"]


def test_persist_enforcement_event_without_event_path_raises(tmp_path: Path) -> None:
    """A ctx with no event_path fails fast rather than silently dropping the row."""
    ctx = MethodContext(
        started_at="2026-06-10T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="0.6.0",
        bus=None,
        event_path=None,
        state_path=None,
    )
    event = make_enforcement_event(session=_SESSION_ID, kind="argv-deny", target="Bash")

    with pytest.raises(RuntimeError, match="event_path not configured"):
        persist_enforcement_event(ctx, event)


# ---------------------------------------------------------------------------
# Boundary + error: the concurrent-spawn cap + the env-scrub recorder
# ---------------------------------------------------------------------------


def test_concurrent_spawn_cap_fails_fast_at_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn past the concurrent cap raises before any subprocess is forked."""
    # Saturate the in-flight counter at the cap so the next acquire fails.
    monkeypatch.setattr(
        claude_adapter, "_spawn_inflight", claude_adapter._CONCURRENT_SPAWN_CAP
    )

    async def _spawn() -> Any:
        return await ClaudeAdapter().spawn_session(
            "hi",
            model="x",
            session=_SESSION_ID,
        )

    with pytest.raises(ConcurrentSpawnCapError, match="concurrent spawn cap"):
        asyncio.run(_spawn())


def test_env_scrub_recorder_names_dropped_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env-scrub recorder reports how many cred vars the allowlist dropped."""
    recorded: list[SandboxEnforcementEvent] = []
    # A parent env with cred-bearing vars the claude allowlist drops.
    monkeypatch.setattr(
        claude_adapter.os,
        "environ",
        {"HOME": "/h", "AWS_SECRET_ACCESS_KEY": "x", "GH_TOKEN": "y"},
    )

    ClaudeAdapter._record_env_scrub(
        {"HOME": "/h", "PATH": "/usr/bin", "LANG": "C.UTF-8"},
        session=_SESSION_ID,
        sink=recorded.append,
    )

    assert len(recorded) == 1
    event = recorded[0]
    assert event.kind == "env-scrub"
    assert event.session == _SESSION_ID
    assert event.severity == "block"  # cred vars were withheld


def test_env_scrub_recorder_info_severity_when_nothing_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scrub that drops nothing is an info-severity row (the floor still ran)."""
    recorded: list[SandboxEnforcementEvent] = []
    monkeypatch.setattr(claude_adapter.os, "environ", {"HOME": "/h"})

    ClaudeAdapter._record_env_scrub(
        {"HOME": "/h", "PATH": "/usr/bin", "LANG": "C.UTF-8", "EXTRA": "z"},
        session=_SESSION_ID,
        sink=recorded.append,
    )

    assert recorded[0].severity == "info"


# ---------------------------------------------------------------------------
# Model error paths: the enforcement event rejects unknown kinds / fields
# ---------------------------------------------------------------------------


def test_enforcement_event_rejects_unknown_kind() -> None:
    """An out-of-enum kind fails validation at the boundary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SandboxEnforcementEvent(
            ts=make_enforcement_event(
                session="s", kind="argv-deny", target="x"
            ).ts,
            session="s",
            kind="not-a-real-kind",  # type: ignore[arg-type]
            target="x",
        )


def test_enforcement_event_forbids_extra_field() -> None:
    """An unknown field is rejected (extra='forbid')."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SandboxEnforcementEvent.model_validate(
            {
                "ts": "2026-06-10T00:00:00Z",
                "session": "s",
                "kind": "argv-deny",
                "target": "x",
                "severity": "block",
                "surprise": 1,
            }
        )


def test_classify_enforcement_hard_halt_threads_into_outcome() -> None:
    """Sanity: the real classifier HALTs under hard enforce at the cap."""
    decision = classify_enforcement(2000, 1000, enforce="hard", multiplier=1.5)
    outcome = enforce_token_cap(
        consumed=2000,
        base_budget=1000,
        enforce="hard",
        multiplier=1.5,
        pgid=999,
        cancel=lambda _pgid: TerminationResult(
            sigterm_sent=True, sigkill_sent=False, exited_on_term=True, waited_seconds=0.0
        ),
    )
    assert decision.over_cap is True
    assert outcome.terminated is True


def test_egress_socket_env_routes_child_outbound() -> None:
    """The egress socket env overlay names the UDS the child reaches outbound."""
    env = egress_proxy.egress_socket_env(Path("/run/eawf/egress.sock"))
    assert env["EAWF_EGRESS_SOCKET"] == "/run/eawf/egress.sock"
    assert env["ALL_PROXY"] == "unix:///run/eawf/egress.sock"
