"""Integration: spawn-safety wiring.

Pins the seams that keep the safety floor from going idle: a spawned
jailed child's outbound is routed through the egress proxy (a denied host
is refused and recorded), and a sandbox enforcement event carries the
five named fields (``ts``, ``session``, ``kind``, ``target``,
``severity``) and rejects any other shape.

Boundary + error paths: the concurrent-spawn cap fails fast at the
ceiling; the env-scrub recorder names how many cred vars were dropped.

Every test drives async coroutines via ``asyncio.run`` inside plain sync
``def test_`` bodies (the suite has no ``pytest-asyncio`` dep) and never
touches the real network -- the outbound connector is always a fake.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.budget.policy import classify_enforcement
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.daemon.budget_interlock import enforce_token_cap
from eawf.runtime.runtimes import adapter as shared_adapter
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

_SESSION_ID = "SES-executor"
_CLAUDE = "claude"


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
# Boundary + error: the concurrent-spawn cap + the env-scrub recorder
# ---------------------------------------------------------------------------


def test_concurrent_spawn_cap_fails_fast_at_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch past the concurrent cap is refused before any subprocess is forked."""
    # Saturate the shared in-flight counter at the one ceiling so the next
    # acquire fails. The counter lives on the shared adapter module: the cap is
    # process-wide, not per-vendor.
    monkeypatch.setattr(shared_adapter, "_spawn_inflight", shared_adapter.CONCURRENT_SPAWN_CAP)

    with pytest.raises(ConcurrentSpawnCapError, match="concurrent spawn cap"):
        shared_adapter.acquire_spawn_slot()


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
            ts=make_enforcement_event(session="s", kind="argv-deny", target="x").ts,
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
