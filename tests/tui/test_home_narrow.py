"""Narrow-terminal + resize acceptance for the HOME surfaces (P30-I02-W30).

The three HOME panes -- the roadmap tree
(:class:`~eawf.surfaces.tui.widgets.roadmap_tree.RoadmapTree`), the status pane
(:class:`~eawf.surfaces.tui.widgets.status_pane.StatusPane`), and the autopilot
frontier pane (:class:`~eawf.surfaces.tui.modes.autopilot.AutopilotModeScreen`)
-- carried no narrow-width golden, so the P29 "snapshot-green-but-live-broken
at small terminals" failure class could reopen: a row whose load-bearing token
(a lifecycle sigil, a wave id, or an ``n/m`` completion count) is clipped at a
constrained column count would pass every wide-layout snapshot yet read broken
on a 40-column terminal.

This module closes that gap with three bands:

* **Narrow-width render goldens.** Each HOME pane is rendered at a constrained
  :data:`_NARROW_COLS`-column width (well below the wide default) and asserted
  to keep every load-bearing token un-clipped: the leading sigil, the wave /
  iter / phase id, and the ``n/m`` completion ratio all survive; only the
  prose title ellipsizes / soft-wraps. The goldens **RED on an overflow
  regression** -- a reconstructed clipped row (the id or count cut off the row)
  fails the same assertions the reflowed row passes, so the golden
  discriminates a graceful reflow from a clip.
* **An ``on_resize`` reflow pass.** Shrinking the pane re-cuts the title to the
  new width (the roadmap tree's :meth:`RoadmapTree.on_resize` rebuild, the
  status pane's two-column -> single-column collapse) rather than clipping the
  load-bearing token: after the shrink the title is shorter but the sigil, id,
  and count are intact.
* **An ``affordance_parity`` check.** Every HOME affordance the wide layout
  exposes (each pane's footer hints + key bindings + its dispatch-target /
  selectable rows) stays reachable at the narrow width -- no action is clipped
  offscreen. :func:`_affordance_parity` diffs the affordance set across the
  wide and narrow renders of each pane and asserts the narrow set is a superset
  (never a subset) of the wide one.

Determinism follows the project Pilot-worker rule: each Pilot body drains
workers via :func:`~eawf.surfaces.tui.snapshot.settle_screen` before asserting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import orjson
from textual.app import ComposeResult

from eawf.kernel.spec.auq_bridge import compute_ready_frontier
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.autopilot import (
    BLOCKED_BY_MARKER,
    BLOCKED_ROW_CLASS,
    FRONTIER_ROW_CLASS,
    AutopilotModeScreen,
    blocked_rows,
    build_frontier_items,
    ready_rows,
    render_blocked_row,
    render_ready_row,
)
from eawf.surfaces.tui.snapshot import (
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.roadmap_tree import ELLIPSIS, RoadmapTree
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph
from eawf.surfaces.tui.widgets.status_pane import (
    StatusPane,
    build_status_columns,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

#: The constrained column count the narrow-width goldens render at. Well below
#: the wide default (120) and below the status pane's two-column threshold, so
#: every pane is forced through its narrow reflow path -- the regime where the
#: P29 clip-at-small-terminal failure class lives. 40 cols is a realistic
#: minimum a split-pane / tmux operator works at.
_NARROW_COLS: int = 40

#: The wide column count the parity baseline renders at (the operator default).
_WIDE_COLS: int = 120

#: The unicode running-diamond lifecycle sigil a HOME row leads with for an
#: active phase / iter / in-progress wave (the operator's default render mode).
#: Every reflowed row keeps this leading sigil, so a clipped row that ate its
#: leading mark is detectable by the sigil's absence.
_RUNNING_SIGIL = glyph(Sigil.RUNNING, mode="unicode")

#: The dispatch chrome arrow + the multi-select checkbox the autopilot ready
#: row leads with -- the dispatch-affordance look that must stay reachable at
#: the narrow width.
_DISPATCH_ARROW = chrome("dispatch", mode="unicode")
_CHECK_ON = chrome("check_on", mode="unicode")


class _TreeHarness(PaletteHarnessApp):
    """Bare host mounting a roadmap tree at the full terminal width.

    The tree spans the whole terminal, so the terminal column count IS the
    pane content width -- this drives the tree's reflow at a precise pane width
    without the full-app 2x2 quadrant splitting it. The ``render_mode``
    attribute seeds the operator's ``unicode`` reskin column (the tree reads
    :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` off its host App), so the
    pane-width goldens exercise the unicode sigils the operator sees, not the
    bare-harness ASCII fallback.
    """

    CSS_PATH = str(_THEME)

    #: Seed the unicode reskin column so the tree renders the same glyph set
    #: the production :class:`~eawf.surfaces.tui.app.EaApp` does.
    render_mode = "unicode"

    def compose(self) -> ComposeResult:
        yield RoadmapTree(id="rt")


class _StatusHarness(PaletteHarnessApp):
    """Bare host mounting a status pane at the full terminal width.

    The pane spans the whole terminal so the column count IS the pane content
    width -- this drives the status pane's narrow single-column reflow at a
    precise pane width (rather than the full-app 2x2 quadrant splitting it). The
    ``render_mode`` attribute seeds the operator's ``unicode`` reskin column the
    pane reads off its host App.
    """

    CSS_PATH = str(_THEME)

    #: Seed the unicode reskin column (matching production).
    render_mode = "unicode"

    def compose(self) -> ComposeResult:
        yield StatusPane(id="sp")


_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)

#: The digit key that switches to the Autopilot mode.
_AUTOPILOT_DIGIT = "2"


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _labels(tree: RoadmapTree) -> list[str]:
    """Flatten every non-root node label to a plain string."""
    out: list[str] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            out.append(str(child.label))
            walk(child)

    walk(tree.root)
    return out


def _node_by_data(tree: RoadmapTree, data: str) -> object:
    """Return the first tree node whose ``data`` payload equals *data*."""
    found: list[object] = []

    def walk(node: object) -> None:
        for child in node.children:  # type: ignore[attr-defined]
            if child.data == data:  # type: ignore[attr-defined]
                found.append(child)
            walk(child)

    walk(tree.root)
    return found[0]


def _long_title_state() -> State:
    """Return the fixture with phase / iter / wave titles far wider than 40 cols.

    Each title overflows the narrow pane so the row-title reflow (ellipsis /
    soft-wrap) fires at every depth -- the regime where a careless renderer
    would clip the trailing ``n/m`` count or the leading sigil instead of the
    title.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    long = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed"
    payload["phases"]["P01"]["title"] = long
    payload["iters"]["P01-I01"]["title"] = long
    payload["waves"]["P01-I01-W01"]["title"] = long
    return State.model_validate(payload)


def _write_state(tmp_path: Path, state: State) -> Path:
    """Write *state* to ``<tmp>/.ea/state.json`` and return the path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


# --------------------------------------------------------------------------
# Affordance parity -- the wide-vs-narrow reachability invariant (pure)
# --------------------------------------------------------------------------


def _affordance_parity(wide: set[str], narrow: set[str]) -> None:
    """Assert every affordance reachable wide stays reachable narrow.

    The narrow-render affordance set must be a SUPERSET of the wide one: a
    HOME affordance the wide layout exposes (a footer key hint, a bound key,
    or a selectable / dispatch-target row) is never clipped offscreen by the
    narrow reflow. A missing affordance at the narrow width is the exact
    "action clipped offscreen" regression this check guards.

    Args:
        wide: The affordance tokens reachable in the wide render.
        narrow: The affordance tokens reachable in the narrow render.

    Raises:
        AssertionError: When the narrow set drops an affordance the wide set
            exposed (the parity violation).
    """
    missing = wide - narrow
    assert not missing, f"narrow render clipped wide affordances offscreen: {sorted(missing)}"


def test_affordance_parity_superset_passes() -> None:
    """A narrow set that keeps every wide affordance passes the parity check."""
    wide = {"up", "down", "dispatch", "select"}
    narrow = {"up", "down", "dispatch", "select", "scroll"}  # extra is fine
    _affordance_parity(wide, narrow)  # superset -> no raise


def test_affordance_parity_dropped_affordance_reds() -> None:
    """A narrow set that drops a wide affordance reds the parity check.

    The RED side of the parity check: an affordance clipped offscreen at the
    narrow width (here ``dispatch`` is missing) trips the assertion, so the
    check discriminates a clipped action from a fully-reachable one.
    """
    wide = {"up", "down", "dispatch", "select"}
    narrow = {"up", "down", "select"}  # dispatch clipped offscreen
    try:
        _affordance_parity(wide, narrow)
    except AssertionError as exc:
        assert "dispatch" in str(exc)
    else:  # pragma: no cover - the assertion above must fire
        raise AssertionError("parity check did not red on a dropped affordance")


# --------------------------------------------------------------------------
# Roadmap tree -- narrow-width render keeps sigil + id + n/m count
# --------------------------------------------------------------------------


def test_roadmap_tree_narrow_keeps_sigil_id_and_count() -> None:
    """At 40 cols the tree reflows: sigil + id + ``n/m`` count survive, title cuts.

    The narrow-width golden over the roadmap tree at a 40-column pane (the bare
    harness spans the whole terminal, so the column count IS the pane content
    width). Every row's load-bearing tokens -- the leading unicode lifecycle
    sigil, the wave / iter / phase id, and the iter / phase ``n/m`` completion
    count -- stay intact; only the prose title ellipsizes to fit. A clip of the
    count / id / sigil -- the P29 failure class -- would fail these assertions.
    """

    async def body() -> None:
        app = _TreeHarness()
        async with app.run_test(size=(_NARROW_COLS, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _long_title_state()
            await pilot.pause()
            assert tree.size.width == _NARROW_COLS

            phase_label = str(_node_by_data(tree, "P01").label)  # type: ignore[attr-defined]
            iter_label = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]
            wave_label = str(_node_by_data(tree, "P01-I01-W01").label)  # type: ignore[attr-defined]

            # The leading lifecycle sigil survives on every row (the active
            # phase / iter render the running diamond; the in-progress wave too).
            assert phase_label.startswith(_RUNNING_SIGIL)
            assert iter_label.startswith(_RUNNING_SIGIL)
            assert wave_label.startswith(_RUNNING_SIGIL)

            # The id survives -- the title was cut to make room, not the id.
            assert "P01" in phase_label
            assert "P01-I01" in iter_label
            assert "P01-I01-W01" in wave_label

            # The branch rows keep their flush-right ``n/m`` completion count;
            # the wave row keeps its empty-state burn sentinel (no budget here).
            assert phase_label.rstrip().endswith("0/1")
            assert iter_label.rstrip().endswith("0/1")
            assert wave_label.rstrip().endswith(EMPTY_STATE)

            # The reflow ellipsized the over-long title rather than clipping the
            # count -- and every visible label fits inside the narrow width.
            for label in (phase_label, iter_label, wave_label):
                assert ELLIPSIS in label
                assert len(label) <= _NARROW_COLS

            # And the CAPTURED SCREEN FRAME keeps the count + sigil + id too --
            # the on-screen render is what an operator sees, so the count
            # surviving the node label but not the frame would still be a clip
            # (``overflow-x: hidden`` at the pane edge). Asserting the frame
            # proves the reflow reaches the rendered terminal text.
            frame = normalize_snapshot(capture_screen_text(app))
            assert _RUNNING_SIGIL in frame
            assert "P01-I01-W01" in frame
            assert "0/1" in frame

    asyncio.run(body())


def test_roadmap_tree_narrow_overflow_regression_reds() -> None:
    """A clipped row (count cut off) fails the narrow golden -- the RED side.

    Reconstructs the overflow regression the golden guards: a row whose
    trailing ``n/m`` count was clipped off the narrow width. Running the
    narrow golden's count assertion over it fails, proving the golden
    discriminates a graceful reflow (count survives) from a clip (count gone).
    """

    async def body() -> None:
        app = _TreeHarness()
        async with app.run_test(size=(_NARROW_COLS, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _long_title_state()
            await pilot.pause()
            iter_label = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]
            # The real reflowed row keeps the count flush-right.
            assert iter_label.rstrip().endswith("0/1")

            # Reconstruct the regression: a row whose width-overflow clipped the
            # trailing count (the renderer dropped it instead of cutting the
            # title). The golden's count assertion fails against the clipped row.
            clipped = iter_label.rstrip().removesuffix("0/1").rstrip()
            assert not clipped.endswith("0/1")  # the count is gone -> golden reds

    asyncio.run(body())


def test_roadmap_tree_on_resize_reflows_not_clips() -> None:
    """Shrinking the pane re-cuts the title; the sigil + id + count survive.

    The ``on_resize`` reflow pass for the roadmap tree: a title that fit at the
    wide width is re-truncated when the pane narrows (proving the rebuild fires
    on resize), while the leading sigil, the wave id, and the flush-right count
    stay intact -- the resize reflows the row, it does not clip a load-bearing
    token.
    """

    async def body() -> None:
        app = _TreeHarness()
        async with app.run_test(size=(_WIDE_COLS, 20)) as pilot:
            await pilot.pause()
            tree = app.query_one("#rt", RoadmapTree)
            tree.state = _long_title_state()
            await pilot.pause()
            wide_iter = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]
            wide_len = len(wide_iter)
            assert wide_iter.rstrip().endswith("0/1")

            # Narrow the viewport; on_resize rebuilds + re-truncates the title.
            await pilot.resize_terminal(_NARROW_COLS, 20)
            await pilot.pause()
            narrow_iter = str(_node_by_data(tree, "P01-I01").label)  # type: ignore[attr-defined]

            # The row got shorter (the title re-truncated to the new width)...
            assert len(narrow_iter) < wide_len
            assert len(narrow_iter) <= _NARROW_COLS
            assert ELLIPSIS in narrow_iter
            # ...but the load-bearing tokens are intact: the row reflowed, not
            # clipped.
            assert narrow_iter.startswith(_RUNNING_SIGIL)
            assert "P01-I01" in narrow_iter
            assert narrow_iter.rstrip().endswith("0/1")

    asyncio.run(body())


# --------------------------------------------------------------------------
# Status pane -- narrow-width single-column reflow keeps load-bearing tokens
# --------------------------------------------------------------------------


def test_status_pane_narrow_keeps_sigil_id_and_count() -> None:
    """The narrow status build keeps the sigil + ``n/m`` progress + wave id.

    Below the two-column threshold :func:`build_status_columns` returns the
    flat single-column line set; at the narrow width every load-bearing token
    survives -- the live-row sigil, the ``progress:`` ``n/m`` completion count,
    and the DISPATCH NOW wave id -- so the pane reflows to one column rather
    than clipping a counter.
    """
    state = _load(_PHASE_ITER_WAVE)
    rows = build_status_columns(state, mode="unicode", width=_NARROW_COLS)
    blob = "\n".join(rows)

    # The live counters lead with the running sigil (the live-row mark).
    assert any(row.startswith(_RUNNING_SIGIL) for row in rows)
    # The progress completion count survives un-clipped.
    progress = next(row for row in rows if row.startswith("progress:"))
    assert "0/1" in progress
    # The DISPATCH NOW band keeps the active wave id (short form) un-clipped.
    assert "W01" in blob
    # The project / phase / iter pointers survive too.
    assert any("QR" in row for row in rows)
    assert any("P01-I01" in row for row in rows)


def test_status_pane_narrow_mounted_renders_tokens() -> None:
    """The mounted status pane at a 40-col pane keeps sigil + count + wave id on screen.

    The mounted counterpart to the pure narrow build: the pane laid out at a
    40-column pane content width (the bare harness spans the whole terminal, so
    the column count IS the pane width) surfaces the live-row sigil, the ``0/1``
    completion count, and the active wave id in the CAPTURED SCREEN FRAME -- not
    merely the widget content markup. Asserting against the on-screen frame is
    what proves the single-column reflow keeps the tokens visible rather than
    clipping them off the pane edge.
    """

    async def body() -> None:
        app = _StatusHarness()
        async with app.run_test(size=(_NARROW_COLS, 40)) as pilot:
            await pilot.pause()
            pane = app.query_one("#sp", StatusPane)
            pane.state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            assert pane.content_size.width == _NARROW_COLS
            frame = normalize_snapshot(capture_screen_text(app))
            assert _RUNNING_SIGIL in frame
            assert "0/1" in frame  # progress completion count, not clipped
            assert "W01" in frame  # DISPATCH NOW active wave id, not clipped

    asyncio.run(body())


def test_status_pane_on_resize_collapses_to_single_column_keeps_tokens() -> None:
    """Shrinking the pane flips two-column -> single-column without losing tokens.

    The ``on_resize`` reflow pass for the status pane: the wide layout lays the
    four sections in two columns; narrowing it below the two-column threshold
    collapses to one column (more rows, narrower) rather than clipping a cell.
    The load-bearing tokens (the live-row sigil, the ``0/1`` completion count,
    the active wave id) survive the collapse.
    """
    state = _load(_PHASE_ITER_WAVE)
    wide = build_status_columns(state, mode="unicode", width=_WIDE_COLS)
    narrow = build_status_columns(state, mode="unicode", width=_NARROW_COLS)

    # The collapse is real: the single column is taller than the two-column
    # layout (the right column's sections moved below the left column's).
    assert len(narrow) > len(wide)

    # And every load-bearing token survives the collapse.
    narrow_blob = "\n".join(narrow)
    assert any(row.startswith(_RUNNING_SIGIL) for row in narrow)
    assert any("0/1" in row for row in narrow)
    assert "W01" in narrow_blob


# --------------------------------------------------------------------------
# Autopilot pane -- narrow-width render keeps the dispatch affordance + ids
# --------------------------------------------------------------------------


def _autopilot_state() -> State:
    """Build a repo state whose frontier is one ready + one blocked wave.

    W01 is CLOSED; W02 is PENDING with W01 closed (ready) and carries a title
    far wider than 40 cols so the narrow reflow fires; W03 is PENDING blocked
    by W02 (the monotonic lower-sibling gate), so the blocked band renders.
    """
    waves = {
        "P01-I01-W01": Wave(
            id="P01-I01-W01",
            iter_id="P01-I01",
            title="First wave",
            status=WaveStatus.CLOSED,
            deps=[],
            opened_at=_T0,
        ),
        "P01-I01-W02": Wave(
            id="P01-I01-W02",
            iter_id="P01-I01",
            title="Add the narrow-width acceptance gate over the HOME panes here",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            opened_at=_T0,
        ),
        "P01-I01-W03": Wave(
            id="P01-I01-W03",
            iter_id="P01-I01",
            title="Wave P01-I01-W03",
            status=WaveStatus.PENDING,
            deps=["P01-I01-W01"],
            opened_at=_T0,
        ),
    }
    return State.model_validate(
        {
            "schema_version": "1.3",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": {
                "code": "QR",
                "slug": "quant-research",
                "title": "Quant Research",
                "domains": ["quant"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {"project_code": "QR"},
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {wid: w.model_dump(mode="json") for wid, w in waves.items()},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_autopilot_ready_row_narrow_keeps_id_and_dispatch_affordance() -> None:
    """The autopilot ready row keeps its id + checkbox + dispatch arrow at width.

    The narrow-width golden over the autopilot ready band (pure render). The
    ready row leads with the load-bearing dispatch affordance (the multi-select
    checkbox + the dispatch chrome arrow) and the wave id BEFORE the prose
    title, so the narrow soft-wrap pushes the title down without ever clipping
    the affordance or the id -- the dispatch target stays reachable.
    """
    state = _autopilot_state()
    frontier = compute_ready_frontier(build_frontier_items(state))
    rows = ready_rows(frontier, state)
    assert rows  # the frontier is non-empty
    rendered = render_ready_row(rows[0], selected=True, mode="unicode")
    # The dispatch affordance (checkbox + arrow) leads the row, ahead of the
    # over-long title, so it is never the token a narrow wrap clips.
    assert _CHECK_ON in rendered
    assert _DISPATCH_ARROW in rendered
    # The wave id survives -- it precedes the prose title in the row.
    assert "P01-I01-W02" in rendered


def test_autopilot_blocked_row_narrow_keeps_id_and_dep_marker() -> None:
    """The blocked row keeps its id + ``<- dep`` marker at the narrow width.

    A blocked row's load-bearing tokens are the wave id and the ``<- <dep>``
    marker naming the wave holding it off the frontier; both render in the row
    markup (the wide content the narrow soft-wrap reflows, never clips).
    """
    state = _autopilot_state()
    frontier = compute_ready_frontier(build_frontier_items(state))
    blocked = blocked_rows(frontier, state)
    assert blocked  # one blocked wave (W03 held by the lower-sibling gate)
    rendered = render_blocked_row(blocked[0])
    assert "P01-I01-W03" in rendered
    assert BLOCKED_BY_MARKER in rendered  # the "<- dep" edge label survives


def test_autopilot_pane_narrow_mounted_keeps_dispatch_target_reachable(tmp_path: Path) -> None:
    """The mounted autopilot pane at 40 cols keeps the ready row + ids reachable.

    The mounted narrow-width golden: at the constrained width the pane still
    mounts the ready (dispatch-target) row and renders both the ready wave id
    and the blocked wave id + its dep marker in the captured frame -- the
    narrow reflow soft-wraps the over-long title without clipping the
    dispatch-target row or any id.
    """
    state_path = _write_state(tmp_path, _autopilot_state())

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(_NARROW_COLS + 4, 30)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)  # -> autopilot
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            # The dispatch-target ready row stays mounted (reachable for d).
            assert pane.query(f".{FRONTIER_ROW_CLASS}")
            # The held wave stays mounted in the blocked band.
            assert pane.query(f".{BLOCKED_ROW_CLASS}")
            frame = normalize_snapshot(capture_screen_text(app))
            # Both ids + the dep marker survive in the captured narrow frame.
            assert "P01-I01-W02" in frame  # the ready (dispatch-target) wave
            assert "P01-I01-W03" in frame  # the blocked wave
            assert BLOCKED_BY_MARKER in frame  # its "<- dep" edge label

    asyncio.run(body())


# --------------------------------------------------------------------------
# Affordance parity across the wide + narrow renders of each HOME pane
# --------------------------------------------------------------------------


def _tree_affordances(tree: RoadmapTree) -> set[str]:
    """Return the reachable roadmap-tree affordances: bindings + selectable rows.

    The tree's affordances are its key bindings (collapse / expand) plus the
    set of selectable rows (every node's data-id) -- a row clipped offscreen by
    a narrow reflow would drop its data-id from this set.
    """
    keys = {binding.key for binding in tree.BINDINGS if hasattr(binding, "key")}
    row_ids = {label.split()[1] for label in _labels(tree) if len(label.split()) > 1}
    return keys | row_ids


def test_roadmap_tree_affordance_parity_wide_vs_narrow() -> None:
    """Every roadmap-tree affordance reachable wide stays reachable narrow.

    Diffs the tree's affordance set (bound keys + selectable row ids) across the
    wide and narrow renders and asserts the narrow set is a superset of the wide
    one -- no row (and so no Enter / collapse / expand target) is clipped
    offscreen when the pane narrows.
    """

    async def body() -> None:
        wide_app = _TreeHarness()
        async with wide_app.run_test(size=(_WIDE_COLS, 30)) as pilot:
            await pilot.pause()
            tree = wide_app.query_one("#rt", RoadmapTree)
            tree.state = _long_title_state()
            await pilot.pause()
            wide = _tree_affordances(tree)

        narrow_app = _TreeHarness()
        async with narrow_app.run_test(size=(_NARROW_COLS, 30)) as pilot:
            await pilot.pause()
            tree = narrow_app.query_one("#rt", RoadmapTree)
            tree.state = _long_title_state()
            await pilot.pause()
            narrow = _tree_affordances(tree)

        _affordance_parity(wide, narrow)
        # The selectable rows themselves are all present (the parity is over a
        # non-trivial set, not a vacuous empty one).
        assert {"P01", "P01-I01", "P01-I01-W01"} <= narrow

    asyncio.run(body())


def test_autopilot_affordance_parity_wide_vs_narrow(tmp_path: Path) -> None:
    """Every autopilot affordance reachable wide stays reachable narrow.

    The autopilot affordances are its footer-advertised intervention keys
    (dispatch / halt / skip / kill / pause / arm) plus its dispatch-target +
    blocked rows. The keys are layout-independent (bound on the screen), and the
    rows stay mounted at the narrow width, so the narrow affordance set is a
    superset of the wide one -- no intervention or row is clipped offscreen.
    """
    state_path = _write_state(tmp_path, _autopilot_state())

    async def _capture(cols: int) -> set[str]:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=(cols, 30)) as pilot:
            await settle_screen(pilot)
            await pilot.press(_AUTOPILOT_DIGIT)
            await settle_screen(pilot)
            pane = app.screen
            assert isinstance(pane, AutopilotModeScreen)
            keys = {binding.key for binding in pane.BINDINGS if hasattr(binding, "key")}
            rows = {"ready_row" for _ in pane.query(f".{FRONTIER_ROW_CLASS}")} | {
                "blocked_row" for _ in pane.query(f".{BLOCKED_ROW_CLASS}")
            }
            return keys | rows

    async def body() -> None:
        wide = await _capture(_WIDE_COLS)
        narrow = await _capture(_NARROW_COLS + 4)
        _affordance_parity(wide, narrow)
        # The intervention keys + both row kinds are non-trivially present.
        assert {"d", "H", "S", "K", "space", "a", "ready_row", "blocked_row"} <= narrow

    asyncio.run(body())
