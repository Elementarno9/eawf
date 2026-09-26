"""The header's ``!N``, Activity's buckets, and the epoch-1 footer the native mode must not cost.

Three things meet on the register routes. The header prints an attention count on every
frame and the Attention route prints its own ``mine`` row, and the two must be one number:
a console that showed ``!3`` in the corner and two rows under the cursor would be telling
an operator two different things about the same register. :func:`needs_count` is where
that number is produced, and both readers go through it.

Activity's buckets are the second. A bucket count used to be a count of prototype rows,
which made it true of a file rather than of the workspace; it is now derived from the rows
``projection.activity.read`` served, bucketed by the status each row states. A row that
states no status falls in no bucket and is reported apart, so the buckets never add up to
more than the rows that stated one.

The third is the cost of the first two. The console still opens against the prototype
registers, and the tracked golden contract replays that mode, so every key the footer
advertises on a register route must still resolve there. A footer advertising a key
nothing handles is a frame promising an action it does not have.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    REGISTER_ROUTES,
    UNWRITTEN_REASON,
    RegisterView,
    attention_mine,
    build_register_view,
)
from eawf.kernel.projection.spine import build_spine_view
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.store.compaction import compact_terminal_record, document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS
from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.clock import Clock, FakeClock
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View, needs_count
from eawf.surfaces.tui.console.keybar import KEY_NAMES
from eawf.surfaces.tui.console.keymap import route_keys
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays import render_overlay
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.launch import repair_lines, with_repair_lines
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import AT as TXN_AT
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

#: When the probe projections are stamped. The digest does not cover the stamp; a fixed
#: clock only keeps this suite's output reproducible.
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

#: The scope every probe projection is built for.
SCOPE = "EAWF"

#: Four Runs in three statuses, so a bucket holding two is told from one holding one and
#: the statuses are not a permutation of the keys.
RUNS: dict[str, Any] = {
    "RUN-0000000a": {"urn": f"{SCOPE}/run/RUN-0000000a", "revision": 1, "status": "RUNNING"},
    "RUN-0000000b": {"urn": f"{SCOPE}/run/RUN-0000000b", "revision": 2, "status": "RUNNING"},
    "RUN-0000000c": {"urn": f"{SCOPE}/run/RUN-0000000c", "revision": 1, "status": "BLOCKED"},
    "RUN-0000000d": {"urn": f"{SCOPE}/run/RUN-0000000d", "revision": 5, "status": "CANCELLED"},
}

DOCUMENT: dict[str, Any] = {"run": RUNS}


def _projection(route: str, *, cursor: int = 41208, document: Any = None) -> RouteProjection:
    """Return one route's projection over the probe document at ``cursor``."""
    return build_route_projection(
        route=route,
        document=DOCUMENT if document is None else document,
        cursor=cursor,
        scope_id=SCOPE,
        generated_at=AT,
    )


def _register(route: str, **kwargs: Any) -> RegisterView:
    """Return the register read model of ``route`` over the probe document."""
    return build_register_view(_projection(route, **kwargs))


def _fixture() -> Fixture:
    """Return the tracked console fixture the epoch-1 mode draws."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _written_attention(rows: int) -> RegisterView:
    """Return an Attention register as it reads once a producer writes it.

    The pending-action producer has not shipped, so the shipped register is withheld.
    Stating one by hand is how the count's wiring is checked on both sides of that
    arrival: the day the producer lands, the header must already be reading this.
    """
    source = _register("activity")
    return dataclasses.replace(
        source,
        route=ATTENTION_ROUTE,
        read_model=source.read_model,
        rows=source.rows[:rows],
        counts={"pending_action": rows},
        withheld=(),
    )


def _view(register: RegisterView | None, *, route: str, attention: RegisterView | None) -> View:
    """Return a render view on ``route`` holding ``register`` and ``attention``."""
    session = Session()
    session.route = route
    return View(
        session=session,
        fixture=_fixture(),
        w=120,
        h=24,
        register=register,
        attention=attention,
    )


# ---------- the four register routes, and the verbs that serve them ----------


def test_register_routes_are_the_four_the_console_draws_from_a_register() -> None:
    """The register table names exactly the routes a register read model is stated for."""
    assert REGISTER_ROUTES == ("activity", "attention", "cost.ceiling", "notifications")


@pytest.mark.parametrize("route", REGISTER_ROUTES)
def test_every_register_route_is_served_by_its_own_read_verb(route: str) -> None:
    """A route the console draws natively is one the daemon reads at a cursor."""
    assert READ_METHOD_TEMPLATE.format(route=route) in ROUTE_READ_METHODS
    assert ROUTE_COLLECTIONS[route]


# ---------- the header count and the attention mine count are one number ----------


def test_the_header_count_equals_the_attention_register_mine_count() -> None:
    """One producer states both, so the corner and the route cannot disagree."""
    register = _written_attention(3)
    mine = attention_mine(register)
    view = _view(register, route=ATTENTION_ROUTE, attention=register)
    assert mine.state is TruthState.KNOWN
    assert needs_count(view) == int(mine.value or "")
    assert needs_count(view) == 3


def test_the_header_prints_the_badge_the_attention_register_states() -> None:
    """The count reaches the drawn header row, not just the function that made it."""
    register = _written_attention(2)
    view = _view(register, route=ATTENTION_ROUTE, attention=register)
    assert "!2 NEEDS YOU" in render_route(view)[0]


def test_the_header_count_travels_to_a_route_that_is_not_attention() -> None:
    """The badge is fleet-wide, so it is the attention register that states it everywhere."""
    attention = _written_attention(2)
    view = _view(_register("activity"), route="activity", attention=attention)
    assert needs_count(view) == 2
    assert "!2 NEEDS YOU" in render_route(view)[0]


def test_a_withheld_attention_register_states_no_count_and_prints_no_badge() -> None:
    """A register nobody writes is unknown; the badge is absent, never a zero."""
    register = _register(ATTENTION_ROUTE, document={})
    mine = attention_mine(register)
    view = _view(register, route=ATTENTION_ROUTE, attention=register)
    assert mine.state is TruthState.UNKNOWN
    assert mine.value is None
    assert mine.missing_reason == UNWRITTEN_REASON
    assert needs_count(view) == 0
    assert "NEEDS YOU" not in render_route(view)[0]


def test_an_empty_written_attention_register_counts_zero() -> None:
    """A register that was read and held nothing states zero, which is a count."""
    register = _written_attention(0)
    mine = attention_mine(register)
    assert mine.state is TruthState.KNOWN
    assert mine.value == "0"
    assert needs_count(_view(register, route=ATTENTION_ROUTE, attention=register)) == 0


def test_one_open_action_counts_one() -> None:
    """The off-by-one boundary under the multi-row case."""
    register = _written_attention(1)
    assert attention_mine(register).value == "1"


def test_the_epoch_one_header_still_counts_the_prototype_register() -> None:
    """With no register held the console falls back, and the golden mode is unchanged."""
    view = _view(None, route=ATTENTION_ROUTE, attention=None)
    assert needs_count(view) > 0


def test_asking_another_route_for_the_attention_count_raises() -> None:
    """Rows that are not actions cannot be counted as actions."""
    with pytest.raises(ValueError, match="states no attention count"):
        attention_mine(_register("activity"))


# ---------- Activity's buckets come off the served read model ----------


def test_every_activity_bucket_count_is_the_rows_that_stated_it() -> None:
    """A bucket is the rows of one status, counted off the projection's own rows."""
    register = _register("activity")
    assert dict(register.status_counts()) == {"RUNNING": 2, "BLOCKED": 1, "CANCELLED": 1}
    assert sum(register.status_counts().values()) == len(register.rows)
    assert register.count("run") == 4


def test_the_activity_frame_prints_the_derived_bucket_counts() -> None:
    """Every bucket on the frame is one the read model derived."""
    register = _register("activity")
    rows = render_route(_view(register, route="activity", attention=None))
    buckets = next(row for row in rows if row.startswith(" BUCKETS"))
    for name, count in register.status_counts().items():
        assert re.search(rf"\b{name} {count}\b", buckets), buckets
    assert "cursor 41,208" in rows[1]


def test_an_empty_activity_register_counts_zero_and_buckets_nothing() -> None:
    """An empty register is a count that was taken; it holds no bucket at all."""
    register = _register("activity", document={"run": {}})
    assert register.counts == {"run": 0}
    assert dict(register.status_counts()) == {}
    assert register.rows == ()


def test_a_single_row_register_buckets_one() -> None:
    """The off-by-one boundary below the probe document's four-row register."""
    one = {"run": {"RUN-0000000a": RUNS["RUN-0000000a"]}}
    register = _register("activity", document=one)
    assert dict(register.status_counts()) == {"RUNNING": 1}
    assert register.unstated_rows() == 0


def test_a_row_stating_no_status_falls_in_no_bucket() -> None:
    """An unstated row is reported apart rather than filed under a bucket it never named."""
    document = {"run": {"RUN-0000000e": {"urn": f"{SCOPE}/run/RUN-0000000e", "revision": 1}}}
    register = _register("activity", document=document)
    assert dict(register.status_counts()) == {}
    assert register.unstated_rows() == 1
    rows = render_route(_view(register, route="activity", attention=None))
    assert any(row.startswith(" UNBUCKETED") for row in rows)


def test_a_projection_of_another_route_is_not_a_register_view() -> None:
    """A register frame drawn from another route's rows would mislabel every row."""
    with pytest.raises(ValueError, match="has no register read model"):
        build_register_view(_projection("track"))


# ---------- the epoch-1 mode still resolves every advertised footer key ----------


#: The dispatcher name of every key the bar prints under another name.
_DISPATCHER_NAME: dict[str, str] = {name: key for key, name in KEY_NAMES.items()}


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


def _advertised(route: str, fixture: Fixture) -> list[str]:
    """Return the dispatcher key name of every key the route's footer advertises."""
    session = Session()
    session.route = route
    names: list[str] = []
    for entry in route_keys(session, fixture, route):
        for token in entry.keys:
            glyphs = list(token) if all(ch in _DISPATCHER_NAME for ch in token) else [token]
            names.extend(_DISPATCHER_NAME.get(glyph, glyph) for glyph in glyphs)
    return names


@pytest.mark.parametrize("route", REGISTER_ROUTES)
def test_the_epoch_one_feed_resolves_every_key_its_footer_advertises(route: str) -> None:
    """Binding a route natively must not cost the prototype mode a single footer key."""
    fixture = _fixture()
    unclaimed: list[str] = []
    for key in _advertised(route, fixture):
        session = Session()
        session.route = route
        render_route(View(session=session, fixture=fixture, w=120, h=30))
        ctx = Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30)
        dispatch(ctx, key, False)
        if session.trace is None or session.trace.endswith("unclaimed"):
            unclaimed.append(f"{route}:{key}")
    assert not unclaimed, f"advertised but unhandled: {', '.join(unclaimed)}"


@pytest.mark.parametrize("route", REGISTER_ROUTES)
def test_every_register_route_advertises_at_least_one_key(route: str) -> None:
    """A frame with no footer key is one an operator cannot leave."""
    assert _advertised(route, _fixture())


def test_an_unadvertised_key_on_a_register_route_reads_as_unclaimed() -> None:
    """The claim check has teeth: a key no register route binds reads as unclaimed."""
    fixture = _fixture()
    session = Session()
    session.route = ATTENTION_ROUTE
    render_route(View(session=session, fixture=fixture, w=120, h=30))
    dispatch(Ctx(session=session, fixture=fixture, host=_Host(), w=120, h=30), "Q", False)
    assert session.trace is not None
    assert session.trace.endswith("unclaimed")


# ---------- native frames print the live count, not a hard-coded zero ----------


def _chrome_fixture() -> Fixture:
    """Return the fixture a live console holds: the packaged chrome and no prototype row."""
    return Fixture.from_chrome(load_chrome())


def _native_view(route: str, *, attention: RegisterView | None) -> View:
    """Return a live-console view on native ``route`` holding ``attention``."""
    session = Session()
    session.route = route
    return View(
        session=session,
        fixture=_chrome_fixture(),
        w=120,
        h=24,
        projection=build_spine_view(_projection(route, document={})),
        attention=attention,
    )


@pytest.mark.parametrize("route", ["track", "roadmap"])
def test_native_frame_header_prints_two_open_attention_items(route: str) -> None:
    """A native frame reads the same register the prototype frames read."""
    rows = render_route(_native_view(route, attention=_written_attention(2)))
    assert "!2 NEEDS YOU" in rows[0]


def test_native_frame_header_prints_a_single_open_item() -> None:
    """The off-by-one boundary: one open action is a badge, not an absent one."""
    rows = render_route(_native_view("track", attention=_written_attention(1)))
    assert "!1 NEEDS YOU" in rows[0]


def test_native_frame_header_prints_no_badge_when_no_register_is_held() -> None:
    """The empty boundary: a live console holding no Attention register shows no count."""
    rows = render_route(_native_view("track", attention=None))
    assert "NEEDS YOU" not in rows[0]


# ---------- the help overlay lists the paging keys a native frame advertises ----------


def test_help_overlay_on_a_native_frame_lists_the_paging_keys() -> None:
    """A native keybar pages, so its help names the keys in full, as the bar does."""
    view = _native_view("track", attention=None)
    rows = render_overlay("help", view)
    assert any("PageUp PageDown" in row and "page" in row for row in rows)
    assert any("Home End" in row and "ends" in row for row in rows)


def test_help_overlay_on_a_prototype_frame_keeps_the_prototype_table() -> None:
    """Without a held read model the help lists the route's own table, unchanged."""
    session = Session()
    session.route = "track"
    rows = render_overlay("help", View(session=session, fixture=_fixture(), w=120, h=30))
    table = {entry.token for entry in route_keys(session, _fixture())}
    assert ("PageUp PageDown" in table) == any("PageUp PageDown" in row for row in rows)


def test_help_overlay_for_an_unknown_overlay_name_raises() -> None:
    """The overlay table is closed: a name it does not bind is refused."""
    with pytest.raises(KeyError):
        render_overlay("not-an-overlay", _native_view("track", attention=None))


# ---------- a Batch compacted at a terminal status still reaches its route ----------


def test_native_route_rows_keep_a_batch_compacted_at_a_terminal_status(tmp_path: Path) -> None:
    """The daemon's read verb merges the Batch ledger's terminal rows back in."""
    provisioned = provision(tmp_path / "repo")
    row = seed_row("batch", "COMPLETED")
    key = str(row["key"])
    seed(provisioned, {Epoch2Collection.BATCH.value: {key: row}})
    compact_terminal_record(
        document_path(provisioned),
        record=LedgerRecord(
            collection=Epoch2Collection.BATCH,
            record_key=key,
            status="COMPLETED",
            recorded_at=TXN_AT,
            payload=row,
        ),
    )
    document = read_document(document_path(provisioned))
    assert key not in document_rows(document, Epoch2Collection.BATCH)

    answer = asyncio.run(
        methods.dispatch(
            "projection.git.pr.read",
            method_context(tmp_path / "runtime"),
            {"repo_root": str(provisioned.root)},
        )
    )

    batches = {r["key"]: r for r in answer["rows"] if r["collection"] == "batch"}
    assert key in batches
    assert batches[key]["status"]["value"] == "COMPLETED"


def test_native_route_rows_hold_no_batch_when_the_ledger_is_empty(tmp_path: Path) -> None:
    """The empty boundary: a tree whose Batch ledger was never written reads no Batch."""
    provisioned = provision(tmp_path / "repo")
    answer = asyncio.run(
        methods.dispatch(
            "projection.git.pr.read",
            method_context(tmp_path / "runtime"),
            {"repo_root": str(provisioned.root)},
        )
    )
    assert [r for r in answer["rows"] if r["collection"] == "batch"] == []


# ---------- the stuck-migration entry layer shows the repair command ----------


@pytest.mark.parametrize("state_id", ["migration", "interrupted"])
def test_stuck_migration_entry_shows_the_repair_command(tmp_path: Path, state_id: str) -> None:
    """The entry layer draws from the chrome, so a live console shows the command."""
    chrome = with_repair_lines(load_chrome(), state_id, repair_lines(tmp_path))
    session = Session()
    session.route = "entry"
    session.entry_sel = next(i for i, s in enumerate(chrome.entry) if s.id == state_id)
    view = View(session=session, fixture=Fixture.from_chrome(chrome), w=240, h=30)

    rows = render_route(view)

    assert not any("NOT HELD" in row for row in rows)
    assert any(f"eawf migrate epoch2 --recover --target-root {tmp_path}" in r for r in rows)
    assert "MIGRATION REQUIRED" in rows[0]


def test_repair_lines_quote_a_target_root_holding_a_space(tmp_path: Path) -> None:
    """A path the shell would split is quoted, so the shown command runs as printed."""
    root = tmp_path / "a tree"
    lines = repair_lines(root)
    assert lines[1].endswith(f"--target-root '{root}'")
    assert lines[-1].endswith(f"--rollback --target-root '{root}'")


def test_with_repair_lines_refuses_an_unknown_entry_state() -> None:
    """Replacing the tail of a state the chrome does not carry is refused."""
    with pytest.raises(ValueError, match="no entry state"):
        with_repair_lines(load_chrome(), "not-a-state", ("x",))
