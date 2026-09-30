"""Route the CLI's epoch-1 upkeep verbs to the daemon's verbs in process.

Session close and recover and worktree merge-back and cleanup reach the
daemon; a test drives them the way an operator does, with the daemon client
answered from the registered verbs in this process instead of a spawned
daemon.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.runtime.daemon import methods
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context


@pytest.fixture
def upkeep_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Answer the upkeep verbs in process; yields the in-process daemon's WAL root."""
    # Imported here: the in-process client's module loads the whole CLI.
    from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient

    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", InProcessClient)
    runtime = tmp_path / "daemon-runtime"
    InProcessClient.context = method_context(runtime)
    methods.ensure_all_methods_registered()
    yield runtime
    InProcessClient.context = None
