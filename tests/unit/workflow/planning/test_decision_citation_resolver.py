"""Standing decision citations retain the scope of their native record."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from eawf.kernel.store.ledger import LedgerRecord, render_ledger_line
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.workflow.planning.apply import _build_citation_resolvers


@pytest.mark.parametrize(
    ("scope", "citation", "expected"),
    [
        ("eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY", "D006", False),
        ("eawf://CP-SRV/track/TRK-CP-SRV-PLAY", "D006", False),
        ("invalid", "D006", False),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/CP-SRV/decision/D006",
            True,
        ),
        ("eawf://CP-SRV/track/TRK-CP-SRV-PLAY", "eawf://CP-SRV/decision/D006", True),
        ("eawf://CP-SRV/track/TRK-CP-SRV-PLAY", "eawf://CP-SRV/CP-SRV/CP-SRV/decision/D006", True),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://FOREIGN/CP-SRV/CP-SRV/decision/D006",
            False,
        ),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/OTHER/CP-SRV/decision/D006",
            False,
        ),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/OTHER/decision/D006",
            False,
        ),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/CP-SRV/task/CP-SRV-0006",
            False,
        ),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/CP-SRV/decision/D007",
            False,
        ),
        ("eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY", "D007", False),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/CP-SRV/decision/D006#rung-1",
            False,
        ),
        (
            "eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY",
            "eawf://ALLODS/CP-SRV/CP-SRV/decision/D006/extra",
            False,
        ),
        (None, "D006", True),
        (None, "eawf://ALLODS/CP-SRV/CP-SRV/decision/D006", False),
        ("invalid", "eawf://ALLODS/CP-SRV/CP-SRV/decision/D006", False),
        ("eawf://ALLODS/CP-SRV/CP-SRV/track/TRK-CP-SRV-PLAY", "", False),
    ],
)
def test_build_citation_resolvers_preserves_decision_scope(
    tmp_path: Path, scope: str | None, citation: str, expected: bool
) -> None:
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D006",
        status="ACTIVE",
        recorded_at=datetime(2026, 10, 29, tzinfo=UTC),
        payload={"payload": {"status": "ACTIVE", "superseded_by": None, "scope_ref": scope}},
    )
    (tmp_path / "decision.jsonl").write_text(f"{render_ledger_line(record)}\n")
    session = cast(
        RootSession,
        SimpleNamespace(ledger_path=lambda collection: tmp_path / f"{collection.value}.jsonl"),
    )
    resolver = _build_citation_resolvers(session, {}, provider_tuple=())
    assert resolver.decision_is_resolvable(citation) is expected


@pytest.mark.parametrize(
    ("status", "superseded_by"),
    [("PROPOSED", None), ("SUPERSEDED", None), ("OBSOLETE", None), ("ACTIVE", "D007")],
)
def test_build_citation_resolvers_refuses_nonstanding_decisions(
    tmp_path: Path, status: str, superseded_by: str | None
) -> None:
    record = LedgerRecord(
        collection=Epoch2Collection.DECISION,
        record_key="D006",
        status=status,
        recorded_at=datetime(2026, 10, 29, tzinfo=UTC),
        payload={
            "payload": {
                "status": status,
                "scope_ref": "eawf://CP-SRV/track/TRK-CP-SRV-PLAY",
                "superseded_by": superseded_by,
            }
        },
    )
    (tmp_path / "decision.jsonl").write_text(f"{render_ledger_line(record)}\n")
    session = cast(
        RootSession,
        SimpleNamespace(ledger_path=lambda collection: tmp_path / f"{collection.value}.jsonl"),
    )
    resolver = _build_citation_resolvers(session, {}, provider_tuple=())
    assert not resolver.decision_is_resolvable("D006")
    assert not resolver.decision_is_resolvable("eawf://CP-SRV/decision/D006")
