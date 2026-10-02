"""The Evidence route, the evidence viewer and the rung card draw the scorer's own records, live.

PLAN-048, PLAN-044, CON-133, CON-139, UI-021, UI-051 and UI-064 on the console. A claim
is filed through ``runtime.evidence.claim.file`` over a provisioned canary, the console
is served that tree over a private socket as ``eawf ui`` would, and the seam reads the
claim's ladder through ``projection.evidence.ladder``. The route rows, the overlay and
the card opened on a rung are then drawn from those records -- no fixture claim is
involved, so every word asserted below came from the producer.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.state.epoch2.evidence_rung import ClaimLadder
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from eawf.runtime.daemon.methods.delivery_acceptance import DELIVERY_RECORD_EVIDENCE_METHOD
from eawf.runtime.daemon.methods.evidence_ladder import EVIDENCE_CLAIM_FILE_METHOD
from eawf.surfaces.tui.console.decisions import ClaimRecord, RungOutcome
from eawf.surfaces.tui.console.navigation import open_overlay
from eawf.surfaces.tui.console.operations import Operator
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import live_console, render_setup

CLAIM_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0001"
AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
IN_WORDS: Final = "Replay any recorded log and every event arrives in the order it was recorded."
BREAKS_IF: Final = "One replay puts two events out of order."


def _dispatch(root: Path, runtime: Path, method: str, **params: Any) -> dict[str, Any]:
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(method, method_context(runtime), {"repo_root": str(root), **params})
    )
    return answer


def _file_claim(canary: CanaryProvision, runtime: Path) -> str:
    """File one evidence row and a claim about MLS-0030 citing it; return the row's URN."""
    root = canary.root
    evidence = _dispatch(
        root,
        runtime,
        DELIVERY_RECORD_EVIDENCE_METHOD,
        urn=MILESTONE_URN,
        actor="OPERATOR",
        idempotency_key="evidence-1",
        kind="artifact",
        summary="replaying run 12 kept every event in order",
    )["evidence_ref"]
    _dispatch(
        root,
        runtime,
        EVIDENCE_CLAIM_FILE_METHOD,
        urn=MILESTONE_URN,
        expected_revision=read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY],
        actor="OPERATOR",
        idempotency_key="claim-1",
        title="Replay keeps order",
        description=IN_WORDS,
        falsifier=BREAKS_IF,
        evidence=[str(evidence).rsplit("/", 1)[-1]],
    )
    return str(evidence)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    """A canary holding one filed, scored claim; its root, runtime and cited evidence."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    canary = provision(tmp_path / "tree", code="EVL")
    seed(canary, {"milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")}})
    runtime = tmp_path / "runtime"
    return canary.root, runtime, _file_claim(canary, runtime)


def _frames(
    root: Path,
    runtime: Path,
    *setups: SessionSetup,
    enter: bool = False,
    overlay_on: str | None = None,
) -> list[str]:
    """Render each setup on a live console, reading what the route owes before each one."""

    async def body() -> list[str]:
        frames: list[str] = []
        async with (
            live_console(root, runtime) as (app, _seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            for setup in setups:
                await render_setup(app, pilot, setup)
                # the console's own sync worker reads the route, then the ladder it owes
                for _ in range(3):
                    await app.workers.wait_for_complete()
                    await render_setup(app, pilot, setup)
                if overlay_on is not None:
                    open_overlay(app.session, "evidence", subject=overlay_on)
                    app.render_frame()
                    await pilot.pause()
                if enter:
                    app.press_key("Enter")
                    for _ in range(2):
                        await app.workers.wait_for_complete()
                        app.render_frame()
                        await pilot.pause()
                frames.append("\n".join(app.frame_rows))
        return frames

    return asyncio.run(body())


def _row(frame: str, start: str) -> str:
    return next(line for line in frame.splitlines() if line.lstrip(" ▸│").startswith(start))


def test_ui_064_live_the_route_draws_each_rung_from_its_record(
    tree: tuple[Path, Path, str],
) -> None:
    """UI-021/UI-064/PLAN-044: rung outcomes, the claim's prose and the standing, all produced."""
    root, runtime, _ = tree
    (frame,) = _frames(root, runtime, SessionSetup(route="evidence", subj_id="CLM-0001", size=2))
    assert "CLM-0001 · Replay keeps order" in frame
    assert IN_WORDS[:40] in _row(frame, "IN WORDS")
    assert BREAKS_IF in _row(frame, "BREAKS IF")
    # the implication was never filed, so it draws no row at all rather than a glyph
    assert "IT PROVES" not in frame
    assert "pass" in _row(frame, "1 resolve")
    assert "? unknown" in _row(frame, "2 anchor")
    assert _row(frame, "2 anchor").rstrip().endswith("open")
    for rung in ("3 screen", "4 entail"):
        assert "not run · ∅ awaiting rung 2" in _row(frame, rung)
    assert "FAILED" not in frame and "blocked by" not in frame
    assert "uncertified · derived from the four rung records" in _row(frame, "STANDING")
    # the check column is the rung's question, verbatim from its record
    assert "Does every reference resolve" in _row(frame, "1 resolve")


def test_con_139_live_enter_on_a_rung_opens_the_card_over_the_same_record(
    tree: tuple[Path, Path, str],
) -> None:
    """UI-051/CON-139/PLAN-048: the card's rows come from the rung record the row was drawn from."""
    root, runtime, evidence = tree
    # at 120 columns a reference and its full digest no longer fit on one card line
    (card,) = _frames(
        root, runtime, SessionSetup(route="evidence", subj_id="CLM-0001", size=1), enter=True
    )
    assert "RUNG 1 RESOLVE · PASS" in card
    assert "CHECK      Does every reference resolve" in card
    assert evidence in _row(card, "OVER")
    # the digest the rung ran over is reachable in full on the card, never shortened
    digest = _ladder_answer(root, runtime)["rungs"][0]["input_refs"][0]["digest"]
    assert digest.startswith("sha256:") and digest in card
    assert "references cited 1" in card and "references resolved 1" in card
    assert "evidence scorer" in _row(card, "AS OF")
    assert "written at sequence 3" in card
    assert "This rung holds" in card
    assert "conformance runner" not in card


def test_con_139_live_an_unknown_rung_states_unknown_and_never_an_empty_card(
    tree: tuple[Path, Path, str],
) -> None:
    root, runtime, _ = tree
    (card,) = _frames(
        root,
        runtime,
        SessionSetup(route="evidence", subj_id="CLM-0001", size=2, sel=1),
        enter=True,
    )
    assert "RUNG 2 ANCHOR · UNKNOWN" in card
    assert "no outcome yet - unknown, not failed" in card
    assert "open · evidence scorer" in card


def test_con_133_live_the_evidence_viewer_draws_the_filed_claim(
    tree: tuple[Path, Path, str],
) -> None:
    """CON-133: the overlay over the live claim, its ladder and a derived standing."""
    root, runtime, _ = tree
    (frame,) = _frames(
        root,
        runtime,
        SessionSetup(route="evidence", subj_id="CLM-0001", size=2),
        overlay_on="CLM-0001",
    )
    assert "evidence · CLM-0001" in frame
    assert BREAKS_IF in frame
    assert "1 resolve" in frame and "pass" in frame
    assert "? unknown" in frame
    assert "uncertified" in frame
    for stage in ("probe", "canary", "blocked"):
        assert stage not in frame


# ---------- the seam owes and holds the ladder ----------


def _ladder_answer(root: Path, runtime: Path) -> dict[str, Any]:
    return _dispatch(root, runtime, EVIDENCE_LADDER_METHOD, claim_key="CLM-0001")


class _Daemon:
    """Serves the Evidence route read and one claim's ladder; counts the ladder reads."""

    def __init__(self, ladder: dict[str, Any], claim_row: dict[str, Any]) -> None:
        self.ladder = ladder
        self.claim_row = claim_row
        self.ladder_reads = 0
        self._reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str) -> dict[str, Any]:
        if method == EVIDENCE_LADDER_METHOD:
            self.ladder_reads += 1
            return self.ladder
        document = {"claim": {"CLM-0001": self.claim_row}}
        return build_route_projection(
            route=self._reads[method], document=document, cursor=6, scope_id="EAWF", generated_at=AT
        ).model_dump(mode="json")


class _Client:
    def __init__(self, daemon: _Daemon) -> None:
        self._daemon = daemon

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._daemon.answer(method)


def _seam(daemon: _Daemon, route: str) -> ProjectionSeam:
    return ProjectionSeam(
        route=route,
        scope_id="EAWF",
        state_path=None,
        clock=lambda: AT,
        operator=Operator(principal="OP-0001"),
        daemon_client_factory=daemon.client,
    )


def _daemon(tree: tuple[Path, Path, str]) -> _Daemon:
    root, runtime, _ = tree
    ladder = _ladder_answer(root, runtime)
    claim = ladder["claim"]
    return _Daemon(ladder, claim)


def test_ui_021_a_route_opened_on_no_claim_owes_the_ladder_of_the_first_it_lists(
    tree: tuple[Path, Path, str],
) -> None:
    daemon = _daemon(tree)
    seam = _seam(daemon, "evidence")
    # the claim is unknown until the route's rows arrive, and owed as soon as they do
    assert EVIDENCE_LADDER_METHOD not in seam.owed()
    loaded = asyncio.run(seam.sync())
    assert loaded.index("evidence") < loaded.index(EVIDENCE_LADDER_METHOD)
    held = seam.decisions
    assert held is not None
    claim = held.claim("CLM-0001")
    assert claim is not None
    assert [r.outcome for r in claim.rungs] == [
        RungOutcome.PASS,
        RungOutcome.UNKNOWN,
        RungOutcome.NOT_RUN,
        RungOutcome.NOT_RUN,
    ]
    assert EVIDENCE_LADDER_METHOD not in seam.owed()
    assert daemon.ladder_reads == 1


def test_ui_051_another_route_owes_no_ladder(tree: tuple[Path, Path, str]) -> None:
    """Boundary: the ladder is read for the Evidence surfaces only."""
    seam = _seam(_daemon(tree), "activity")
    seam.about("CLM-0001")
    assert EVIDENCE_LADDER_METHOD not in seam.owed()
    assert seam.decisions is None


def test_plan_048_the_card_record_carries_every_field_of_the_rung_record(
    tree: tuple[Path, Path, str],
) -> None:
    root, runtime, evidence = tree
    ladder = ClaimLadder.model_validate(_ladder_answer(root, runtime))
    record = ClaimRecord.of_ladder(ladder)
    resolve = record.rungs[0]
    source = ladder.rungs[0]
    assert record.urn == CLAIM_URN
    assert record.in_words == IN_WORDS and record.proves is None
    assert resolve.check == source.question
    assert [(i.ref, i.digest) for i in resolve.inputs] == [(evidence, source.input_refs[0].digest)]
    assert resolve.as_of == source.evaluated_at
    assert resolve.sequence == source.written_at_sequence
    assert resolve.evaluator == source.evaluator
    assert not resolve.digest_unavailable
