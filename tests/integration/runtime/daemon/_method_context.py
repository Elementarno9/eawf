"""A daemon method context over files under a test's tmp directory."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext


def build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    """Return a context bound to *state_path*, its event store and a WAL under *tmp_path*."""
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
