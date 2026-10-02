"""History lists the change feed the daemon serves, and Enter opens the diff of a row.

The ledger is drawn from the feed's newest page: one row per record a commit changed,
naming its fields, the revision it left, who asked and when. A feed that holds nothing
says since when nothing was recorded, and one not read yet says that instead; neither
says there is no change feed. The live reads that feed both routes address the whole tree
for History and the diff's subject for the diff.
"""

from __future__ import annotations

from typing import Any

from eawf.kernel.store.changes import ChangePage, StoredValue
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.live_reads import (
    HISTORY_DIFF_READ,
    HISTORY_READ,
    LIVE_READS,
    diff_subject,
)
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session
from tests.tui.surfaces.tui.console.overlay_support import Host
from tests.tui.surfaces.tui.console.test_spine_frames import _model, _session


def _record(key: str, sequence: int, actor: str | None = "OP-0001", **fields: Any) -> dict:
    """Return one change record of Task *key*, each field mapped to ``(before, after)``."""
    return {
        "change_id": f"evt-{sequence}:task:{key}",
        "tier": "committed",
        "collection": "task",
        "record_key": key,
        "event_name": "domain.task.planned",
        "revision_before": 1,
        "revision_after": 2,
        "canonical_sequence": sequence,
        "actor_ref": actor,
        "recorded_at": "2026-10-02T12:34:00Z",
        "changes": [
            {
                "field": name,
                "before": StoredValue.of(then).model_dump(mode="json"),
                "after": StoredValue.of(now).model_dump(mode="json"),
            }
            for name, (then, now) in sorted(fields.items())
        ],
    }


def _page(*records: dict[str, Any]) -> ChangePage:
    return ChangePage.model_validate({"changes": list(records)})


def _view(session: Session, live: dict[str, object], w: int = 120) -> View:
    return View(
        session=session,
        fixture=Fixture.from_chrome(load_chrome()),
        w=w,
        h=dict(SIZES)[w],
        projection=_model("history"),
        live=live,
    )


def _frame(live: dict[str, object], session: Session | None = None, w: int = 120) -> list[str]:
    frame = render_route(_view(session or _session("history", None), live, w))
    assert len(frame) == dict(SIZES)[w]
    return frame


FEED = _page(
    _record("TSK-0003", 12, status=("DRAFT", "PLANNED"), batch_ref=(None, "BAT-0101")),
    _record("TSK-0001", 11, actor=None, status=("PLANNED", "CLAIMED")),
)


def test_history_lists_each_change_newest_first() -> None:
    frame = _frame({HISTORY_READ: FEED})
    rows = [row for row in frame if "TSK-000" in row]
    assert rows[0].lstrip(" ▸").startswith("TSK-0003 batch_ref, status")
    assert "rev 2" in rows[0] and "OP-0001" in rows[0] and "Oct 2 12:34" in rows[0]
    assert rows[1].lstrip().startswith("TSK-0001 status")
    assert "no change feed" not in "\n".join(frame)


def test_the_source_pane_names_the_change_under_the_caret() -> None:
    frame = _frame({HISTORY_READ: FEED})
    source = next(row for row in frame if row.startswith(" SOURCE"))
    assert "domain.task.planned" in source and "OP-0001" in source and "sequence 12" in source
    assert "2 fields changed" in frame[frame.index(source) + 1]
    assert frame[-1].split()[-2:] == ["Esc", "back"]
    assert "Enter" in frame[-1].split()


def test_a_change_naming_nobody_is_never_given_an_actor() -> None:
    frame = _frame({HISTORY_READ: FEED})
    row = next(row for row in frame if "TSK-0001 status" in row)
    assert "unknown" in row and "system" not in row


def test_an_empty_feed_says_since_when_nothing_was_recorded() -> None:
    frame = _frame({HISTORY_READ: _page()})
    assert any("no changes recorded since 2026-10-02" in row for row in frame)
    assert frame[-1].split() == ["Esc", "back"]


def test_enter_opens_the_diff_of_the_change_under_the_caret() -> None:
    session = _session("history", None)
    live: dict[str, object] = {HISTORY_READ: FEED}
    model = _model("history")
    for key in ("ArrowDown", "Enter"):
        compose_frame(_view(session, live))
        ctx = Ctx(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            host=Host(),
            w=120,
            h=dict(SIZES)[120],
            projection=model,
        )
        dispatch(ctx, key, False)
    assert session.route == "history.diff"
    assert session.subj_id == "TSK-0001"


def test_history_reads_the_tree_and_the_diff_reads_its_subject() -> None:
    assert LIVE_READS[HISTORY_READ].routes == frozenset({"history"})
    assert LIVE_READS[HISTORY_DIFF_READ].routes == frozenset({"history.diff"})
    assert diff_subject("TSK-0001", ["TRK-CORE"]) == "TSK-0001"
    assert diff_subject(None, ["TRK-CORE", "MLS-0100"]) == "TRK-CORE"
    assert diff_subject("not a key", []) is None
    assert diff_subject(None, []) is None
