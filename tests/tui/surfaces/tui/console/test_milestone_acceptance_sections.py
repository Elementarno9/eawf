"""The Milestone frame's sections are drawn from its acceptance records.

The journey the Milestone record declares, the sealed bundle's outcome per step, the
evidence rows the store holds for what a step cites, the revision chain and the
approval stage each fill one section; a step with no evidence says so in words, and a
Milestone whose acceptance record is not read yet says that instead of a section.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.delivery.acceptance import EvidenceRow, MilestoneAcceptanceBundle
from eawf.kernel.state.epoch2.milestone import AcceptanceStep
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods import projection as projection_methods
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.registry import SECTIONS
from eawf.surfaces.tui.console.renderers.milestone import NO_BUNDLE, NO_JOURNEY
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.workflow.projection.acceptance import (
    CriterionRow,
    MilestoneAcceptanceRecord,
    journey_rows,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import sealed_approval_row

from .test_console_live_smoke import (
    committed_tree,
    live_console,
    render_setup,
    require_epoch2_repository,
)
from .test_enter_opened_cards import CONTAINER, MILESTONE_URN, _approval, _bundle
from .test_linked_data_truth import _linked

#: The Milestone every probe frame is about.
KEY = "MLS-0030"

#: The waiting acceptance question the stage test names.
QUESTION_URN = f"{CONTAINER}/pending-action/ACT-0009"

#: When the probe evidence was recorded.
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _journey(*, optional_second: bool = False) -> tuple[AcceptanceStep, ...]:
    """Return a two-step journey: an operator step the bundle covers and a system step."""
    return (
        AcceptanceStep(
            step_id="AS-01",
            actor="operator",
            action="install the package from the index",
            expected_observation="the install completes and reports the version",
            evidence_kinds=("artifact",),
        ),
        AcceptanceStep(
            step_id="AS-02",
            actor="system",
            action="run the release pre-flight",
            expected_observation="the pre-flight passes",
            evidence_kinds=("store_record", "audit"),
            required=not optional_second,
        ),
    )


def _evidence() -> tuple[EvidenceRow, ...]:
    """Return the store's row for the key the probe bundle cites."""
    return (
        EvidenceRow(
            id="EVD-0002",
            kind="artifact",
            summary="the install log shows version 0.7.0",
            recorded_at=AT,
        ),
    )


def _app(status: str, record: MilestoneAcceptanceRecord, *, section: str) -> ConsoleApp:
    """Return a console on the Milestone in ``status`` holding ``record``, on ``section``."""
    document = {
        "milestone": {
            KEY: {"urn": MILESTONE_URN, "revision": 3, "status": status},
        },
    }
    app = _linked("milestone", document, subject=KEY)
    assert app.seam is not None
    app.seam._acceptance[KEY] = record
    app.session.section = SECTIONS.index(section)
    return app


def _text(status: str, section: str, **record: Any) -> str:
    """Return the frame of one section over a record built from ``record``."""
    held = MilestoneAcceptanceRecord(milestone_key=KEY, **record)
    view = _app(status, held, section=section).view()
    # the widest tracked frame, so no long line is clipped
    w, h = SIZES[-1]
    return "\n".join(compose_frame(replace(view, w=w, h=h)))


# ---------- glance: the declared journey beside the sealed outcome ----------


def test_glance_draws_each_declared_step_beside_what_the_bundle_found() -> None:
    """A covered step reads proven; a step the bundle does not cover reads not yet run."""
    text = _text(
        "ACCEPTANCE_REVIEW",
        "glance",
        bundle=_bundle(),
        journey=_journey(optional_second=True),
        evidence=_evidence(),
    )
    assert "JOURNEY      2 steps · 1 proven · 0 open · 1 not yet run" in text
    assert "AS-01 · operator · required · proven · install the package from the index" in text
    assert "expects the install completes and reports the version" in text
    assert "AS-02 · system · optional · not yet run · run the release pre-flight" in text


def test_glance_without_a_journey_keeps_the_sealed_criteria() -> None:
    """Boundary: a bundle read without its journey still draws the criteria it sealed."""
    text = _text("COMPLETED", "glance", bundle=_bundle())
    assert "CRITERIA     1 of 1 proven · 0 open" in text
    assert "JOURNEY" not in text


# ---------- try: the verb the stage asks for next ----------


@pytest.mark.parametrize(
    ("status", "record", "verb"),
    [
        ("PLANNED", {}, f"eawf milestone activate {MILESTONE_URN}"),
        ("ACTIVE", {}, f"eawf milestone open-review {MILESTONE_URN}"),
        (
            "ACCEPTANCE_REVIEW",
            {"bundle": _bundle()},
            f"eawf milestone open-approval {MILESTONE_URN}",
        ),
        (
            "ACCEPTANCE_REVIEW",
            {"bundle": _bundle(), "waiting_approval_urn": QUESTION_URN},
            f"eawf milestone seal-approval {QUESTION_URN}",
        ),
        (
            "ACCEPTANCE_REVIEW",
            {"bundle": _bundle(), "approval": _approval()},
            f"eawf milestone accept {MILESTONE_URN}",
        ),
        (
            "COMPLETED",
            {"bundle": _bundle(), "accepted_binding": _bundle().accepted_binding},
            f"accepted at head {'a' * 7} · tree {'b' * 7}",
        ),
        ("CANCELLED", {}, "cancelled"),
    ],
)
def test_try_names_the_verb_the_stage_asks_for_next(
    status: str, record: dict[str, Any], verb: str
) -> None:
    """Each stage of review and approval names its own next verb, never a fixed one."""
    text = _text(status, "try", journey=_journey(), **record)
    assert f"TRY          {verb}" in text


def test_try_lists_the_steps_an_operator_walks_and_only_those() -> None:
    """The operator's steps are listed to walk; a system step is not."""
    text = _text("ACTIVE", "try", journey=_journey())
    assert "WALK         AS-01 · install the package from the index" in text
    assert "AS-02 · run" not in text


# ---------- changes: the bundle revision chain ----------


def test_changes_name_the_revision_a_repair_superseded() -> None:
    """A successor revision names the one it replaced and the repair's reason."""
    first = _bundle()
    second = MilestoneAcceptanceBundle.model_validate(
        {
            **first.model_dump(mode="json"),
            "revision": 2,
            "supersedes_revision": 1,
            "supersedes_digest": first.digest(),
            "repair_reason": {"code": "repair-requested", "message": "the log was truncated"},
        }
    )
    text = _text("ACCEPTANCE_REVIEW", "changes", bundle=second, journey=_journey())
    assert "CHANGES      revision 2 supersedes revision 1" in text
    assert "repair · the log was truncated" in text


@pytest.mark.parametrize(
    ("record", "line"),
    [
        ({}, "∅ no bundle is sealed, so no revision has changed"),
        ({"bundle": _bundle()}, "revision 1 · the first sealed revision"),
    ],
)
def test_changes_boundaries(record: dict[str, Any], line: str) -> None:
    """Boundary: no bundle, and a first revision that superseded nothing."""
    assert line in _text("ACCEPTANCE_REVIEW", "changes", journey=_journey(), **record)


# ---------- evidence: per step, the rows the store holds ----------


def test_evidence_draws_the_held_row_and_says_so_when_a_step_has_none() -> None:
    """A cited row draws its summary; a step no bundle covers says so in words."""
    text = _text(
        "ACCEPTANCE_REVIEW", "evidence", bundle=_bundle(), journey=_journey(), evidence=_evidence()
    )
    assert "EVIDENCE     AS-01 · asks for artifact" in text
    assert "EVD-0002 · artifact · the install log shows version 0.7.0" in text
    assert "AS-02 · asks for store_record, audit" in text
    assert "no evidence is recorded: no sealed bundle covers this step" in text


def test_evidence_names_a_cited_key_the_store_does_not_hold() -> None:
    """Error path: a citation the evidence ledger has no row for is named, not dropped."""
    text = _text("ACCEPTANCE_REVIEW", "evidence", bundle=_bundle(), journey=_journey())
    assert "EVD-0002 · cited, but the evidence store holds no row for it" in text


def test_evidence_says_a_sealed_step_without_evidence_has_none() -> None:
    """A step the bundle sealed with no citation says it carries no evidence."""
    text = _text("ACCEPTANCE_REVIEW", "evidence", bundle=_bundle(passed=False), journey=_journey())
    assert "no evidence is recorded for this step" in text


# ---------- risks and raw ----------


def test_risks_name_each_step_standing_between_the_journey_and_acceptance() -> None:
    """A failed step, an unrun step and an optional step each read as their own risk."""
    open_step = _text(
        "ACCEPTANCE_REVIEW", "risks", bundle=_bundle(passed=False), journey=_journey()
    )
    assert "RISKS        AS-01 did not pass · the install completed and reported the version" in (
        open_step
    )
    assert "AS-02 has not been run" in open_step
    optional = _text(
        "ACCEPTANCE_REVIEW", "risks", bundle=_bundle(), journey=_journey(optional_second=True)
    )
    assert "AS-02 is optional, so it cannot satisfy acceptance" in optional


def test_risks_say_nothing_is_open_once_every_required_step_is_proven() -> None:
    """Boundary: a journey of one proven required step leaves no risk."""
    text = _text("COMPLETED", "risks", bundle=_bundle(), journey=_journey()[:1])
    assert "RISKS        nothing open · every required step is proven" in text


def test_raw_draws_the_whole_digest_and_binding() -> None:
    """Raw prints the full digest and the binding the record was accepted at."""
    bundle = _bundle()
    text = _text(
        "COMPLETED",
        "raw",
        bundle=bundle,
        journey=_journey(),
        accepted_binding=bundle.accepted_binding,
    )
    assert f"bundle revision 1 · digest {bundle.digest()}" in text
    assert f"accepted head {'a' * 40} · tree {'b' * 40}" in text
    assert NO_BUNDLE in _text("ACTIVE", "raw", journey=_journey())


@pytest.mark.parametrize("section", ["evidence", "risks"])
def test_a_journey_section_before_the_record_is_read_says_so(section: str) -> None:
    """Boundary: with no journey read, a journey section names that absence."""
    assert NO_JOURNEY in _text("ACTIVE", section)


# ---------- the read model and the daemon producer ----------


def test_journey_rows_pair_steps_with_outcomes_and_split_held_from_unheld() -> None:
    """Boundary: empty in, empty out; a cited key is either held or named unheld."""
    assert journey_rows((), (), ()) == ()
    outcome = CriterionRow(
        step_id="AS-01",
        passed=True,
        observation="seen",
        evidence_keys=("EVD-0002", "EVD-0003"),
    )
    rows = journey_rows(_journey(), (outcome,), _evidence())
    assert [row.step_id for row in rows] == ["AS-01", "AS-02"]
    assert [row.id for row in rows[0].evidence] == ["EVD-0002"]
    assert rows[0].unheld_keys == ("EVD-0003",)
    assert rows[1].outcome is None


def _action(**changes: Any) -> dict[str, Any]:
    """Return a protected approval row with ``changes`` applied."""
    row = sealed_approval_row()
    row.update(urn=QUESTION_URN, **changes)
    return row


def test_the_daemon_names_only_an_unanswered_question_of_this_milestone() -> None:
    """A waiting question is named; a sealed one or another Milestone's is not."""
    waiting = _action(
        status="WAITING", resolution_actor=None, selected_option_id=None, receipt_ref=None
    )
    other = f"{CONTAINER}/milestone/MLS-0031"
    find = projection_methods._waiting_approval
    assert find({"pending_action": {"ACT-0009": waiting}}, MILESTONE_URN) == QUESTION_URN
    assert find({"pending_action": {"ACT-0009": waiting}}, other) is None
    assert find({"pending_action": {"ACT-0009": _action()}}, MILESTONE_URN) is None
    assert find({}, MILESTONE_URN) is None


def test_a_journey_that_does_not_read_back_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: a Milestone row whose journey is malformed refuses the read."""
    fields = {"urn": MILESTONE_URN, "acceptance_journey": [{"step_id": "AS-01"}]}
    monkeypatch.setattr(projection_methods, "read_document", lambda _path: {})
    monkeypatch.setattr(projection_methods, "document_path", lambda _authority: Path("doc"))
    monkeypatch.setattr(projection_methods, "_milestone_fields", lambda **_kw: fields)
    monkeypatch.setattr(projection_methods, "_sealed_bundle", lambda **_kw: None)
    with pytest.raises(DaemonValidationError, match="acceptance journey of MLS-0030"):
        projection_methods._milestone_acceptance(authority=None, key=KEY)  # type: ignore[arg-type]


# ---------- the committed tree ----------


def test_the_committed_milestones_draw_their_sections_from_their_records(
    tmp_path: Path,
) -> None:
    """MLS-0100 (accepted) and MLS-0101 (active) draw their journeys and evidence live."""
    require_epoch2_repository()
    root = committed_tree(tmp_path)

    async def body() -> dict[tuple[str, str], str]:
        frames: dict[tuple[str, str], str] = {}
        async with (
            live_console(root, tmp_path / "runtime") as (app, seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            for key in ("MLS-0100", "MLS-0101"):
                for section in ("glance", "try", "evidence", "risks"):
                    setup = SessionSetup(
                        route="milestone", subjId=key, size=2, section=SECTIONS.index(section)
                    )
                    await render_setup(app, pilot, setup)
                    for _ in range(100):
                        if seam.acceptance_for(key) is not None:
                            break
                        await pilot.pause(0.05)
                    frames[key, section] = await render_setup(app, pilot, setup)
        return frames

    frames = asyncio.run(body())
    accepted = frames["MLS-0100", "glance"]
    assert "JOURNEY      2 steps · 2 proven · 0 open · 0 not yet run" in accepted
    assert "AS-01 · operator · required · proven · run the fast suite" in accepted
    assert "TRY          accepted at head 61c75ef · tree 8d8ea39" in frames["MLS-0100", "try"]
    assert "EVD-0002 · audit · Audit A-P36-I01-eval-r4" in frames["MLS-0100", "evidence"]
    assert "nothing open · every required step is proven" in frames["MLS-0100", "risks"]
    active = frames["MLS-0101", "glance"]
    # the record's own action text, drawn as written
    assert "AS-01 · operator · required · not yet run · open eawf tui" in active
    assert "TRY          eawf milestone open-review " in frames["MLS-0101", "try"]
    assert (
        "no evidence is recorded: no sealed bundle covers this step"
        in (frames["MLS-0101", "evidence"])
    )
    assert "AS-02 has not been run" in frames["MLS-0101", "risks"]
    for text in frames.values():
        assert "no producer states this section" not in text
