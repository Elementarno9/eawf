"""The live routes draw the pack's frames from what the tree holds, and say what it does not.

Each test is named for the console jury finding it closes: the Activity rail marks the
chosen bucket in the pack's words, Attention keeps its buckets whatever is open and lists
only open items grouped by bucket, Notifications is the boxed card, Backlog says what a
draft still needs, Release says when no release is cut, Search states an empty query once,
the card routes stay cards when nothing is held, and the readiness matrix names the
absent release rather than an unknown one.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.projection.activity import ActivityExceptionBucket
from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.verification import build_verification_view
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays.decision import render_readiness
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.renderers.activity import bucket_seam
from eawf.surfaces.tui.console.renderers.backlog import promotion_needs
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.workflow.projection.acceptance import ReleaseReadinessView, build_acceptance_view
from tests.tui.surfaces.tui.console.test_console_verbs import _Host

AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CURSOR = 41208
ROOT = "eawf://EAWF/EAWF/EAWF"
ME = "OP-0001"


def _row(kind: str, key: str, status: str, revision: int = 1, **extra: Any) -> dict[str, Any]:
    return {"urn": f"{ROOT}/{kind}/{key}", "revision": revision, "status": status, **extra}


def _action(key: str, status: str, subject: str) -> dict[str, Any]:
    return {
        **_row("pending-action", key, status),
        "kind": "operator_decision",
        "question": f"which way for {subject}?",
        "subject_ref": f"{ROOT}/{subject}",
        "requested_by": {"principal_kind": "human", "principal_id": ME},
        "created_at": "2026-09-17T11:00:00Z",
    }


#: Two Milestones on a Track, a planned and a draft Task, three Runs, and two pending
#: actions: one open, one sealed.
DOCUMENT: dict[str, Any] = {
    "track": {"TRK-CORE": _row("track", "TRK-CORE", "ACTIVE", title="Core framework")},
    "milestone": {
        "MLS-0100": _row(
            "milestone",
            "MLS-0100",
            "ACTIVE",
            title="Cut the candidate",
            primary_track_ref=f"{ROOT}/track/TRK-CORE",
        ),
        "MLS-0101": _row("milestone", "MLS-0101", "PLANNED", title="Publish the release"),
    },
    "task": {
        "TSK-0001": _row("task", "TSK-0001", "DRAFT", intent="Bound the replay window"),
        "TSK-0002": _row("task", "TSK-0002", "PLANNED", intent="Seal the ledger"),
    },
    "run": {
        "RUN-00000001": _row("run", "RUN-00000001", "RUNNING"),
        "RUN-00000002": _row("run", "RUN-00000002", "FAILED"),
        "RUN-00000003": _row("run", "RUN-00000003", "QUEUED"),
    },
    "pending_action": {
        "ACT-0001": _action("ACT-0001", "WAITING", "run/RUN-00000001"),
        "ACT-0002": _action("ACT-0002", "SEALED", "run/RUN-00000002"),
    },
}


def _projection(route: str, document: dict[str, Any] | None = None) -> Any:
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=CURSOR,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _view(
    route: str,
    *,
    w: int = 160,
    document: dict[str, Any] | None = None,
    bucket: str | None = None,
    query: str = "",
) -> View:
    """Return a linked console's view of ``route``, holding the read model the app builds."""
    session = Session()
    session.route = route
    session.bucket = bucket
    session.pq = query
    projection = _projection(route, document)
    held: dict[str, Any]
    if route in ("activity", "attention", "notifications"):
        held = {"register": build_register_view(projection)}
    elif route == "evidence.digest":
        held = {"projection": build_verification_view(projection, verdicts=())}
    elif route == "release":
        held = {"projection": build_acceptance_view(projection)}
    else:
        held = {"projection": build_spine_view(projection)}
    attention = build_register_view(_projection("attention", document))
    return View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        linked=True,
        principal=ME,
        attention=attention,
        **held,
    )


def _frame(route: str, **kwargs: Any) -> list[str]:
    view = _view(route, **kwargs)
    frame = render_route(view)
    assert len(frame) == view.h
    return frame


def _text(frame: list[str]) -> str:
    return "\n".join(frame)


def _rail(frame: list[str]) -> list[str]:
    """Return the rail half of every row that has one."""
    return [row.split("│ ", 1)[1] for row in frame if "│ " in row]


# ---------- J2-03 and J2-04: the Activity rail ----------


def test_j2_03_the_activity_rail_marks_the_chosen_bucket_with_the_caret() -> None:
    rail = _rail(_frame("activity", bucket="failed"))
    carets = [row for row in rail if "▸" in row]
    assert len(carets) == 1
    assert carets[0].startswith("▸failed")


def test_j2_03_an_empty_chosen_bucket_names_itself_rather_than_a_filter() -> None:
    frame = _frame("activity", bucket="lost or stale")
    assert "nothing in lost or stale · 3 runs are in other buckets" in _text(frame)
    assert "Esc clears the bucket" in _text(frame)


def test_j2_03_the_activity_rail_marks_nothing_when_every_bucket_shows() -> None:
    assert not [row for row in _rail(_frame("activity")) if "▸" in row]


def test_j2_03_tab_walks_the_buckets_the_native_frame_drew() -> None:
    view = _view("activity")
    render_route(view)
    session = view.session
    keys = session.bucket_keys
    assert keys is not None and keys[0] is None and len(keys) == 9
    ctx = Ctx(session=session, fixture=view.fixture, host=_Host(), w=160, h=40)
    for expected in [*keys[1:], None]:
        assert bucket_seam(ctx, "Tab", False)
        assert session.bucket == expected


def test_j2_03_tab_on_the_prototype_frame_is_left_to_the_dispatcher() -> None:
    session = Session()
    session.route = "activity"
    ctx = Ctx(
        session=session, fixture=Fixture.from_chrome(load_chrome()), host=_Host(), w=160, h=40
    )
    assert not bucket_seam(ctx, "Tab", False)
    session.bucket_keys = [None, "failed"]
    assert not bucket_seam(ctx, "Enter", False)


def test_j2_04_the_activity_buckets_use_the_pack_words_and_keep_all_eight() -> None:
    assert ActivityExceptionBucket.LOST_STALE.value == "lost or stale"
    assert ActivityExceptionBucket.CHECKING_INTEGRATING.value == "checking or integrating"
    rail = _rail(_frame("activity"))
    tops = [row.strip() for row in rail[1:] if row.strip() and "↳" not in row]
    assert len(tops) == 8
    assert any(row.startswith("lost or stale") for row in tops)
    assert any(row.startswith("queued") for row in tops)
    assert not [row for row in rail if "/" in row]


# ---------- J2-05 and J2-06: Attention ----------


def _nothing_open() -> dict[str, Any]:
    return {**DOCUMENT, "pending_action": {"ACT-0002": DOCUMENT["pending_action"]["ACT-0002"]}}


def test_j2_05_the_attention_rail_stays_when_nothing_is_open() -> None:
    frame = _frame("attention", document=_nothing_open())
    rail = _rail(frame)
    assert rail[0].startswith("BUCKETS")
    assert len([row for row in rail[1:] if row.strip() and "↳" not in row]) == 8
    assert any(row.startswith(" NOTHING YET") for row in frame)


def test_j2_05_the_attention_strip_stays_at_80_columns_when_nothing_is_open() -> None:
    frame = _frame("attention", w=80, document=_nothing_open())
    strip = next(row for row in frame if row.startswith(" BUCKETS"))
    assert "▸all 0" in strip


def test_j2_05_a_chosen_bucket_is_marked_and_its_empty_list_says_so() -> None:
    frame = _frame("attention", bucket="failed")
    assert next(row for row in _rail(frame) if "▸" in row).startswith("▸failed")
    assert "nothing in this bucket needs you" in _text(frame)
    assert "ACT-0001" not in _text(frame)


def test_j2_06_open_items_group_under_their_bucket_and_sealed_ones_are_not_listed() -> None:
    frame = _frame("attention")
    assert any(row.startswith(" NEEDS OPERATOR  1") for row in frame)
    assert "ACT-0001" in _text(frame)
    assert "ACT-0002" not in _text(frame)
    for section in ("MINE", "ALL PRINCIPALS", "SEALED"):
        assert not [row for row in frame if row.startswith(f" {section}  ")]


def test_j2_06_the_selected_row_names_its_owner_on_the_detail_line() -> None:
    frame = _frame("attention")
    at = next(i for i, row in enumerate(frame) if "▸ ACT-0001" in row)
    assert "decision · you are the only eligible answer" in frame[at + 1]


# ---------- J2-07: the notifications card ----------


@pytest.mark.parametrize("w", [80, 160])
def test_j2_07_notifications_draws_the_boxed_card_from_a_held_register(w: int) -> None:
    frame = _frame("notifications", w=w)
    text = _text(frame)
    assert any(row.startswith("┌─ NOTIFICATIONS · what may interrupt") for row in frame)
    assert "needs permission" in text and "needs_permission" not in text
    assert "RUN-00000001" not in text
    assert frame[-1].split() == ["↑↓", "class", "Esc", "close"]


def test_j2_07_the_notifications_cursor_walks_the_classes() -> None:
    view = _view("notifications")
    view.session.sel = 9
    frame = render_route(view)
    assert view.session.sel == 4
    assert "│ ▸budget passed" in _text(frame)


# ---------- J2-09: backlog ----------


def test_j2_09_a_draft_says_what_promotion_still_needs() -> None:
    frame = _frame("backlog")
    row = next(row for row in frame if "TSK-0001" in row)
    assert "needs criteria · batch" in row
    assert "DRAFT" not in row


def test_j2_09_a_draft_holding_its_contract_is_ready_to_promote() -> None:
    spine = build_spine_view(_projection("backlog"))
    draft = next(row for row in spine.rows if row.key == "TSK-0001")
    ready = dataclasses.replace(draft, parent_key="BAT-0001", facts={"criteria": "2"})
    assert promotion_needs(ready) == "ready to promote"
    assert promotion_needs(draft) == "needs criteria · batch"


def test_j2_09_the_backlog_title_column_is_capped_so_status_sits_beside_it() -> None:
    frame = _frame("backlog")
    head = next(row for row in frame if row.startswith(" DRAFTS"))
    assert head.index("STATUS") < 110


# ---------- J2-11: release ----------


def _release_document() -> dict[str, Any]:
    return {
        **DOCUMENT,
        "release": {"REL-0007": _row("release", "REL-0007", "candidate")},
    }


def test_j2_11_with_no_release_cut_the_frame_says_so_and_lists_no_member() -> None:
    frame = _frame("release")
    text = _text(frame)
    assert "REL-0001" not in text
    assert frame[0].split("▸")[-1].split()[0] == "Release"
    assert "no release is cut" in frame[1]
    assert "MLS-0100" not in text
    head = next(row for row in frame if "MEMBERSHIP" in row)
    assert "TRACK" in head and "ACCEPTED" in head and "KIND" not in head
    publication = next(row for row in frame if row.startswith(" PUBLICATION"))
    assert "∅ nothing to publish · no release is cut" in publication
    assert "UNSTATED" not in text


def test_j2_11_a_cut_release_lists_members_and_its_publication_state() -> None:
    frame = _frame("release", document=_release_document())
    text = _text(frame)
    assert "REL-0007" in frame[0]
    assert frame[1].startswith(" Release REL-0007") and "CANDIDATE" in frame[1]
    member = next(row for row in frame if "MLS-0100" in row)
    assert "Cut the candidate" in member and "TRK-CORE" in member
    assert "Not started — nothing has been published." in text


def test_j2_11_an_unknown_readiness_signal_keeps_its_reason_whole() -> None:
    frame = _frame("release")
    row = next(row for row in frame if row.lstrip("▸ ").startswith("policy gate"))
    assert "…" not in row
    assert "no producer observes" in row


# ---------- J2-14: search ----------


def test_j2_14_an_empty_query_is_stated_once_in_the_sub_line() -> None:
    frame = _frame("search")
    assert frame[1].startswith(" no query · every record")
    assert "every record" not in "\n".join(frame[2:])


def test_j2_14_kinds_name_the_kind_before_its_count() -> None:
    frame = _frame("search")
    kinds = next(row for row in frame if row.startswith(" KINDS"))
    assert re.search(r"tracks 1 · milestones 2", kinds)


# ---------- J2-15: the card routes ----------


@pytest.mark.parametrize(
    ("route", "leaf", "title"),
    [
        ("campaign.step", "Step", "STEP"),
        ("campaign.artifact", "Artifact", "ARTIFACT"),
        ("evidence.digest", "Rung", "RUNG"),
    ],
)
def test_j2_15_a_card_route_holding_no_record_is_still_a_card(
    route: str, leaf: str, title: str
) -> None:
    frame = _frame(route, document={})
    text = _text(frame)
    assert frame[0].split("▸")[-1].split()[0] == leaf
    assert route not in frame[0]
    assert any(row.startswith(f"┌─ {title} ") for row in frame)
    assert "ROW" not in text and "WINDOW" not in text
    assert all(re.match(r"^│ [A-Z ]+ {2,}", row) for row in frame if "no producer" in row)


# ---------- J2-18: the readiness matrix ----------


def test_j2_18_the_readiness_matrix_names_an_absent_release() -> None:
    view = _view("release")
    model = view.projection
    assert isinstance(model, ReleaseReadinessView)
    view.session.overlay = "readiness"
    frame = render_readiness(view, model)
    text = _text(frame)
    assert "readiness · no release" in frame[0]
    assert "? unknown · ? unknown" not in frame[0]
    assert re.search(r"^ STATE\s+no release is cut", text, re.MULTILINE)
    assert frame[0].count("no release") == 1
    assert "MLS-0100" not in text


# ---------- J2-19: campaign ----------


def test_j2_19_enter_on_a_campaign_route_holding_nothing_names_the_absence() -> None:
    session = Session()
    session.route = "campaign"
    ctx = Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        host=_Host(),
        w=160,
        h=40,
        projection=build_spine_view(_projection("campaign", {})),
    )
    dispatch(ctx, "Enter", False)
    assert session.route == "campaign"
    assert session.log[0].note == "nothing to open · no campaign is held"
