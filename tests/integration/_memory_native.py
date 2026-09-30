"""An epoch-2 tree whose memory verbs reach the daemon's verbs in process.

The memory writers send the native ``memory.*`` verbs, so a test drives
them the way an operator does: through the CLI, with the daemon client
answered from the registered verbs in this process. ``HOME`` moves under
the test so nothing it writes lands in the operator's registry.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.memory.book import StandingNote, generation_memory_ledger, read_book
from eawf.runtime.daemon import methods
from tests._epoch2_helpers import lay_epoch2_tree
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context

#: The frozen epoch-1 document a tree born for project QR keeps.
QR_DOCUMENT: dict[str, Any] = {
    "schema_version": "1.0",
    "scope_kind": "repo",
    "urn": "urn:eawf:v1:state:QR",
    "updated_at": "2026-05-08T00:00:00Z",
    "project": {
        "code": "QR",
        "slug": "quant",
        "title": "Quant",
        "domains": ["quant"],
        "default_branch": "main",
        "status": "active",
        "repo_urn": "urn:eawf:v1:repo:QR",
    },
    "current": {
        "project_code": "QR",
        "track_id": None,
        "phase_id": None,
        "iter_id": None,
        "active_wave_ids": [],
        "active_session_ids": [],
    },
    "workspace": None,
    "phases": {},
    "iters": {},
    "waves": {},
    "artifacts": {},
    "agent_sessions": {},
    "plugins": {},
    "indexes": {},
}


def native_memory_tree(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Bear an epoch-2 tree for QR under *root* and route the CLI's daemon client in process.

    Yields:
        The tree's ``state.json``, which ``EA_STATE`` names.
    """
    # Imported here: the in-process client's module loads the whole CLI,
    # which a reader of this module's constants does not need.
    from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient

    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    state_path = lay_epoch2_tree(root, state=QR_DOCUMENT)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr("eawf.surfaces.cli.commands.domain.DaemonClient", InProcessClient)
    InProcessClient.context = method_context(root / "runtime")
    methods.ensure_all_methods_registered()
    yield state_path
    InProcessClient.context = None


def standing_notes(state_path: Path) -> dict[str, StandingNote]:
    """Return the notes the tree's memory ledger stands at."""
    ledger = generation_memory_ledger(state_path.parent)
    assert ledger is not None, "the tree answers in epoch 2"
    return read_book(ledger)


def decision_ledger(state_path: Path) -> Path:
    """Return the decision ledger beside the tree's memory ledger."""
    ledger = generation_memory_ledger(state_path.parent)
    assert ledger is not None, "the tree answers in epoch 2"
    return ledger_path(ledger.parent.parent / GENERATION_DOCUMENT, Epoch2Collection.DECISION)
