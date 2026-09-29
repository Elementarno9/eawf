"""What a linked console states is the record it read, in the operator's words.

Each test is named for the confirmation-jury finding it closes. A Milestone's sealed
bundle and approval are read for that Milestone alone, through the seam, and drawn from
the live tree; ``y`` copies what the frame drew rather than a prototype literal; a line
under the header names the route's own subject instead of an unrelated register count;
the readiness remedy, the transcript reason and the absent cards say their prose whole;
a light verb names the place it opened and the record it is about; and a console whose
daemon answers nothing lands on the offline frame, filled from the tree's last commit.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eawf.kernel.projection.compute import KeyedPatch, PatchEntry
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.projection import read_milestone_acceptance
from eawf.surfaces.tui.console.action_menu import light_opening
from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
from eawf.surfaces.tui.console.attach import (
    NO_SNAPSHOT,
    OFFLINE,
    OfflineSnapshot,
    offline_snapshot,
    offline_state,
)
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.derive import NO_SCOPE_URN
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import KEY
from eawf.surfaces.tui.console.keymap import ENTRY_ROUTE
from eawf.surfaces.tui.console.renderers import copy_for, copy_target
from eawf.surfaces.tui.console.renderers.entry import render_state
from eawf.surfaces.tui.console.renderers.milestone import (
    NO_BUNDLE,
    NO_BUNDLE_CRITERIA,
    NO_STEP,
    short_digest,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.workflow.projection.acceptance import (
    MILESTONE_ACCEPTANCE_METHOD,
    RC_GATE_REASON,
    MilestoneAcceptanceRecord,
)

from . import decision_support as ds
from .overlay_support import chrome
from .test_console_live_smoke import (
    REPO_ROOT,
    authority_digests,
    live_console,
    render_setup,
    require_epoch2_repository,
)
from .test_enter_opened_cards import AT, DOCUMENT, SCOPE, _approval, _bundle

#: The Milestone of this repository that was accepted on a sealed approval, and what
#: that approval bound: the head the bundle was sealed over, as the frame abbreviates it,
#: and the operator who gave it.
ACCEPTED = "MLS-0100"
ACCEPTED_HEAD = "61c75ef"
ACCEPTED_BY = "OP-0001"

#: One Run, the record the operations and light-verb tests are about.
RUN = "RUN-00000005"
RUN_DOCUMENT: dict[str, Any] = {
    "run": {RUN: {"urn": f"urn:eawf:{SCOPE}:run:{RUN}", "revision": 1, "status": "RUNNING"}},
}


class _Clipped(ConsoleApp):
    """A console that is never run, whose clipboard takes a copy as a terminal's would."""

    def copy_text(self, text: str) -> bool:
        """Take ``text``; a console that is not running has no terminal of its own."""
        return bool(text)


def _linked(route: str, document: dict[str, Any], *, subject: str | None = None) -> ConsoleApp:
    """Return a console linked to a seam already holding ``route``'s projection."""
    seam = ProjectionSeam(
        route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT, scope_name="eawf"
    )
    seam._projection = ds.projection(route, document)
    app = _Clipped(chrome=load_chrome(), seam=seam, clock=FakeClock())
    app.session.route = route
    app.session.subj_id = subject
    return app


def _frame(app: ConsoleApp) -> list[str]:
    """Return the frame the app composes for its session, as its production path does."""
    return compose_frame(app.view())


class _Calls:
    """A binding call that answers an acceptance read per Milestone and records it."""

    def __init__(self, held: dict[str, MilestoneAcceptanceRecord]) -> None:
        self.held = held
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != MILESTONE_ACCEPTANCE_METHOD:
            raise ConnectionRefusedError(f"{method} is not served here")
        self.asked.append((method, params))
        key = params["milestone_key"]
        record = self.held.get(key, MilestoneAcceptanceRecord(milestone_key=key))
        return record.model_dump(mode="json")


# ---------- C1-02 = C2-13: the Milestone's bundle and approval, per subject ----------


def test_c1_02_the_seam_owes_an_acceptance_read_for_the_milestone_on_screen() -> None:
    """The read is owed for the subject alone, answered per key, and not owed twice."""
    seam = ProjectionSeam(route="milestone", scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = ds.projection("milestone", DOCUMENT)
    calls = _Calls({"MLS-0030": MilestoneAcceptanceRecord(milestone_key="MLS-0030")})
    seam._binding.call = calls  # type: ignore[method-assign]
    assert MILESTONE_ACCEPTANCE_METHOD not in seam.owed()
    seam.about("MLS-0030")
    assert MILESTONE_ACCEPTANCE_METHOD in seam.owed()
    asyncio.run(seam.sync())
    assert calls.asked == [(MILESTONE_ACCEPTANCE_METHOD, {"milestone_key": "MLS-0030"})]
    assert seam.acceptance_for("MLS-0030") is not None
    assert MILESTONE_ACCEPTANCE_METHOD not in seam.owed()
    seam.about("MLS-0031")
    assert MILESTONE_ACCEPTANCE_METHOD in seam.owed()
    assert seam.acceptance_for("MLS-0031") is None
    assert seam.acceptance_for(None) is None


def test_c1_02_a_moved_milestone_drops_the_read_so_it_is_asked_again() -> None:
    """A patch on a Milestone or a sealed question may move what it was accepted at."""
    seam = ProjectionSeam(route="milestone", scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = ds.projection("milestone", DOCUMENT)
    seam._acceptance["MLS-0030"] = MilestoneAcceptanceRecord(milestone_key="MLS-0030")
    seam.about("MLS-0030")
    patch = KeyedPatch(
        schema_version="1.0",
        projection_kind=seam.projection.header.projection_kind,  # type: ignore[union-attr]
        routes=("milestone",),
        scope_id=SCOPE,
        canonical_sequence=41209,
        entries=(
            PatchEntry(
                key="MLS-0030",
                urn=f"urn:eawf:{SCOPE}:milestone:MLS-0030",
                collection=Epoch2Collection.MILESTONE,
                revision=5,
                status="COMPLETED",
            ),
        ),
    )
    asyncio.run(seam.apply_patch(patch))
    assert seam.acceptance_for("MLS-0030") is None
    assert MILESTONE_ACCEPTANCE_METHOD in seam.owed()


def test_c1_02_the_view_draws_the_subjects_acceptance_and_never_anothers() -> None:
    """The production call site: route_view takes the record held for the subject."""
    app = _linked("milestone", DOCUMENT, subject="MLS-0030")
    assert app.seam is not None
    app.seam._acceptance["MLS-0030"] = MilestoneAcceptanceRecord(
        milestone_key="MLS-0030", bundle=_bundle(), approval=_approval()
    )
    rows = _frame(app)
    assert any(f"digest {short_digest(_bundle().digest())}" in row for row in rows)
    assert any("by OP-0001" in row for row in rows)
    app.session.subj_id = "MLS-0031"
    assert app.route_view().bundle_digest is None  # type: ignore[union-attr]


def test_c1_02_criteria_say_no_bundle_is_held_rather_than_that_it_lists_no_step() -> None:
    """With nothing sealed there is no held bundle for the criteria to be silent about."""
    rows = _frame(_linked("milestone", DOCUMENT, subject="MLS-0030"))
    text = "\n".join(rows)
    assert NO_BUNDLE in text
    assert NO_BUNDLE_CRITERIA in text
    assert NO_STEP not in text


def test_c1_02_an_acceptance_read_that_names_no_milestone_is_refused() -> None:
    """The error path: the verb validates its parameters before it touches a tree."""
    with pytest.raises(DaemonValidationError, match=r"milestone\.acceptance"):
        asyncio.run(read_milestone_acceptance(None, {}))  # type: ignore[arg-type]
    with pytest.raises(DaemonValidationError):
        asyncio.run(read_milestone_acceptance(None, {"milestone_key": ""}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="names the Milestone"):
        seam = ProjectionSeam(route="milestone", scope_id=SCOPE, state_path=None)
        asyncio.run(seam.load_acceptance())


def test_c1_02_the_live_tree_draws_the_accepted_milestones_bundle_and_approval(
    tmp_path: Path,
) -> None:
    """MLS-0100 was accepted on a sealed approval; the live frame says so, from the tree."""
    require_epoch2_repository()
    before = authority_digests(REPO_ROOT)

    async def body() -> tuple[str, str, MilestoneAcceptanceRecord | None]:
        async with (
            live_console(REPO_ROOT, tmp_path / "runtime") as (app, seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            setup = SessionSetup(route="milestone", subjId=ACCEPTED, size=2)
            await render_setup(app, pilot, setup)
            for _ in range(100):
                if seam.acceptance_for(ACCEPTED) is not None:
                    break
                await pilot.pause(0.05)
            text = await render_setup(app, pilot, setup)
            return text, copy_for(app._ctx()), seam.acceptance_for(ACCEPTED)

    text, copied, record = asyncio.run(body())
    assert record is not None and record.bundle is not None and record.approval is not None
    digest = record.bundle.digest()
    assert f"revision 1 · sealed 2026-09-28 00:58 · digest {short_digest(digest)}" in text
    assert f"head {ACCEPTED_HEAD} · tree" in text
    assert f"by {ACCEPTED_BY}" in text
    assert NO_BUNDLE not in text
    assert copied == f"digest {digest}"
    assert authority_digests(REPO_ROOT) == before, "the live serve wrote to the tree"


# ---------- C2-01: y copies what the frame drew ----------


def test_c2_01_y_copies_the_row_under_the_caret_then_the_subject() -> None:
    """A linked console never copies a prototype literal: the caret's row, else the subject."""
    session = Session()
    session.route = "run.detail"
    session.sel_id = "RUN-00000003"
    assert copy_target(session, chrome()) == "RUN-00000003"
    session.sel_id = None
    session.subj_id = RUN
    assert copy_target(session, chrome()) == RUN


def test_c2_01_with_nothing_selected_y_copies_the_address_in_the_attached_scope() -> None:
    """The empty boundary: no row and no subject say no URN is held, never ``?``.

    The copy is never a route address in a second scheme: a tree's URN is the one its
    held records are spelled under, and with none held there is none to copy.
    """
    session = Session()
    session.route = "notifications"
    copied = copy_target(session, chrome(), scope="eawf")
    assert copied == NO_SCOPE_URN
    assert "urn:eawf:" not in copied


def test_c2_01_the_urn_copy_is_the_urn_the_held_record_carries() -> None:
    """Shift-Y on a linked console copies the record's own address, not one spelled here."""
    app = _linked("run.detail", RUN_DOCUMENT, subject=RUN)
    dispatch(app._ctx(), "Y", False)
    assert app.session.toasts[-1].text == f"urn:eawf:{SCOPE}:run:{RUN}"


def test_c2_01_the_milestone_copies_the_digest_it_drew_and_offers_none_without_one() -> None:
    """The digest is read off the frame's bundle; with none, y is not offered as a digest."""
    app = _linked("milestone", DOCUMENT, subject="MLS-0030")
    assert app.seam is not None
    digest_key = KEY["digest"].pair()[0] + " " + KEY["digest"].pair()[1]
    assert digest_key not in _frame(app)[-1]
    assert copy_for(app._ctx()) == "MLS-0030"
    app.seam._acceptance["MLS-0030"] = MilestoneAcceptanceRecord(
        milestone_key="MLS-0030", bundle=_bundle(), approval=_approval()
    )
    assert digest_key in _frame(app)[-1]
    assert copy_for(app._ctx()) == f"digest {_bundle().digest()}"


def test_c2_01_y_inside_the_inspect_drawer_copies_the_run_on_screen() -> None:
    """The drawers copy through the same path, so no prototype Run id reaches the clipboard."""
    app = _linked("run.detail", RUN_DOCUMENT, subject=RUN)
    dispatch(app._ctx(), "i", False)
    dispatch(app._ctx(), "y", False)
    assert app.session.toasts[-1].text == RUN


# ---------- C1-13: the line under the header names the route's own subject ----------


@pytest.mark.parametrize(
    ("route", "subject", "says"),
    [
        ("notifications", None, "notification classes · what may interrupt you"),
        ("cost.ceiling", None, "spend against ceiling · observe only"),
        ("crash.recovery", None, "The console stopped · the daemon did not"),
        ("unattended", None, "1 run in it"),
        ("git.pr", "BAT-0007", "BAT-0007 · read only"),
    ],
)
def test_c1_13_the_summary_line_names_its_own_subject_not_a_register_count(
    route: str, subject: str | None, says: str
) -> None:
    """The count of the register a route reads is not what the route is about."""
    document = {
        **RUN_DOCUMENT,
        "batch": {
            "BAT-0007": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0007", "revision": 1, "status": "OK"}
        },
    }
    line = _frame(_linked(route, document, subject=subject))[1]
    assert says in line
    assert "1 batch" not in line
    if route != "unattended":
        assert "1 run" not in line


# ---------- C1-09: the readiness remedy is stated whole ----------


def test_c1_09_the_focused_signals_remedy_is_not_clipped_at_the_narrowest_width() -> None:
    """At 82 cells the table cuts its cell, so the REMEDY row carries the reason whole."""
    model = ds.release_view("candidate")
    session = ds.opened("release", "readiness", "REL-0001")
    session.sel = 2
    rows = ds.frame(session, projection=model, size=0)
    remedy = " ".join(r.strip() for r in rows if r.startswith(" REMEDY") or r.startswith("  "))
    assert "policy gate" in ds.row(rows, "REMEDY")
    assert RC_GATE_REASON in remedy


def test_c1_09_an_invalidated_approval_returns_the_release_to_draft_as_the_lifecycle_does() -> None:
    """The copy follows the transition table: APPROVED invalidates to DRAFT, never CANDIDATE."""
    from eawf.kernel.spec.release import ReleaseStatus
    from eawf.workflow.release.lifecycle import RELEASE_TRANSITIONS

    targets = {to for to, _guard in RELEASE_TRANSITIONS[ReleaseStatus.APPROVED]}
    assert ReleaseStatus.DRAFT in targets
    assert ReleaseStatus.CANDIDATE not in targets
    rows = ds.frame(
        ds.opened("release", "readiness", "REL-0001"), projection=ds.release_view("approved")
    )
    assert "returns to DRAFT" in ds.text(rows)


# ---------- C1-14: the article and the Esc verb of the absent cards ----------


def test_c1_14_the_artifact_card_takes_an_and_goes_back_as_the_step_card_does() -> None:
    """A noun starting with a vowel takes ``an``; both campaign cards say ``Esc back``."""
    for route in ("campaign.artifact", "campaign.step"):
        rows = _frame(_linked(route, {"campaign": {}, "artifact": {}}))
        text = "\n".join(rows)
        assert "a artifact" not in text
        assert "Esc back" in rows[-1]
        assert "Esc close" not in rows[-1]
    assert "an artifact is drawn" in "\n".join(_frame(_linked("campaign.artifact", {})))
    assert "a step is drawn" in "\n".join(_frame(_linked("campaign.step", {})))


# ---------- C1-16: the transcript reason wraps at the frame width ----------


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_c1_16_the_transcript_reason_is_whole_and_says_its_state_word_once(size: int) -> None:
    """The reason is wrapped rather than cut, and the word the STATE row printed is not repeated."""
    app = _linked("transcript", RUN_DOCUMENT, subject=RUN)
    app.session.size = size
    rows = _frame(app)
    at = next(i for i, row in enumerate(rows) if row.startswith(" STATE"))
    reason = []
    for row in rows[at + 1 :]:
        if not row.startswith("           ") or not row.strip():
            break
        reason.append(row.strip())
    assert reason, "a marked thinking cell states its reason"
    assert not reason[0].startswith("unknown ·")
    assert not any(line.endswith("…") for line in reason)
    thinking = app.route_view().thinking  # type: ignore[union-attr]
    assert " ".join(reason) == thinking.missing_reason


# ---------- C2-10: a light verb names the place it opened and its subject ----------


def test_c2_10_the_light_toast_is_the_place_in_words_and_the_subject() -> None:
    """Boundary: with no subject the toast is the place alone, never a route id."""
    menus = chrome().menus
    branch, cost = menus.verb("run.detail", "b"), menus.verb("run.detail", "m")
    assert branch is not None and cost is not None
    assert light_opening(branch, RUN, prototype=False) == (RUN, f"branch and PR · {RUN}")
    assert light_opening(cost, None, prototype=False) == (None, "cost ceiling")
    subject, _toast = light_opening(branch, RUN, prototype=True)
    assert subject is None, "the prototype replay opens the route unscoped, as the pack does"


@pytest.mark.parametrize(
    ("key", "route", "toast"),
    [("b", "git.pr", f"branch and PR · {RUN}"), ("m", "cost.ceiling", f"cost ceiling · {RUN}")],
)
def test_c2_10_a_light_verb_from_a_run_carries_the_run_and_names_the_place(
    key: str, route: str, toast: str
) -> None:
    """The production call site: the action menu's light verb, pressed on a Run."""
    app = _linked("run.detail", RUN_DOCUMENT, subject=RUN)
    dispatch(app._ctx(), ".", False)
    dispatch(app._ctx(), key, False)
    assert app.session.route == route
    assert app.session.subj_id == RUN
    assert app.session.toasts[-1].text == toast
    assert route not in app.session.toasts[-1].text.split(" · ")


def test_c2_10_the_git_surface_opened_on_a_run_is_about_the_runs_batch() -> None:
    """A Run subject is read through the Batch its record is filed under."""
    document = {
        "batch": {
            "BAT-0007": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0007", "revision": 1, "status": "OK"},
            "BAT-0008": {"urn": f"urn:eawf:{SCOPE}:batch:BAT-0008", "revision": 1, "status": "OK"},
        },
    }
    app = _linked("git.pr", document, subject=RUN)
    run_row = ds.projection("run.detail", RUN_DOCUMENT).rows[0]
    filed = run_row.model_copy(update={"facts": {"batch": "BAT-0008"}})
    assert "BAT-0008 · read only" in compose_frame(replace(app.view(), rows=(filed,)))[1]
    unfiled = run_row.model_copy(update={"facts": {}})
    line = compose_frame(replace(app.view(), rows=(unfiled,)))[1]
    assert "no Batch · read only" in line, "an unstated Batch is not borrowed from the rows"


# ---------- C1-10: the offline entry frame ----------


def test_c1_10_with_no_snapshot_the_offline_frame_says_none_is_held() -> None:
    """The empty boundary: no rows are drawn and the absence is said."""
    state = offline_state(load_chrome(), None)
    assert state.id == OFFLINE
    assert not state.rows
    assert state.tail == (NO_SNAPSHOT,)


def test_c1_10_the_offline_frame_lists_this_trees_tracks_from_its_last_commit() -> None:
    """One row per Track of the committed document, as of when it was committed."""
    require_epoch2_repository()
    authority = resolve_authority(REPO_ROOT / ".ea")
    snapshot = offline_snapshot(authority, scope_id=SCOPE, now=AT)
    assert isinstance(snapshot, OfflineSnapshot)
    tracks = [r for r in snapshot.projection.rows if r.collection is Epoch2Collection.TRACK]
    state = offline_state(load_chrome(), snapshot)
    assert state.rows is not None and len(state.rows) == len(tracks) >= 1
    assert all(row[2] == "?" for row in state.rows), "what needs you is not read offline"
    frame = render_state(
        View(session=Session(), fixture=chrome(), w=SIZES[0][0], h=SIZES[0][1]), state
    )
    assert any((tracks[0].title or tracks[0].key)[:20] in row for row in frame)
    assert NO_SNAPSHOT not in "\n".join(frame)


def test_c1_10_an_unreadable_generation_reads_as_no_snapshot() -> None:
    """A tree naming no generation, or one whose document is gone, gives no snapshot."""
    require_epoch2_repository()
    authority = resolve_authority(REPO_ROOT / ".ea")
    unnamed = SimpleNamespace(target=None, generation_id=None)
    missing = SimpleNamespace(target=authority.target, generation_id="gen-0000000000000000")
    assert offline_snapshot(unnamed, scope_id=SCOPE, now=AT) is None  # type: ignore[arg-type]
    assert offline_snapshot(missing, scope_id=SCOPE, now=AT) is None  # type: ignore[arg-type]


def test_c1_10_a_console_whose_daemon_answers_nothing_lands_on_the_offline_frame() -> None:
    """The production call site: a first sync that reads nothing opens the offline state."""

    async def refused(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionRefusedError("no daemon")

    seam = ProjectionSeam(route="scope.home", scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._binding.call = refused  # type: ignore[method-assign]
    chrome_ = load_chrome()
    offline = next(i for i, state in enumerate(chrome_.entry) if state.id == OFFLINE)
    app = ConsoleApp(chrome=chrome_, seam=seam, clock=FakeClock())

    async def body() -> tuple[str, int]:
        async with app.run_test(size=SIZES[0]) as pilot:
            for _ in range(100):
                if app.session.route == ENTRY_ROUTE:
                    break
                await pilot.pause(0.05)
            return app.session.route, app.session.entry_sel

    route, entry_sel = asyncio.run(body())
    assert route == ENTRY_ROUTE
    assert entry_sel == offline
