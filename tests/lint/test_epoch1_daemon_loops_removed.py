"""The daemon's epoch-1 stale-wave and session-TTL sweeps stay deleted.

Both loops swept waves and sessions out of ``.ea/state.json``, a document a
native tree no longer carries, so they were removed with their schedulers.
These checks red when either module, an import of one, or a boot-time task
running one returns.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Final

import pytest

from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import AT, provision, tree_root

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted sweep modules, by dotted name.
SWEEP_MODULES: Final = ("eawf.runtime.daemon.stale_wave", "eawf.runtime.daemon.session_ttl")

#: The deleted schedulers ``eawfd`` boot called.
SWEEP_SCHEDULERS: Final = ("_schedule_session_ttl_sweep", "_schedule_stale_wave_sweep")

#: How many loop turns the boot gets to schedule its tasks before they are read.
_BOOT_TURNS: Final = 20


def _imported_modules(path: Path) -> set[str]:
    """Return every absolute dotted name *path* imports, module or attribute.

    ``from a import b`` yields both ``a`` and ``a.b`` because ``b`` may be a
    submodule.

    Args:
        path: A Python source file.

    Returns:
        The dotted names the file imports at any nesting level.

    Raises:
        SyntaxError: *path* is not valid Python.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_v1_sweep_modules_are_gone() -> None:
    """Neither module exists and nothing under ``src/`` or ``tests/`` imports one."""
    present = [
        name for name in SWEEP_MODULES if (REPO / "src" / f"{name.replace('.', '/')}.py").exists()
    ]
    assert present == []
    importers = sorted(
        str(path.relative_to(REPO))
        for root in ("src", "tests")
        for path in (REPO / root).rglob("*.py")
        if _imported_modules(path) & set(SWEEP_MODULES)
    )
    assert importers == []


async def _boot_task_sources(ctx: MethodContext, sock_path: Path) -> set[str]:
    """Boot the server on *ctx*, return the source file of every task it runs, then stop it.

    Args:
        ctx: A daemon context bound to a native tree, with a shutdown event.
        sock_path: Where the server binds its socket.

    Returns:
        The ``co_filename`` of each live task's coroutine once boot has scheduled them.
    """
    server = asyncio.create_task(daemon_main._run_server(sock_path, ctx, None))
    for _ in range(_BOOT_TURNS):
        await asyncio.sleep(0)
    sources = {
        task.get_coro().cr_code.co_filename  # type: ignore[union-attr]
        for task in asyncio.all_tasks()
        if hasattr(task.get_coro(), "cr_code")
    }
    assert isinstance(ctx.shutdown_event, asyncio.Event)
    ctx.shutdown_event.set()
    await server
    return sources


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX server binds a unix socket")
def test_daemon_boot_schedules_no_v1_sweep(tmp_path: Path) -> None:
    """The schedulers are gone and a booted daemon runs no task from either module."""
    assert [name for name in SWEEP_SCHEDULERS if hasattr(daemon_main, name)] == []
    provisioned = provision(tmp_path / "repo")
    state_path = tree_root(provisioned) / "state.json"

    async def _boot() -> set[str]:
        ctx = MethodContext(
            started_at=AT.isoformat(),
            pid=1,
            protocol_version="1",
            version="test",
            shutdown_event=asyncio.Event(),
            bus=EventBus(),
            state_path=state_path,
            wal_dir=tmp_path / "runtime" / "wal",
            idempotency_cache={},
        )
        # A unix socket path is capped near 108 bytes, which a pytest tmp_path can exceed.
        with tempfile.TemporaryDirectory(prefix="ea") as short:
            return await _boot_task_sources(ctx, Path(short) / "d.sock")

    sources = asyncio.run(_boot())
    assert any(source.endswith("main.py") for source in sources), sources
    swept = sorted(
        source
        for source in sources
        if Path(source).stem in {name.rsplit(".", 1)[1] for name in SWEEP_MODULES}
    )
    assert swept == []
