"""The per-route key audit: one meaning per letter, one primary mutation, safe verbs stay safe.

Each test names the console requirement row it proves. The audit runs over the route
tables of :mod:`~eawf.surfaces.tui.console.keybar`, the one source every keybar, the
refusal gate and help read; the table module also runs it when it loads, so a colliding
table never reaches a frame. The behavioural legs press keys through the production
dispatcher over the tracked registers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import attention as att
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.keybar import (
    KEY,
    RESERVED,
    ROUTE_KEYS,
    KeyEntry,
    KeyKind,
    collision_audit,
)
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.operations import ANSWER_OPTIONS, RUN_CONTROLS, VerbRequest
from eawf.surfaces.tui.console.session import Session

ROUTES = sorted(ROUTE_KEYS)


class _Host:
    """The dispatcher's host: a held clock and a quit that records it was asked."""

    def __init__(self) -> None:
        self.quits = 0
        self._clock = FakeClock()

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self._clock

    def quit(self) -> None:
        """Record that the console was asked to end."""
        self.quits += 1


def _fixture() -> Fixture:
    root = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"
    return load_fixture(root)


FIXTURE = _fixture()


def _session(route: str, subj: str | None = None) -> Session:
    session = Session()
    session.route = route
    session.subj_id = subj
    compose_frame(View(session=session, fixture=FIXTURE, w=120, h=30))
    return session


def _press(session: Session, *keys: str) -> None:
    for key in keys:
        dispatch(Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30), key, False)
        compose_frame(View(session=session, fixture=FIXTURE, w=120, h=30))


def _recorder(sent: list[VerbRequest]) -> Callable[[VerbRequest], bool]:
    """Return a daemon link that records every verb handed to it and takes each one."""

    def send(request: VerbRequest) -> bool:
        sent.append(request)
        return True

    return send


def audit_table() -> dict[str, dict[str, str]]:
    """Return route → single key → meaning, the table the collision audit reads."""
    table: dict[str, dict[str, str]] = {}
    for route in ROUTES:
        row = table.setdefault(route, {})
        for entry in ROUTE_KEYS[route]:
            for key in entry.keys:
                if len(key) == 1:
                    row.setdefault(key, entry.label)
    return table


# ---------- CON-042: no letter carries two meanings inside one route ----------


def test_con042_the_route_tables_audit_clean() -> None:
    """CON-042: the tables the console ships hold no collision and no reserved-key misuse."""
    assert collision_audit(ROUTE_KEYS) == ()


@pytest.mark.parametrize("route", ROUTES)
def test_con042_each_route_binds_every_single_key_once(route: str) -> None:
    """CON-042: the per-route audit table has one meaning for each letter the route binds."""
    labels: dict[str, set[str]] = {}
    for entry in ROUTE_KEYS[route]:
        for key in entry.keys:
            if len(key) == 1:
                labels.setdefault(key, set()).add(entry.label)
    assert all(len(meanings) == 1 for meanings in labels.values()), labels


@pytest.mark.parametrize("route", ROUTES)
def test_con042_a_reserved_key_keeps_its_global_meaning(route: str) -> None:
    """CON-042: ``g / \\ . ? ! i r y Y - u [ ]`` are bound only through the shared vocabulary."""
    shared = set(KEY.values())
    for entry in ROUTE_KEYS[route]:
        if set(entry.keys) & RESERVED:
            assert entry in shared, (route, entry)


def test_con042_reused_letters_carry_the_meaning_their_route_can_hold() -> None:
    """CON-042: ``m`` is readiness on release and conflict on git/PR; ``a`` answers on attention."""
    table = audit_table()
    assert table["release"]["m"] == "readiness"
    assert table["git.pr"]["m"] == "conflict"
    assert table["attention"]["a"] == "answer"
    assert "a" not in table["release"]
    assert "a" not in table["milestone"]


def test_con042_two_meanings_for_one_letter_is_a_finding() -> None:
    """CON-042: the audit names the route, the letter and both meanings."""
    tables = {"x": (KeyEntry("sort", ("s",)), KeyEntry("steer", ("s",)))}
    assert collision_audit(tables) == ("x: 's' means both 'sort' and 'steer'",)


@pytest.mark.parametrize("key", sorted(RESERVED - {"!"}))
def test_con042_a_reserved_key_bound_to_a_local_verb_is_a_finding(key: str) -> None:
    """CON-042: a route wanting a verb takes an unclaimed letter, never a global one."""
    tables = {"x": (KeyEntry("local verb", (key,)),)}
    assert collision_audit(tables) == (f"x: reserved key {key!r} bound to 'local verb'",)


def test_con042_named_keys_may_follow_the_state_and_an_empty_table_is_clean() -> None:
    """CON-042: Enter or the arrows may relabel with the state; an empty table has no finding."""
    tables = {"x": (KeyEntry("line", ("ArrowUp", "ArrowDown")), KeyEntry("product", ("ArrowUp",)))}
    assert collision_audit(tables) == ()
    assert collision_audit({}) == ()
    assert collision_audit({"x": ()}) == ()


# ---------- CON-043: at most one primary mutation key, and it previews first ----------


def _primaries(route: str) -> list[KeyEntry]:
    return [entry for entry in ROUTE_KEYS[route] if entry.kind is KeyKind.PRIMARY]


_TWO_PRIMARIES = pytest.mark.xfail(
    strict=True,
    reason="the pack's unattended frame binds request pause and request drain directly; "
    "one of them belongs in the action menu and needs a ruling",
)


@pytest.mark.parametrize(
    "route",
    [pytest.param(r, marks=_TWO_PRIMARIES) if r == "unattended" else r for r in ROUTES],
)
def test_con043_a_route_binds_at_most_one_primary_mutation_key(route: str) -> None:
    """CON-043: each route has at most one direct mutation key."""
    assert len(_primaries(route)) <= 1


_NO_PREVIEW = pytest.mark.xfail(
    strict=True,
    reason="settings x unsets at once in the prototype registers and the native view; "
    "its preview card needs a ruling",
)


@pytest.mark.parametrize(
    ("route", "subj", "key"),
    [
        ("attention", None, "v"),
        ("unattended", None, "a"),
        ("unattended", None, "d"),
        pytest.param("settings", None, "x", marks=_NO_PREVIEW),
    ],
)
def test_con043_the_primary_key_previews_or_refuses_and_never_writes(
    route: str, subj: str | None, key: str
) -> None:
    """CON-043: a primary key opens the preview, or refuses with its reason; it never writes."""
    assert key in {k for entry in _primaries(route) for k in entry.keys}
    session = _session(route, subj)
    if route == "attention":
        session.sel = 1
    sent: list[VerbRequest] = []
    ctx = Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30, send=_recorder(sent))
    dispatch(ctx, key, False)
    assert sent == []
    assert session.overlay == "consequence" or "unavailable" in (session.trace or "")


# ---------- CON-044: answers resolve a question or disposition, no lifecycle ----------


def test_con044_answer_verbs_are_answers_and_never_run_controls() -> None:
    """CON-044: an answer key is sent as an answer option, never as a Run control."""
    answers = [e for table in ROUTE_KEYS.values() for e in table if e.kind is KeyKind.ANSWER]
    assert {e.label for e in answers} == {"answer", "deny", "snooze"}
    for entry in answers:
        assert entry.label not in RUN_CONTROLS
    assert set(ANSWER_OPTIONS) <= {e.label for e in answers}


@pytest.mark.parametrize("route", ROUTES)
def test_con044_no_route_carries_a_second_key_for_one_answer(route: str) -> None:
    """CON-044: one key per answer verb on a route; ``a`` dispatches on the item's kind."""
    labels = [e.label for e in ROUTE_KEYS[route] if e.kind is KeyKind.ANSWER]
    assert len(labels) == len(set(labels))


def _attention_at(kind: str) -> Session:
    session = _session("attention")
    rows = att.attn_list(session, FIXTURE)
    session.sel = next(i for i, row in enumerate(rows) if kind in row.kind)
    return session


@pytest.mark.parametrize(
    ("kind", "card"),
    [("question", "question"), ("readiness", "readiness"), ("permission", "consequence")],
)
def test_con044_answer_opens_the_card_the_item_kind_needs(kind: str, card: str) -> None:
    """CON-044: a question opens its options, readiness its matrix, a permission its card."""
    session = _attention_at(kind)
    _press(session, "a")
    assert session.overlay == card


def test_con044_answering_a_question_sends_nothing_until_an_option_is_chosen() -> None:
    """CON-044: the question card waits on a numbered option; opening it writes nothing."""
    session = _attention_at("question")
    sent: list[VerbRequest] = []
    ctx = Ctx(session=session, fixture=FIXTURE, host=_Host(), w=120, h=30, send=_recorder(sent))
    dispatch(ctx, "a", False)
    assert session.overlay == "question"
    assert sent == []


def test_con044_answer_kind_card_is_empty_for_other_kinds_and_no_row() -> None:
    """CON-044: every other kind, and no row at all, answers on the consequence preview."""
    assert att.answer_card(None) == ""
    session = _attention_at("permission")
    row = att.attn_row(session, FIXTURE)
    assert att.answer_card(row) == ""


# ---------- CON-045: the frequent safe verbs are local, reversible and unpreviewed ----------


def test_con045_safe_verbs_are_declared_safe() -> None:
    """CON-045: bounded raw and follow are the safe verbs the tables bind today."""
    safe = {e.label for table in ROUTE_KEYS.values() for e in table if e.kind is KeyKind.SAFE}
    assert safe == {"raw", "follow"}


def test_con045_raw_opens_without_a_preview_and_escape_puts_it_away() -> None:
    """CON-045: ``r`` opens the bounded raw drawer at once, with no card, and Esc closes it."""
    session = _session("run.detail")
    _press(session, "r")
    assert session.overlay == "raw"
    _press(session, "Escape")
    assert session.overlay is None
    assert session.route == "run.detail"


def test_con045_follow_toggles_the_view_and_claims_nothing_stopped() -> None:
    """CON-045: ``f`` follows the tail and ``f`` again holds; no card, no write."""
    session = _session("transcript", "RUN-538453eb")
    before = session.follow
    _press(session, "f")
    assert session.follow is not before
    assert session.overlay is None
    assert "stopped" not in (session.trace or "")
    _press(session, "f")
    assert session.follow is before
