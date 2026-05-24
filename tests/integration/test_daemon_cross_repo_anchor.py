"""Cross-repo anchor regression for daemon ``state.*`` + ``config.*`` RPCs.

The daemon is one per user (one UDS / named pipe) and serves many repos.
Before P26-W03 the ``state.*`` and ``config.*`` RPCs resolved every
path-join against the **boot-time** ``ctx.state_path``, which was set
once at daemon start via :func:`eawf.kernel.state.resolve.resolve_with_reason`.
A cross-repo invocation from a different cwd was mis-routed against the
daemon's own anchor — worse, if the daemon happened to be launched
from a cwd where the upward ``.ea/state.json`` walk fell off the root
(e.g. the parent worktree being a transient detach), the fallback path
``Path.cwd() / .ea / state.json`` became ``/.ea/state.json``, which on
macOS produces ``[Errno 30] Read-only file system: '/.ea'`` the moment
the daemon tries to write.

P26-W03 adds a per-request ``repo_root`` param to every ``state.*`` /
``config.*`` RPC. Callers (CLI proxy + daemon-client convenience
methods) pass the caller's repo root explicitly so the daemon resolves
the correct anchor regardless of boot-time cwd. Omitting the param
falls back to ``ctx.state_path`` with a one-shot
``daemon_anchor_fallback`` deprecation warning so stale clients surface
in the daemon log without breaking CI.

This module exercises three scenarios end-to-end against a real
:func:`eawf.daemon.server.serve_unix` listener on a per-test UDS:

(a) The daemon, booted with ``ctx.state_path = <repoA>/.ea/state.json``,
    receives a ``state.read`` call carrying ``repo_root=<repoB>`` and
    returns repoB's project payload (NOT repoA's).
(b) Same daemon, ``config.read`` with ``repo_root=<repoB>``, returns
    the layer_path under repoB.
(c) Same daemon, ``state.read`` WITHOUT ``repo_root`` — returns repoA's
    payload AND emits the one-shot ``daemon_anchor_fallback`` warning.
    A second omission does NOT re-emit (one-shot semantics).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.cli._daemon_client import DaemonClient
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods import state as state_methods
from eawf.daemon.server import serve_unix
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX-only client transport; daemon Windows-pipe is a separate harness",
)


def _short_runtime_dir() -> Path:
    """Return a per-test runtime dir short enough for AF_UNIX (104-byte cap)."""
    base = Path(tempfile.gettempdir())
    return base / f"eawfd-{uuid.uuid4().hex[:8]}"


def _build_state_payload(*, code: str) -> dict[str, Any]:
    """Construct a minimal valid State payload for the named project code."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": f"urn:eawf:v1:state:{code}",
        "updated_at": "2026-05-19T00:00:00+00:00",
        "project": {
            "code": code,
            "slug": code.lower(),
            "title": code,
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": f"urn:eawf:v1:repo:{code}",
        },
        "current": {"project_code": code},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state_for(repo: Path, *, code: str) -> Path:
    """Write a minimal valid ``state.json`` under ``<repo>/.ea/``."""
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _build_state_payload(code=code)
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return state_path


class _ServerHandle:
    """Async daemon harness — boots :func:`serve_unix` on a worker thread.

    The harness wires a real :class:`MethodContext` with ``state_path``,
    ``event_path``, and ``wal_dir`` configured against *boot_repo* so the
    legacy-fallback assertions in this module reflect the real
    daemon-with-stale-anchor scenario.
    """

    def __init__(self, runtime_dir: Path, *, boot_repo: Path) -> None:
        self.runtime_dir = runtime_dir
        self.sock_path = runtime_dir / "eawfd.sock"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.boot_state_path = _write_state_for(boot_repo, code="REPOA")
        self.boot_event_path = store_path(self.boot_state_path, StoreKind.EVENT)
        self.boot_wal_dir = runtime_dir / "wal"
        self.boot_wal_dir.mkdir(parents=True, exist_ok=True)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._pid_file = runtime_dir / "eawfd.pid"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=5.0), "server failed to start within 5 s"
        self._pid_file.write_text(
            f"{os.getpid()}\n{PROTOCOL_VERSION}\n2026-05-19T00:00:00+00:00\n",
            encoding="utf-8",
        )

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with contextlib.suppress(FileNotFoundError):
            self.sock_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._pid_file.unlink()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        ctx = MethodContext(
            started_at="2026-05-19T00:00:00+00:00",
            pid=os.getpid(),
            protocol_version=PROTOCOL_VERSION,
            version=__version__,
            shutdown_event=asyncio.Event(),
            bus=EventBus(),
            event_path=self.boot_event_path,
            state_path=self.boot_state_path,
            wal_dir=self.boot_wal_dir,
            idempotency_cache={},
        )

        async def _start() -> None:
            self._server = await serve_unix(str(self.sock_path), ctx, expected_uid=None)
            self._ready.set()

        loop.run_until_complete(_start())
        try:
            loop.run_forever()
        finally:
            if self._server is not None:
                self._server.close()
                loop.run_until_complete(self._server.wait_closed())
            loop.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[tuple[_ServerHandle, Path, Path]]:
    """Yield ``(server, repo_a, repo_b)`` for the cross-repo tests.

    Two repos are pre-populated with distinct ``state.json`` files:
    repoA's project code is ``REPOA`` (the daemon's boot anchor),
    repoB's is ``REPOB``. Every test that wants to exercise the
    per-request override targets repoB.
    """
    # Each integration test under tests/integration/ runs with
    # EAWF_DAEMONLESS=1 (per conftest), but this module spins its own
    # daemon and drives DaemonClient directly so the env var is
    # irrelevant — the client connects via the explicit ``runtime_dir``
    # arg below, not the auto-spawn ladder.
    repo_a = tmp_path / "repoA"
    repo_b = tmp_path / "repoB"
    _write_state_for(repo_b, code="REPOB")
    server = _ServerHandle(_short_runtime_dir(), boot_repo=repo_a)
    server.start()
    try:
        yield server, repo_a, repo_b
    finally:
        server.stop()


# ---- (a) state.read honours per-request repo_root --------------------------


def test_state_read_with_repo_root_resolves_target_repo(
    harness: tuple[_ServerHandle, Path, Path],
) -> None:
    """Daemon booted against repoA returns repoB when caller passes repo_root."""
    server, _repo_a, repo_b = harness
    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.call("state.read", {"repo_root": str(repo_b)})
    assert result["state"]["project"]["code"] == "REPOB"


# ---- (b) config.read honours per-request repo_root -------------------------


def test_config_read_with_repo_root_resolves_target_repo(
    harness: tuple[_ServerHandle, Path, Path],
) -> None:
    """config.read with repo_root returns the layer path under that repo."""
    server, _repo_a, repo_b = harness
    # Seed a config.yaml so the daemon has something to parse.
    cfg = repo_b / ".ea" / "config.yaml"
    cfg.write_text("vcs:\n  auto_commit: true\n")

    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        result = client.call("config.read", {"layer": "repo", "repo_root": str(repo_b)})

    # layer_path must point under repoB (NOT repoA / the daemon's boot anchor).
    assert result["layer_path"].startswith(str(repo_b)), (
        f"layer_path {result['layer_path']!r} not anchored under {repo_b}"
    )
    assert result["config"] == {"vcs": {"auto_commit": True}}


# ---- (c) Omitting repo_root falls back + emits one-shot warning -----------


def test_state_read_without_repo_root_falls_back_and_warns_once(
    harness: tuple[_ServerHandle, Path, Path],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller omitting repo_root resolves via ctx + emits one warning."""
    server, _repo_a, _repo_b = harness
    # Reset the module-level one-shot flag for an isolated assertion;
    # other tests in the suite may have flipped it already.
    monkeypatch.setattr(state_methods, "_ANCHOR_FALLBACK_WARN_EMITTED", False)
    caplog.set_level(logging.WARNING, logger="eawf.daemon.methods.state")

    with DaemonClient(runtime_dir=server.runtime_dir) as client:
        # First call → fallback path → warning emitted.
        first = client.call("state.read", {})
        # Second call → still falls back, but the one-shot flag stays
        # set so no second warning is logged.
        second = client.call("state.read", {})

    # Both calls return the daemon's boot-time anchor — repoA, NOT repoB.
    assert first["state"]["project"]["code"] == "REPOA"
    assert second["state"]["project"]["code"] == "REPOA"

    # Exactly one ``daemon_anchor_fallback`` warning is recorded across
    # the two calls — one-shot semantics enforced.
    anchor_warnings = [
        record for record in caplog.records if "daemon_anchor_fallback" in record.getMessage()
    ]
    assert len(anchor_warnings) == 1, (
        f"expected exactly 1 daemon_anchor_fallback warning, got {len(anchor_warnings)}"
    )
