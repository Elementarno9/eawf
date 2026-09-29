"""The question card is fed the waiting operator decisions through the seam, live.

SURF-030, SURF-031 and SURF-111 on the console: a decision filed by
``runtime.question.open_decision`` is read in full through the seam's
``projection.question.decisions`` read, held as the question it asks, and Enter on its
Attention row opens the question detail drawing exactly the filed options, the one
recommendation and the default with its window -- not the approve-or-decline
consequence card, whose answers the decision does not offer. The live case serves a
provisioned canary over a private socket, as ``eawf ui`` would.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    KeyedPatch,
    PatchEntry,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.question_decision import QUESTION_OPEN_DECISION_METHOD
from eawf.surfaces.tui.console.decisions import QuestionRecord, QuestionStatus
from eawf.surfaces.tui.console.operations import Operator
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.workflow.decision_question import QUESTION_DECISIONS_METHOD
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    MILESTONE_URN,
    method_context,
    provision,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import live_console, render_setup

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0001"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
QUESTION: Final = "Should finished steps be committed without asking?"


def _options() -> list[dict[str, Any]]:
    return [
        {
            "option_id": "ask",
            "label": "Ask before each commit",
            "effect": "decline",
            "consequence": "Nothing is committed until you say so.",
            "preview": "edit -> ask -> commit",
        },
        {
            "option_id": "auto",
            "label": "Commit on its own",
            "effect": "approve",
            "consequence": "Each finished step is committed at once.",
            "preview": "edit -> commit",
        },
    ]


def _decision(**overrides: Any) -> PendingAction:
    """Return a waiting decision asked by an agent, recommending ``auto``."""
    row: dict[str, Any] = {
        "id": "ACT-0001",
        "urn": ACTION,
        "kind": "operator_decision",
        "subject_ref": MILESTONE_URN,
        "question": QUESTION,
        "options": _options(),
        "recommended_option_id": "auto",
        "recommendation_rationale": "Small commits keep every step easy to undo.",
        "idempotency_key": "req-commit-policy-1",
        "status": "WAITING",
        "requested_by": {"principal_kind": "agent", "principal_id": "AG-0001", "run_ref": RUN},
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }
    row.update(overrides)
    return PendingAction.model_validate(row)


# ---------- the record the card draws ----------


def test_surf_031_the_card_record_is_exactly_the_filed_options() -> None:
    """Order, labels and the one recommendation are the persisted ones."""
    record = QuestionRecord.of_decision(_decision())

    assert record.id == "ACT-0001"
    assert record.run == "RUN-00000010"
    assert record.scope == "MLS-0030"
    assert record.status is QuestionStatus.OPEN
    assert [(o.key, o.label, o.recommended) for o in record.options] == [
        ("ask", "Ask before each commit", False),
        ("auto", "Commit on its own", True),
    ]
    assert record.default_option is None


def test_surf_032_the_card_record_carries_the_default_and_its_window() -> None:
    """A filed default reaches the card with the time its override window closes."""
    until = AT + timedelta(minutes=30)
    record = QuestionRecord.of_decision(
        _decision(
            default_on_timeout="auto",
            override_until=until.isoformat(),
            default_policy="preferences.auto_choose=recommended",
        )
    )

    assert record.default_option == "auto"
    assert record.override_until == until


def test_surf_030_a_decision_a_person_asked_names_no_run() -> None:
    """Boundary: a person asks inside no Run, so the card names none."""
    record = QuestionRecord.of_decision(
        _decision(requested_by={"principal_kind": "human", "principal_id": "OP-0009"})
    )

    assert record.run is None


# ---------- the seam holds them ----------


class _Daemon:
    """Serves route reads and the decisions read; counts the decisions reads."""

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = decisions
        self.decision_reads = 0
        self._reads = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}

    def client(self) -> _Client:
        return _Client(self)

    def answer(self, method: str) -> dict[str, Any]:
        if method == QUESTION_DECISIONS_METHOD:
            self.decision_reads += 1
            return {"decisions": self.decisions}
        route = self._reads[method]
        return build_route_projection(
            route=route, document={}, cursor=5, scope_id="EAWF", generated_at=AT
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


def _seam(daemon: _Daemon, route: str = "attention") -> ProjectionSeam:
    return ProjectionSeam(
        route=route,
        scope_id="EAWF",
        state_path=None,
        clock=lambda: AT,
        operator=Operator(principal="OP-0001"),
        daemon_client_factory=daemon.client,
    )


def _patch(collection: str) -> KeyedPatch:
    return KeyedPatch.model_validate(
        {
            "schema_version": "1.0",
            "projection_kind": ROUTE_READ_MODELS["attention"],
            "routes": ["attention"],
            "scope_id": "EAWF",
            "canonical_sequence": 6,
            "entries": [
                PatchEntry.model_validate(
                    {
                        "key": "ACT-0002",
                        "urn": f"{CONTAINER}/{collection}/ACT-0002",
                        "collection": collection,
                        "revision": 1,
                        "status": "WAITING",
                    }
                ).model_dump(mode="json")
            ],
        }
    )


def test_surf_111_the_seam_holds_nothing_before_the_read() -> None:
    """Boundary: before any read the records are unheld, never an empty set."""
    seam = _seam(_Daemon([]))

    assert seam.decisions is None
    assert QUESTION_DECISIONS_METHOD in seam.owed()


def test_surf_111_the_attention_sync_reads_the_decisions_and_holds_them() -> None:
    daemon = _Daemon([_decision().model_dump(mode="json")])
    seam = _seam(daemon)

    loaded = asyncio.run(seam.sync())

    assert QUESTION_DECISIONS_METHOD in loaded
    held = seam.decisions
    assert held is not None
    assert held.principal == "OP-0001"
    question = held.question("ACT-0001")
    assert question is not None
    assert question.question == QUESTION
    assert QUESTION_DECISIONS_METHOD not in seam.owed()


def test_surf_111_another_route_owes_no_decisions_read() -> None:
    """Boundary: the read is owed where a decision is answered from, Attention only."""
    seam = _seam(_Daemon([]), route="activity")

    assert QUESTION_DECISIONS_METHOD not in seam.owed()


@pytest.mark.parametrize(("collection", "dropped"), [("pending_action", True), ("run", False)])
def test_surf_111_a_pending_action_patch_drops_the_held_read(
    collection: str, dropped: bool
) -> None:
    """A filed or answered action re-owes the read; a patch to anything else does not."""
    daemon = _Daemon([])
    seam = _seam(daemon)

    async def drive() -> None:
        await seam.sync()
        await seam.apply_patch(_patch(collection))

    asyncio.run(drive())

    assert (seam.decisions is None) is dropped
    assert (QUESTION_DECISIONS_METHOD in seam.owed()) is dropped


def test_surf_111_a_malformed_decision_read_is_refused() -> None:
    """Error path: a decision row that does not validate is not held as a question."""
    seam = _seam(_Daemon([{"id": "ACT-0001", "status": "WAITING"}]))

    with pytest.raises(ValueError, match="validation error"):
        asyncio.run(seam.load_decisions())
    assert seam.decisions is None


# ---------- live: the card an operator opens on a real tree ----------


def test_surf_030_live_enter_on_a_decision_opens_its_question_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filed through the verb, read through the seam, drawn as the question it asks."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EAWF_PREFERENCES__AUTO_CHOOSE", raising=False)
    canary = provision(tmp_path / "tree", code="ASK")
    runtime = tmp_path / "runtime"
    opened = asyncio.run(
        methods.dispatch(
            QUESTION_OPEN_DECISION_METHOD,
            method_context(runtime),
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "idempotency_key": "req-commit-policy-1",
                "actor": "AG-0001",
                "requested_by": {
                    "principal_kind": "agent",
                    "principal_id": "AG-0001",
                    "run_ref": RUN,
                },
                "question": QUESTION,
                "options": _options(),
                "recommended_option_id": "auto",
                "recommendation_rationale": "Small commits keep every step easy to undo.",
            },
        )
    )
    assert opened["created"] is True

    async def body() -> tuple[str, str | None, bool]:
        async with (
            live_console(canary.root, runtime) as (app, seam),
            app.run_test(size=SIZES[1]) as pilot,
        ):
            await render_setup(app, pilot, SessionSetup(route="attention", size=1))
            # the console's own sync worker reads what the Attention route owes
            await app.workers.wait_for_complete()
            await render_setup(app, pilot, SessionSetup(route="attention", size=1))
            app.press_key("Enter")
            app.render_frame()
            await pilot.pause()
            frame = "\n".join(app.frame_rows)
            return frame, app.session.overlay, seam.decisions is not None

    frame, overlay, held = asyncio.run(body())

    assert held
    assert overlay == "question"
    assert QUESTION in frame
    assert "Ask before each commit" in frame
    assert "Commit on its own · recommended · not consent" in frame
    assert "asked by RUN-00000010 under MLS-0030" in frame
