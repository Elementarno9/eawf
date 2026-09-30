"""Live-test the four console routes that draw records beside their projection.

The health route's runtime tuple verdicts, the Git surface's generations, the conflict
card's frames and the receipt card's receipt are each filed where their producer files
them -- the conformance stage journal, the Batch ledger, the receipt ledger -- on a
disposable epoch-2 canary. A real daemon on a private socket serves the tree, and the
console opened by ``eawf ui``'s own launch function reads each record back through its
daemon verb and draws it. Nothing is handed to the console directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from eawf.kernel.identity.urn import parse_qualified_urn
from eawf.kernel.runtime.certification import CertificationFailureCode, ConformanceStageRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods.conformance import StoreStageJournal
from eawf.runtime.daemon.methods.delivery import _append_conflict, _append_generation
from eawf.runtime.daemon.methods.delivery_proof import _proof_line
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    rekeyed,
    seed,
    seed_row,
)
from tests.integration.workflow.delivery._completion_fixtures import gate
from tests.tui.surfaces.tui.console.test_enter_opened_cards import RECEIPT_KEY, _receipt
from tests.tui.surfaces.tui.console.test_merge_conflict_surface import (
    BATCH,
    TASK,
    _conflict,
    _generation,
)
from tests.tui.surfaces.tui.console.test_transcript_live import (
    PARENT,
    _Daemon,
    _frame,
    _launch,
    _until,
    live_tree,
)

__all__ = ["live_tree"]

#: The runtime tuple the conformance journal holds a failed certify for.
TUPLE_DIGEST: Final = "sha256:" + "ab12cd34ef56" * 5 + "abcd"  # pragma: allowlist secret

#: When the stage ran.
AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _file_records(repo: Path, daemon: _Daemon) -> None:
    """File one record of each kind where its producer files it."""
    # seed() reads nothing of a provisioned canary but its root
    batches = {Epoch2Collection.BATCH.value: {"BAT-0001": _batch_row()}}
    seed(SimpleNamespace(root=repo), batches)  # type: ignore[arg-type]
    context = daemon._ctx.native_root_context(repo / ".ea")
    with context.session([BATCH]) as session:
        _append_generation(session, _generation(2, parent_generation_id=None))
        _append_conflict(session, _conflict(), at=AT)
    with context.session([TASK]) as session:
        commit_ledger_append(
            session, _proof_line(parse_qualified_urn(TASK), gate("CR-01"), _receipt())
        )
    StoreStageJournal(repo / ".ea" / "state.json").append(
        tuple_digest=TUPLE_DIGEST,
        record=ConformanceStageRecord(
            stage="certify",
            outcome="failed",
            reason_code=CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
            evidence_ref="artifact://conformance/certify-2026-09-30",  # type: ignore[arg-type]
            started_at=AT,
            completed_at=AT,
        ),
    )


def _batch_row() -> dict[str, Any]:
    return rekeyed(seed_row("batch", "ACTIVE"), key="BAT-0001")


async def _open(app: ConsoleApp, pilot: Any, route: str, subject: str | None) -> str:
    app.reset(SessionSetup(route=route, subjId=subject))
    await pilot.pause()
    return await _frame(app, pilot)


def test_fu_31_each_route_draws_the_record_its_daemon_read_returns(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FU-31: health, Git, conflict and receipt draw what the tree filed, read live."""
    repo, daemon = live_tree
    _file_records(repo, daemon)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, "health", None)
        health = await _until(app, pilot, lambda f: "runtime_tuple_ab12cd34ef56" in f)
        assert "certify" in health
        await _open(app, pilot, "git.pr", "BAT-0001")
        git = await _until(app, pilot, lambda f: "head ING-000002" in f)
        assert "no generation is this Batch's head" not in git
        await _open(app, pilot, "merge.conflict", "BAT-0001")
        card = await _until(app, pilot, lambda f: "MERGE CONFLICT · 3 hunks" in f)
        assert "src/pkg/loader.py" in card
        await _open(app, pilot, "receipt", RECEIPT_KEY)
        receipt = await _until(app, pilot, lambda f: "G-01" in f)
        assert RECEIPT_KEY in receipt

    _launch(repo, monkeypatch, scenario, (120, 30))


def test_con_020_con_099_a_key_nothing_names_opens_the_resolution_card_as_missing(
    live_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-020, CON-099: a route opened onto a key no record holds opens the card.

    A key another record does hold -- the live Run, opened on the Git surface whose rows
    are Batches -- opens nothing, and dismissing the card leaves the route in place.
    """
    repo, daemon = live_tree
    _file_records(repo, daemon)

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        await _open(app, pilot, "git.pr", "BAT-0999")
        card = await _until(app, pilot, lambda f: "ENDING    missing" in f)
        assert "BAT-0999 was never recorded" in card
        assert app.session.overlay == "resolution"
        await pilot.press("escape")
        assert (app.session.overlay, app.session.route) == (None, "git.pr")
        await _open(app, pilot, "git.pr", PARENT)
        await _until(app, pilot, lambda f: "Git" in f)
        await seam.sync()
        assert app.session.overlay is None

    _launch(repo, monkeypatch, scenario, (120, 30))
