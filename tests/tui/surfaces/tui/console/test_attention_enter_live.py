"""CON-102 on the live path: Enter on a held Attention row opens its record kind's detail.

The rows are the Attention projection a link holds -- pending actions from the document,
provider permissions beside them -- over the packaged chrome, which holds no prototype
row. Each Enter opens the detail its record kind owns, and dismissing it writes nothing
and returns the caret to the row it was opened from.
"""

from __future__ import annotations

from typing import Any, Final

from eawf.kernel.projection.registers import build_register_view
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.decisions import DecisionRecords, QuestionRecord
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.session import Session
from tests.tui.surfaces.tui.console import test_native_route_bodies as bodies
from tests.tui.surfaces.tui.console.test_native_drills import _Host
from tests.tui.surfaces.tui.console.test_question_decision_card import _decision

#: The one open provider permission the register lists beside the pending actions.
PERMISSION: Final = "PRM-0001"

#: The probe tree with one waiting decision, one waiting action and one permission.
DOCUMENT: Final[dict[str, Any]] = {
    **bodies.DOCUMENT,
    "pending_action": {
        "ACT-0001": bodies._action("ACT-0001", "WAITING", "run/RUN-00000002"),
        "ACT-0004": bodies._action(
            "ACT-0004", "WAITING", "run/RUN-00000002", kind="protected_approval"
        ),
    },
    "permission": {
        PERMISSION: {
            "urn": f"{bodies.ROOT}/permission/{PERMISSION}",
            "revision": 1,
            "status": "open",
            "request_scope": "write src/pkg/loader.py",
        }
    },
}


class _Link:
    """A daemon link that records every verb handed to it."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def __call__(self, request: Any) -> bool:
        self.sent.append(request)
        return True


def _decisions() -> DecisionRecords:
    """Return the held options of the one waiting operator decision."""
    return DecisionRecords(questions=(_question("ACT-0001"),))


def _question(key: str) -> QuestionRecord:
    return QuestionRecord.of_decision(_decision(id=key, urn=f"{bodies.ROOT}/pending-action/{key}"))


def _press(session: Session, key: str, link: _Link) -> None:
    attention = bodies._projection("attention", DOCUMENT)
    ctx = Ctx(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        host=_Host(),
        w=120,
        h=30,
        attention=attention,
        principal=bodies.ME,
        decisions=_decisions(),
        send=link,
    )
    dispatch(ctx, key, False)


def _on(key: str) -> Session:
    session = Session()
    session.route = "attention"
    rows = build_register_view(bodies._projection("attention", DOCUMENT)).rows
    session.sel = [row.key for row in rows].index(key)
    session.sel_id = key
    return session


def test_con_102_live_enter_opens_the_detail_each_held_record_kind_owns() -> None:
    """A decision opens its question, an action and a permission their action detail."""
    opened = {}
    for key in ("ACT-0001", "ACT-0004", PERMISSION):
        session = _on(key)
        _press(session, "Enter", _Link())
        opened[key] = (session.overlay, session.ov_subject)
    assert opened == {
        "ACT-0001": ("question", "ACT-0001"),
        "ACT-0004": ("consequence", "ACT-0004"),
        PERMISSION: ("consequence", PERMISSION),
    }


def test_con_102_live_dismissing_a_detail_writes_nothing_and_returns_to_the_row() -> None:
    """Esc closes the detail, sends nothing, and leaves the caret on the invoking row."""
    for key in ("ACT-0001", "ACT-0004", PERMISSION):
        session, link = _on(key), _Link()
        sel = session.sel
        _press(session, "Enter", link)
        assert session.overlay is not None
        _press(session, "Escape", link)
        assert (session.overlay, session.sel, session.sel_id) == (None, sel, key)
        assert link.sent == []
