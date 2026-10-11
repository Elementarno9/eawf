"""A console holding a Batch shows its proof note the moment the daemon announces it.

A proof's progress is no committed transition, so it arrives as a notice rather than a
keyed patch; the seam reads each held route holding the Batch again and tells the
patch watchers, which is what repaints the frame.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.connection import read_method
from eawf.kernel.projection.row_facts import BATCH_PROOF_FACT
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from tests.tui.surfaces.tui.console.test_seam_pool import AT, CURSOR, SCOPE, FakeDaemon, seam_over

BATCH: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
NOTE: Final = "proving 2 criteria at 0123456789ab"


class ProofDaemon(FakeDaemon):
    """A daemon whose home route holds one Batch, beside the proof note it is told of."""

    def __init__(self) -> None:
        super().__init__()
        self.proofs: dict[str, str] = {}

    def answer(self, method: str) -> dict[str, Any]:
        if method != read_method("scope.home"):
            return super().answer(method)
        self.reads.append("scope.home")
        document = {"batch": {"BAT-0007": {"urn": BATCH, "revision": 1, "status": "PLANNED"}}}
        return build_route_projection(
            route="scope.home",
            document=document,
            cursor=CURSOR,
            scope_id=SCOPE,
            generated_at=AT,
            proofs=self.proofs,
        ).model_dump(mode="json")


def notice(batch_ref: str, note: str | None) -> Envelope:
    """Return the notice the daemon publishes when a Batch's proof moves."""
    return Envelope(
        id="batch-proof-noted-1",
        kind=StoreKind.BATCH_PROOF_NOTED,
        scope_id=batch_ref,
        created_at=datetime(2026, 10, 8, tzinfo=UTC),
        summary="batch_proof_noted",
        payload={"batch_ref": batch_ref, "note": note},
    )


def test_a_proof_notice_reads_the_routes_holding_the_batch_and_repaints() -> None:
    daemon = ProofDaemon()
    seam = seam_over(daemon)
    heard: list[tuple[str, ...]] = []

    async def drive() -> dict[str, str]:
        await seam.load("scope.home")
        seam.watch(heard.append)
        daemon.proofs[BATCH] = NOTE
        await seam.take_notice(notice(BATCH, NOTE))
        row = seam.held_row("BAT-0007")
        assert row is not None
        return dict(row.facts)

    facts = asyncio.run(drive())

    assert facts[BATCH_PROOF_FACT] == NOTE
    assert daemon.reads.count("scope.home") == 2
    assert heard == [("scope.home",)]


def test_a_notice_for_a_batch_no_route_holds_reads_nothing() -> None:
    daemon = ProofDaemon()
    seam = seam_over(daemon)
    heard: list[tuple[str, ...]] = []

    async def drive() -> None:
        await seam.load("scope.home")
        seam.watch(heard.append)
        await seam.take_notice(notice(BATCH.replace("BAT-0007", "BAT-0009"), None))
        await seam.take_notice(
            notice(BATCH, None).model_copy(update={"payload": {"batch_ref": "nope"}})
        )

    asyncio.run(drive())

    assert daemon.reads.count("scope.home") == 1
    assert heard == []
