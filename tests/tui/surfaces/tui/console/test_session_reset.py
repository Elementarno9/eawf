"""The console session refuses extra fields, and ``reset(setup)`` is its one reset.

Every declared field is dirtied and must come back to its default, the golden setups
validate and apply, and the reset agrees with the test-only chassis while both exist.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.session import (
    BACK_CAP,
    LOG_CAP,
    SIZES,
    BackEntry,
    BackStack,
    LogEntry,
    Session,
    SessionSetup,
    Toast,
)
from eawf.surfaces.tui.console.tokens import Severity

TESTS_ROOT = Path(__file__).resolve().parents[4]
SEQUENCES = TESTS_ROOT / "fixtures" / "console" / "golden" / "sequences"
CHASSIS_SESSION = "tests.snapshots.tui.console.console_chassis.chassis.session"
SECTION_ORDER = ("general", "planning", "runtime")

# A non-default value for every declared field; a new field must be added here, which is
# the reset audit made structural.
DIRTY: dict[str, Any] = {
    "route": "activity",
    "sel": 4,
    "subj_id": "RUN-1",
    "sel_id": "RUN-2",
    "overlay": "help",
    "size": 2,
    "conn": "DEGRADED",
    "section": 3,
    "entry_sel": 2,
    "path_sel": 1,
    "pane_sel": 1,
    "scroll": 7,
    "back": BackStack(entries=[BackEntry(route="scope.home", sel=1, subj=None)]),
    "prefix": "g",
    "prefix_seq": 3,
    "prefix_deadline": 12.5,
    "filter": "jury",
    "filters": {"activity": "jury"},
    "bucket": "RUNNING",
    "pq": "roa",
    "pscroll": 2,
    "typing": True,
    "edit": {"kind": "text", "text": "x"},
    "evt": "EVT-2201",
    "reply": {"text": "ok"},
    "c_target": {"verb": "cancel"},
    "verb": "c",
    "home_track": 1,
    "home_ms": 2,
    "home_region": "outcomes",
    "home_sel": 3,
    "track_group": "CAMPAIGNS",
    "toasts": [Toast(title="copied", text="RUN-1", sev=Severity.OK, at=3.0)],
    "trace": "Enter → drill",
    "follow": False,
    "bound": {"id": "RUN-1"},
    "diff_pair": "41,150 → 41,208",
    "mark": 2,
    "timeline_marker": "MLS-0001",
    "timeline_marks": 4,
    "marker_card": {"id": "MLS-0001"},
    "rung": 3,
    "bl_draft": 1,
    "cam_sec": "ARTIFACTS",
    "artifact": 2,
    "art_scroll": 5,
    "cam_step": 1,
    "step_reg": "PRODUCTS",
    "hist_sel": 2,
    "tr_sel": 6,
    "tr_fold": {1: True},
    "tr_t0": 99.0,
    "rec_at": 98.0,
    "rec_seen": {"RUN-1": 1},
    "draft_field": 2,
    "draft_miss": ["owner"],
    "tr_pal": "tool",
    "bl_group": "DEFERRED",
    "tl_reg": "UNDATED",
    "tl_sel": 1,
    "tl_regs": {"UNDATED": []},
    "rel_reg": "GATES",
    "rel_sel": 2,
    "art_max": 9,
    "promote": {"id": "EAWF-0091", "missing": ["owner"]},
    "visible": 12,
    "count": 30,
    "ov_state": {"question": 1, "pause": 2, "evidence": 1, "readiness": 3},
    "log": [LogEntry(key="Enter", note="drill")],
    "record_facts": ["bundle"],
    "record_nav": ["BAT-0001", None],
    "absent_frame": True,
    "absent": True,
    "reserved": 2,
    "set_sec": 4,
    "set_key": 3,
    "set_filter": "vcs",
    "set_typing": True,
    "lens": "workspace",
    "last_esc": 7.0,
    "rack_last": 8.0,
    "keys": 40,
    "prefix_cancels": 2,
    "auto_opens": 1,
    "renders": 41,
}


def _dirty_session() -> Session:
    """Return a session with every declared field set away from its default."""
    session = Session()
    for name, value in copy.deepcopy(DIRTY).items():
        setattr(session, name, value)
    return session


def _golden_setups() -> Iterator[dict[str, Any]]:
    """Yield every ``setup`` block in the tracked frame and journey sequences."""

    def walk(node: object) -> Iterator[dict[str, Any]]:
        if isinstance(node, dict):
            if isinstance(node.get("setup"), dict):
                yield node["setup"]
            for child in node.values():
                yield from walk(child)
        elif isinstance(node, list):
            for child in node:
                yield from walk(child)

    for path in sorted(SEQUENCES.glob("*.json")):
        yield from walk(json.loads(path.read_text(encoding="utf-8")))


def test_session_extra_field_rejected_at_construction() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Session.model_validate({"route": "activity", "zoom": 2})


def test_session_extra_field_rejected_on_assignment() -> None:
    session = Session()
    with pytest.raises(ValidationError, match="no_such_attribute"):
        session.home_cursor = 3


@pytest.mark.parametrize(("name", "value"), [("sel", "first"), ("size", len(SIZES)), ("size", -1)])
def test_session_invalid_assignment_rejected(name: str, value: object) -> None:
    session = Session()
    with pytest.raises(ValidationError):
        setattr(session, name, value)


def test_session_setup_extra_key_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SessionSetup.model_validate({"route": "activity", "verbose": True})


def test_session_setup_size_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionSetup.model_validate({"size": len(SIZES)})


def test_session_dirty_table_covers_every_declared_field() -> None:
    assert set(DIRTY) == set(Session.model_fields)
    defaults = Session()
    unchanged = sorted(n for n, v in DIRTY.items() if v == getattr(defaults, n))
    assert unchanged == []


def test_session_reset_restores_every_remembered_cursor() -> None:
    session = _dirty_session()
    held_back = session.back
    session.reset(None, now=0.0)
    defaults = Session()
    changed = sorted(n for n in Session.model_fields if getattr(session, n) != getattr(defaults, n))
    assert changed == []
    assert session.back is not held_back
    assert len(session.back) == 0


def test_session_reset_starts_the_rack_clock_at_now() -> None:
    session = _dirty_session()
    session.reset(None, now=1234.5)
    assert session.rack_last == pytest.approx(1234.5)
    assert session.last_esc == pytest.approx(0.0)


def test_session_reset_applies_the_setup_by_golden_key() -> None:
    session = _dirty_session()
    setup = SessionSetup.model_validate(
        {
            "route": "run.detail",
            "sel": 2,
            "subjId": "RUN-538453eb",
            "overlay": "palette",
            "size": 1,
            "conn": "OFFLINE SNAPSHOT",
            "section": 3,
            "entrySel": 4,
            "prefix": "g",
        }
    )
    session.reset(setup, settings_section_order=SECTION_ORDER, now=5.0)
    assert (session.route, session.sel, session.subj_id) == ("run.detail", 2, "RUN-538453eb")
    assert (session.overlay, session.size, session.conn) == ("palette", 1, "OFFLINE SNAPSHOT")
    assert (session.section, session.entry_sel, session.prefix) == (3, 4, "g")
    assert session.sel_id is None
    assert session.set_sec == 1
    assert (session.w, session.h) == SIZES[1]


def test_session_reset_falsy_setup_values_fall_back_to_defaults() -> None:
    session = _dirty_session()
    setup = SessionSetup.model_validate({"route": "", "subjId": None, "overlay": "", "conn": ""})
    session.reset(setup)
    assert (session.route, session.subj_id, session.overlay, session.conn) == (
        "scope.home",
        None,
        None,
        "LIVE",
    )


@pytest.mark.parametrize(
    ("order", "expected"),
    [((), 0), (("planning",), 0), (SECTION_ORDER, 1), (("general", "runtime"), 0)],
)
def test_session_reset_settings_rail_opens_on_planning(
    order: tuple[str, ...], expected: int
) -> None:
    session = _dirty_session()
    session.reset(None, settings_section_order=order)
    assert session.set_sec == expected


def test_session_reset_twice_is_idempotent() -> None:
    session = _dirty_session()
    setup = SessionSetup(route="backlog", sel=1)
    session.reset(setup, settings_section_order=SECTION_ORDER, now=2.0)
    once = session.model_dump()
    session.reset(setup, settings_section_order=SECTION_ORDER, now=2.0)
    assert session.model_dump() == once


def test_session_reset_every_golden_setup_validates_and_applies() -> None:
    setups = list(_golden_setups())
    assert setups
    for raw in setups:
        session = _dirty_session()
        session.reset(SessionSetup.model_validate(raw))
        assert session.route == (raw.get("route") or "scope.home")
        assert session.subj_id == (raw.get("subjId") or None)
        assert session.size == raw.get("size", 0)
        assert session.projection()["back_depth"] == 0


def test_session_projection_names_the_nine_compared_keys() -> None:
    session = Session()
    session.back.push(route="activity", sel=1, subj=None)
    projection = session.projection()
    assert projection == {
        "route": "scope.home",
        "overlay": None,
        "sel": 0,
        "subjId": None,
        "conn": "LIVE",
        "bucket": None,
        "back_depth": 1,
        "toasts": 0,
        "mark": 0,
    }


@pytest.mark.parametrize("size", range(len(SIZES)))
def test_session_w_h_follow_the_size_index(size: int) -> None:
    session = Session(size=size)
    assert (session.w, session.h) == SIZES[size]


def test_back_stack_push_caps_at_back_cap() -> None:
    stack = BackStack()
    for sel in range(BACK_CAP + 1):
        stack.push(route="activity", sel=sel, subj=None)
    assert len(stack) == BACK_CAP
    assert stack.items()[0].sel == 1
    top = stack.pop()
    assert top is not None and top.sel == BACK_CAP


def test_back_stack_pop_on_empty_returns_none() -> None:
    stack = BackStack()
    assert not stack
    assert stack.pop() is None
    stack.push(route="track", sel=0, subj="TRK-0001")
    assert stack
    stack.clear()
    assert len(stack) == 0


def test_back_stack_items_is_a_copy() -> None:
    stack = BackStack()
    stack.push(route="track", sel=0, subj=None)
    stack.items().clear()
    assert len(stack) == 1


def test_session_log_key_keeps_the_newest_entries_first() -> None:
    session = Session()
    for n in range(LOG_CAP + 1):
        session.log_key(f"k{n}", f"note {n}")
    assert len(session.log) == LOG_CAP
    assert session.log[0] == LogEntry(key=f"k{LOG_CAP}", note=f"note {LOG_CAP}")
    assert session.trace == f"k{LOG_CAP} → note {LOG_CAP}"
    session.log_key("Enter")
    assert session.trace == "Enter → claimed, no note"


def test_session_noop_is_silent_unless_verbose() -> None:
    session = Session()
    session.noop("q", verbose=False)
    assert session.log == []
    assert session.trace == "q → unclaimed"
    session.noop("q", verbose=True)
    assert session.log == [LogEntry(key="q", note="unclaimed · no handler on this route")]


def test_session_disarm_quit_forgets_a_pending_escape() -> None:
    session = Session(last_esc=3.0)
    session.disarm_quit()
    assert session.last_esc == pytest.approx(0.0)


def test_session_reset_matches_the_test_only_chassis() -> None:
    # parity holds only while both copies exist; the skip marks the chassis' removal
    theirs = pytest.importorskip(CHASSIS_SESSION, exc_type=ModuleNotFoundError)
    outside = {"verbose", "rack_hold", "simulator"}
    chassis_fields = set(theirs.Session.__dataclass_fields__)
    assert set(Session.model_fields) == chassis_fields - outside
    # the chassis reset spares its counters and leaves the rack clock to the app
    spared = {"keys", "prefix_cancels", "auto_opens", "renders", "rack_last"}
    for raw in [{}, *_golden_setups()]:
        ours = _dirty_session()
        ours.reset(SessionSetup.model_validate(raw), settings_section_order=SECTION_ORDER)
        chassis = theirs.Session()
        chassis.reset(raw, SECTION_ORDER)
        for name in sorted(set(Session.model_fields) - spared):
            mine, other = getattr(ours, name), getattr(chassis, name)
            if name in {"back", "toasts", "log"}:
                assert (len(mine), len(other)) == (0, 0), name
            else:
                assert mine == other, (name, raw)
