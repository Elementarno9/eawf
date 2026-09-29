"""A native canary tree holding Runs and their event lines, for the reflection tests.

The tree is provisioned by the real canary birth, so the reflection readers resolve
it through the same authority check a live tree answers; the Runs are placed in the
selected generation's document and their events on its run ledger.
"""

from __future__ import annotations

import hashlib
import socket
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import (
    CommandPayload,
    EventGapPayload,
    ReasoningSummaryPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import canary_ref, provision_canary

AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
CODE = "RFL"
RUN_PREFIX = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/run"


def run_row(key: str, *, status: str = "COMPLETED", hours: int = 2) -> dict[str, Any]:
    """Return one stored Run row, finished ``hours`` after it started unless queued."""
    started = None if status == "QUEUED" else AT
    ended = AT + timedelta(hours=hours) if status in {"COMPLETED", "FAILED"} else None
    return {
        "uid": str(hashlib.md5(key.encode(), usedforsecurity=False).hexdigest()[:8])
        + "-0000-5000-8000-000000000000",
        "key": key,
        "urn": f"{RUN_PREFIX}/{key}",
        "origin": {
            "confidence": "exact",
            "kind": "native",
            "mapping_basis": "native",
            "source_digest": None,
            "source_id": None,
            "source_kind": None,
            "source_schema_version": None,
            "source_urn": None,
        },
        "revision": 3,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "scope": {
            "purpose": "implement",
            "scope_kind": "task",
            "task_ref": f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/task/{CODE}-0001",
            "write_set": ["src"],
        },
        "status": status,
        "started_at": None if started is None else started.isoformat(),
        "ended_at": None if ended is None else ended.isoformat(),
        "suspension_reason": None,
        "failure": None,
    }


def event(key: str, sequence: int, **overrides: Any) -> RunEventRecord:
    """Return one event line of Run ``key``: a started reasoning turn by default."""
    fields: dict[str, Any] = {
        "event_ref": f"EVT-{sequence:08x}",
        "run_ref": f"{RUN_PREFIX}/{key}",
        "run_sequence": sequence,
        "event_kind": RunEventKind.REASONING_STARTED,
        "provenance": "provider_native",
        "payload": ReasoningSummaryPayload(phase="started"),
        "actor": "OP-0001",
        "recorded_at": AT + timedelta(seconds=sequence),
    }
    return RunEventRecord.model_validate(fields | overrides)


def summarized(key: str, sequence: int, summary: str) -> RunEventRecord:
    """Return the line closing a reasoning turn with the provider's summary."""
    return event(
        key,
        sequence,
        event_kind=RunEventKind.REASONING_SUMMARIZED,
        payload=ReasoningSummaryPayload(phase="summarized", summary=summary),
    )


def command(key: str, sequence: int) -> RunEventRecord:
    """Return a started foreground shell command."""
    return event(
        key,
        sequence,
        event_kind=RunEventKind.COMMAND_STARTED,
        payload=CommandPayload(
            command_family_ref="shell",
            command_ref=f"CMD-{sequence:08x}",
            phase="started",
            execution="foreground",
        ),
    )


def gap(key: str, sequence: int, *, expected: int) -> RunEventRecord:
    """Return the gap line explaining sequences ``expected`` to ``sequence - 1``."""
    return event(
        key,
        sequence,
        event_kind=RunEventKind.EVENT_GAP,
        payload=EventGapPayload(
            expected_sequence=expected,
            observed_sequence=sequence,
            replay_requested=True,
            replay_capability_state="verified",
            gap_ref=f"GAP-{sequence:08x}",
        ),
    )


def native_tree(
    repo_root: Path,
    runs: list[dict[str, Any]],
    events: list[RunEventRecord],
    *,
    canonical_sequence: int = 42,
) -> Path:
    """Provision a canary at ``repo_root`` holding ``runs`` and ``events``; return ``.ea``."""
    provision_canary(repo_root=repo_root, ref=canary_ref(CODE), provisioned_at=AT)
    tree_root = repo_root.resolve() / ".ea"
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document_path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    document = read_document(document_path)
    document.setdefault(Epoch2Collection.RUN.value, {}).update({row["key"]: row for row in runs})
    document["canonical_sequence"] = canonical_sequence
    write_document(document_path, document)
    for item in events:
        append_ledger_record(
            ledger_path(document_path, Epoch2Collection.RUN),
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=item.event_ref,
                status="recorded",
                recorded_at=item.recorded_at,
                payload=item.model_dump(mode="json"),
            ),
        )
    return tree_root


def tree_digest(root: Path, *, skip: Path) -> dict[str, str]:
    """Return a digest of every file under ``root`` except those under ``skip``."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_relative_to(skip)
    }


class EgressError(AssertionError):
    """A reflection verb tried to reach off this machine."""


@pytest.fixture
def refuse_egress(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Refuse every outbound connection and every spawned process, recording each try.

    Loopback connections stay open so a test can talk to a server it started itself.
    """
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in {"127.0.0.1", "::1", "localhost"}:
            attempts.append(f"connect {host}")
            raise EgressError(f"egress to {host}")
        return real_connect(self, address)

    def popen(*args: Any, **kwargs: Any) -> Any:
        attempts.append(f"spawn {args[0] if args else kwargs.get('args')}")
        raise EgressError("a spawned process")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield attempts
