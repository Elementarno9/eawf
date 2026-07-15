"""ASCII-text snapshot harness for the ``tui`` operator surface.

Captures a running Textual screen's rendered terminal as **plain ASCII
text** for golden-fixture comparison, driven by Textual's
``App.run_test()`` Pilot. The snapshot artifact is ASCII text (not the
SVG ``App.export_screenshot`` output):

* **diffable** — a reviewer reads the golden ``.txt`` and the unified
  diff in a code-review tool;
* **scrub-safe** — the captured text is exactly what an operator sees,
  so the secrets/PII gate inspects the same surface that ships;
* **drift-free** — SVG output embeds font metrics + style segments that
  shift across Python and Textual versions; the plain-text row dump is
  stable as long as the layout is.

The capture reads the active screen's compositor
(:meth:`textual.screen.Screen.render_strips`), which renders the topmost
screen on the stack — a base scope screen or a pushed modal overlay
alike — so one capture path serves both screen and overlay fixtures.

Determinism: the only volatile cell in the rendered chrome is the
header wall-clock (``HH:MM UTC``). :func:`normalize_snapshot` rewrites it
to a fixed ``HH:MM UTC`` placeholder before comparison so the goldens do
not churn with the time of day; everything else in the frame is a pure
function of the bound (fixture) ``state.json``.
"""

from __future__ import annotations

import asyncio
import difflib
import itertools
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from eawf.surfaces.render.snapshot_normalize import normalize_snapshot

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.app import App
    from textual.dom import DOMNode
    from textual.pilot import Pilot

    from eawf.kernel.state.models import State

#: The pinned SVG rasterizer CLI, mirroring the ``svg_pixel_diff`` /
#: ``tools/rasterize_svg.py`` discipline. The live-render visual oracle
#: exports the TUI screen as an SVG (``App.export_screenshot``) and hands it
#: here to become the PNG the layout-shape rubric scores. System fonts are
#: LEFT ON: the layout-shape oracle tolerates host font variation (it reads
#: border corners, column count, alignment, and highlight contrast, not glyph
#: bytes), and the vendored test font omits box-drawing + sigil glyphs, so
#: pinning it would render the frame chrome as tofu and hide the very layout
#: the oracle inspects. ``resvg`` is absent on most developer machines;
#: callers that need a clean skip check :func:`resvg_available` first.
_RESVG: str = "resvg"

#: Env var that, when set to ``"1"``, makes :func:`assert_screen_snapshot`
#: (re)write the golden fixture from the live capture instead of
#: comparing against it. CI runs **without** this set, so a drift fails
#: the build; a developer regenerates with
#: ``EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/``.
SNAPSHOT_REGEN_ENV: str = "EAWF_SNAPSHOT_REGEN"

#: Upper bound on the settle-pump cycles. The read-only state binder
#: populates ``app.state`` and the widgets seed from it within a couple of
#: message-pump turns; this cap keeps :func:`settle_screen` from hanging
#: if a frame never stabilises (e.g. a live animation) while giving the
#: async state-load + widget-seed handshake ample room.
_SETTLE_MAX_CYCLES: int = 20

#: Upper bound on unified-diff lines embedded in a drift ``AssertionError``.
#: A full-screen frame is ~40 rows, so a real drift fits comfortably; the
#: cap only guards against an unbounded log dump if the two frames diverge
#: wholesale (e.g. an empty capture vs a populated golden).
_DRIFT_DIFF_MAX_LINES: int = 200

#: Upper bound on the mtime-poll backstop pump cycles. A tight poll cadence
#: (set via ``EAWF_POLL_INTERVAL_S``) ticks within a couple of message-pump
#: turns once the on-disk mtime advances; this cap keeps
#: :func:`tick_poll_backstop` from hanging if the binder's poll loop never
#: delivers (e.g. the daemon push path is live and the mtime never bumps).
_POLL_BACKSTOP_MAX_CYCLES: int = 40


async def quiesce_volatile_chrome(pilot: Pilot[object]) -> None:
    """Freeze the two timer-driven chrome elements before a golden capture.

    Two elements of the rendered chrome flip on wall-clock timers, not on
    fixture state, so a capture holds whichever phase the host happened to be
    in when the screen was read -- a coin flip decided by scheduler load, not
    by what the pane renders. Both drove the lone red macos-15 CI job:

    * **The daemon-degraded flip.** The read-only binder trips
      ``app.degraded`` true ~1.5 s after mount when no daemon answers its
      socket probe (the CI default). That top-docks the degraded banner OVER
      the Header row -- :func:`normalize_snapshot` then drops the banner line,
      so the captured frame loses the Header (brand ``Eä``) and a 40-row
      golden reads back as 39 -- and flips every ``app.degraded``-reading pane
      into its degraded notice. Forcing ``degraded`` back to ``False``,
      re-syncing the banner (hidden), and refreshing the feed notices pins the
      frame to the deterministic non-degraded shape every golden holds.
    * **The footer heartbeat pulse.** The footer ``•`` dot blanks to a bare
      space every 1.0 s pulse; a blank cell is rstripped by
      :func:`capture_screen_text`, dropping the trailing bullet from the
      footer row. :meth:`~eawf.surfaces.tui.widgets.heartbeat.Heartbeat.ack`
      forces every heartbeat lit so the bullet is always present. (The prior
      normalizer fix only collapsed the dim glyph, not the blank phase.)

    One frame is pumped after the degraded revert so the banner-hide layout
    pass and the pane-notice repaint land (the Header returns to row 0) before
    the heartbeat is forced last -- with no further event-loop yield, so the
    pulse timer cannot re-blank the dot between the ``ack`` and the caller's
    capture.

    Every guard is a soft ``getattr`` so a bare (non-``EaApp``) host under a
    Pilot -- which carries none of these seams -- is a clean no-op.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
    """
    from eawf.surfaces.tui.widgets.heartbeat import Heartbeat

    app = pilot.app
    if hasattr(app, "degraded"):
        app.degraded = False
    sync_banner = getattr(app, "_sync_degraded_banner", None)
    if callable(sync_banner):
        sync_banner()
    for listener in getattr(app, "_feed_listeners", ()):
        refresh_notice = getattr(listener, "refresh_empty_notice", None)
        if callable(refresh_notice):
            refresh_notice()
    # Pump one frame so the banner-hide layout + pane-notice repaint land
    # before the heartbeat is forced.
    await pilot.pause()
    # Force every heartbeat lit LAST: no further await runs before the caller
    # captures, so the 1.0 s pulse timer cannot toggle the dot back to blank
    # between this ack and the capture.
    for heartbeat in app.query(Heartbeat):
        heartbeat.ack()


async def settle_screen(pilot: Pilot[object], *, quiesce: bool = True) -> str:
    """Pump the app until its rendered frame stabilises, return that frame.

    The read-only state binder loads ``state.json`` and pushes it into
    the App reactive asynchronously, and each widget seeds from
    ``app.state`` on its own mount. A bare ``await pilot.pause()`` can
    therefore capture an in-between frame (state not yet bound → empty-
    scope placeholder), making a golden flaky under scheduler load. This
    helper pumps the message loop until two consecutive normalised
    captures match (or :data:`_SETTLE_MAX_CYCLES` is reached), then
    :func:`quiesce_volatile_chrome` freezes the two timer-driven chrome
    elements (the daemon-degraded flip and the footer heartbeat pulse) and a
    final frame is captured, so the returned text -- and the app state the
    caller reads on any immediately-following sync capture -- is the settled,
    quiesced frame.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from
            ``app.run_test()``.

    Returns:
        The settled, quiesced, normalised screen text.
    """
    # Drain background workers (e.g. the GitPane git probe, which now runs off
    # the event loop) before sampling so the capture reflects the post-worker
    # frame rather than a pre-probe placeholder.
    await pilot.app.workers.wait_for_complete()
    previous = normalize_snapshot(capture_screen_text(pilot.app))
    for _ in range(_SETTLE_MAX_CYCLES):
        await pilot.pause()
        current = normalize_snapshot(capture_screen_text(pilot.app))
        if current == previous:
            break
        previous = current
    # Freeze the timer-driven chrome (degraded banner + heartbeat pulse) so the
    # final capture is phase-independent regardless of how slowly the host ran.
    # A behavioural test that deliberately drives the degraded state passes
    # quiesce=False so the forced degraded=False revert does not erase it.
    if quiesce:
        await quiesce_volatile_chrome(pilot)
    return normalize_snapshot(capture_screen_text(pilot.app))


async def push_state_revision(pilot: Pilot[object], new_state: State) -> str:
    """Push a fresh state revision through the App's daemon-push seam, settle.

    Models the daemon ``state.subscribe`` push leg without a live daemon: a
    fresh :class:`~eawf.kernel.state.models.State` is delivered through the
    App's ``_on_state`` hook (the same coroutine the binder marshals every
    push and poll refresh through), then the frame is pumped to rest. A pane
    that watches the App reactive ``state`` re-renders off this push WITHOUT
    an app restart -- the live-behaviour the project's TUI-staleness lesson
    pins for the push leg.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
        new_state: The fresh read-only state revision to deliver.

    Returns:
        The settled, normalised screen text after the push lands.

    Raises:
        AttributeError: When the mounted app exposes no ``_on_state`` push
            hook (i.e. it is not an :class:`~eawf.surfaces.tui.app.EaApp`),
            so the probe is not silently a no-op against a bare host.
    """
    on_state = getattr(pilot.app, "_on_state", None)
    if not callable(on_state):
        raise AttributeError("app exposes no _on_state push hook")
    await on_state(new_state)
    return await settle_screen(pilot)


async def tick_poll_backstop(
    pilot: Pilot[object],
    state_path: Path,
    new_state: State,
) -> str:
    """Advance the on-disk state + let the always-on mtime-poll backstop fire.

    Models the poll backstop the project's TUI-staleness lesson pins BESIDE
    the push: writes *new_state* to *state_path* (advancing its mtime) and
    pumps the message loop until the binder's mtime-gated poll loop re-reads
    the file and delivers the revision through ``_on_state`` -- without any
    daemon push. Drive this under a tight poll cadence (set
    ``EAWF_POLL_INTERVAL_S`` to a small value before constructing the App)
    so the loop ticks within :data:`_POLL_BACKSTOP_MAX_CYCLES` pump cycles
    rather than the production interval.

    The mtime write is the only state mutation a test performs; it writes a
    fixture path, never the daemon's live ``state.json`` (AGENTS rule 4), so
    the read-only binder contract is preserved.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
        state_path: The fixture ``state.json`` the App's binder polls.
        new_state: The fresh revision to write to disk for the poll to read.

    Returns:
        The settled, normalised screen text after the poll-driven refresh.
    """
    state_path.write_text(new_state.model_dump_json(), encoding="utf-8")
    poll_interval = float(os.environ.get("EAWF_POLL_INTERVAL_S", "2.0"))
    settled = normalize_snapshot(capture_screen_text(pilot.app))
    for _ in range(_POLL_BACKSTOP_MAX_CYCLES):
        # Sleep past one poll cadence so the binder's mtime-gated loop wakes,
        # re-reads the advanced file, and delivers via _on_state; pump after
        # so the watcher-driven recompose lands before the next capture.
        await asyncio.sleep(poll_interval)
        current = await settle_screen(pilot)
        if current != settled:
            return current
        settled = current
    return settled


def mutating_action_keys_resolve(
    *,
    bindings: Iterable[tuple[str, str]],
    namespace: DOMNode,
) -> dict[str, bool]:
    """Map each declared (key, action) binding to whether its handler is real.

    The project's TUI-action lesson: a bound key whose ``action_<name>``
    method is missing on its resolving namespace is a SILENT no-op (the key
    fires nothing). This probe checks each declared binding against the
    namespace that owns it (the focused screen or the App), returning
    ``True`` only when a callable ``action_<name>`` handler exists -- so a
    test can assert every mutating key resolves to a non-no-op handler.

    Args:
        bindings: Pairs of (key, action_name) declared by the surface, e.g.
            ``[("k", "cancel_session")]`` for the agent-watch cancel verb.
        namespace: The DOM node the bound key resolves its action against
            (typically the mounted screen, falling through to the App).

    Returns:
        A ``{key: resolved}`` map; ``resolved`` is ``True`` iff a callable
        ``action_<action_name>`` handler exists on *namespace*.
    """
    resolved: dict[str, bool] = {}
    for key, action_name in bindings:
        handler = getattr(namespace, f"action_{action_name}", None)
        resolved[key] = callable(handler)
    return resolved


def capture_screen_text(app: App[object]) -> str:
    """Capture the app's active screen as plain ASCII text.

    Renders the topmost screen on the stack (a base scope screen or a
    pushed modal overlay) row-by-row via its compositor, joining the
    per-row text with newlines. Trailing whitespace is trimmed per row so
    the golden is not padded out to the terminal width on every line.

    The app MUST already be mounted and settled — call after
    ``await pilot.pause()`` inside an ``async with app.run_test()`` block.

    Trailing all-blank rows are dropped: a modal overlay renders only its
    own box, leaving a variable number of empty terminal rows below it
    whose count is not part of the meaningful frame. Trimming them keeps
    the golden anchored to the last content row so the comparison does not
    churn on incidental terminal-height padding.

    Args:
        app: The live :class:`~textual.app.App` under a Pilot harness.

    Returns:
        The rendered screen as a newline-joined ASCII-text block (no
        trailing blank rows, no trailing newline).
    """
    compositor = app.screen._compositor
    rows = [strip.text.rstrip() for strip in compositor.render_strips()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows)


def toast_messages(app: App[object]) -> tuple[str, ...]:
    """Return the message text of every toast currently on *app*'s rack.

    Reads Textual's notification rack (there is no public accessor) so a
    Pilot test can assert an action outcome surfaced as a fading toast via
    :func:`~eawf.surfaces.tui.toast_emitter.notify_result` rather than as a
    persistent result line. Order follows the rack's insertion order.

    Args:
        app: The live :class:`~textual.app.App` under a Pilot harness.

    Returns:
        Each queued toast's message markup, oldest first.
    """
    return tuple(note.message for note in app._notifications)


#: The cursor-position attributes the response probe reads off the focused
#: widget, in resolution order. A row-cursor move (DataTable) / branch-cursor
#: move (Tree) / option highlight (OptionList / ListView) repaints only the
#: highlighted row's STYLE, not its text, so :func:`capture_screen_text` (a
#: plain-text dump) cannot see it. Snapshotting the focused widget's cursor
#: coordinate closes that blind spot: a selection move IS a visible frame delta
#: to the operator, so the probe must count it as a response. The first
#: attribute the focused widget carries wins; a widget with none contributes
#: only its type name (still enough to notice a focus handoff).
_CURSOR_ATTRS: tuple[str, ...] = (
    "cursor_coordinate",
    "cursor_line",
    "highlighted",
    "cursor_node",
    "cursor_row",
)


def _focus_cursor_signature(app: App[object]) -> tuple[str, str, str] | None:
    """Snapshot the focused widget's identity + cursor position for delta checks.

    Reads the first of :data:`_CURSOR_ATTRS` the focused widget exposes so a
    pure selection / scroll-cursor move (which does not change any rendered
    text) is still observable as a response. Returns ``None`` when nothing is
    focused.

    Args:
        app: The live app under a Pilot harness.

    Returns:
        A ``(widget_type, cursor_attr, cursor_repr)`` triple for the focused
        widget, or ``None`` when no widget holds focus.
    """
    focused = app.focused
    if focused is None:
        return None
    for attr in _CURSOR_ATTRS:
        if hasattr(focused, attr):
            return (type(focused).__name__, attr, repr(getattr(focused, attr)))
    return (type(focused).__name__, "", "")


def _selection_signature(app: App[object]) -> tuple[tuple[str, str, str], ...] | None:
    """Snapshot the active surface's SELECTION identity for movement checks.

    A frame delta is too weak a liveness signal for a navigation key: a key that
    only SCROLLS the pane repaints new pixels while the selection cursor stays
    put -- the pre-W01 arrow-trap, where the roadmap-tree arrows scrolled the
    pane without advancing its cursor, yet the frame changed. This collects
    every selection identity the topmost screen exposes so a movement key can be
    held to actually MOVING it:

    * the focused widget's REAL cursor position (a DataTable row cursor, a Tree
      branch cursor, an OptionList highlight) via :func:`_focus_cursor_signature`
      -- included only when the focused widget carries an actual cursor attribute
      (the degenerate ``(type, "", "")`` fallback is NOT a selection identity, so
      a focused-but-cursorless widget does not spuriously anchor the signature);
      and
    * every screen-or-descendant widget carrying an integer ``selected`` index
      (the research board's flat tree cursor, the agent-watch lane grid / session
      picker), paired with its widget identity so two panes' cursors never alias.

    Returns ``None`` when the surface exposes no selection identity at all, so
    :func:`assert_footer_movement_key_moves_selection` can fall back to the
    frame-change liveness assertion and keep the gate total.

    Args:
        app: The live app under a Pilot harness, already settled.

    Returns:
        A tuple of ``(widget_type, identity, value)`` selection markers, or
        ``None`` when the surface exposes no selection identity.
    """
    signature: list[tuple[str, str, str]] = []
    cursor = _focus_cursor_signature(app)
    if cursor is not None and cursor[1]:
        signature.append(cursor)
    for node in app.screen.walk_children(with_self=True):
        selected = getattr(node, "selected", None)
        if isinstance(selected, bool) or not isinstance(selected, int):
            continue
        signature.append((type(node).__name__, node.id or "", repr(selected)))
    return tuple(signature) if signature else None


@dataclass(frozen=True)
class FooterKeyResponse:
    """The observable response of pressing one advertised footer key.

    A footer key is "live" when its press moves the visible surface in ANY
    operator-visible way. The four channels below are OR-ed into
    :attr:`responds`; a key that trips none of them is a silent no-op -- the
    exact defect the interaction-liveness gate exists to catch.

    Attributes:
        key: The Textual pilot key string that was pressed.
        frame_changed: The normalised plain-text frame differs (content
            appeared / changed / a tree branch expanded).
        toast_added: A new toast landed on the rack (an action outcome
            surfaced as a fading notification rather than a frame edit).
        screen_changed: The active screen identity changed (a modal was
            pushed, or a mode / scope switch swapped the base screen).
        cursor_moved: The focused widget's cursor / selection position changed
            (a row-cursor or tree-cursor move that repaints only a highlight).
    """

    key: str
    frame_changed: bool
    toast_added: bool
    screen_changed: bool
    cursor_moved: bool

    @property
    def responds(self) -> bool:
        """Whether the key produced any operator-visible response."""
        return self.frame_changed or self.toast_added or self.screen_changed or self.cursor_moved


async def probe_footer_key_response(pilot: Pilot[object], key: str) -> FooterKeyResponse:
    """Press *key*, settle, and report the composite visible response.

    Captures the four response channels (:class:`FooterKeyResponse`) around a
    single key press: the normalised text frame, the toast-rack depth, the
    active-screen identity, and the focused widget's cursor position. The frame
    is normalised (:func:`normalize_snapshot`) so the volatile header clock does
    not read as a spurious response, and the settle (:func:`settle_screen`,
    which drains background workers first) lets any worker-offloaded action land
    before the after-capture.

    This is the observation primitive :func:`assert_footer_key_responds` builds
    on; it never raises, so a caller sweeping many keys can inspect the record
    (e.g. to prove an exempted key is genuinely inert). Modal / mode-switch
    residue is NOT cleaned up here -- the caller owns teardown so it can inspect
    the pushed surface first.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
        key: The Textual pilot key string to press (e.g. ``"enter"``,
            ``"down"``, ``"question_mark"``).

    Returns:
        The :class:`FooterKeyResponse` describing what moved.
    """
    app = pilot.app
    before_frame = normalize_snapshot(capture_screen_text(app))
    before_toasts = len(toast_messages(app))
    before_screen = id(app.screen)
    before_cursor = _focus_cursor_signature(app)
    await pilot.press(key)
    await settle_screen(pilot)
    return FooterKeyResponse(
        key=key,
        frame_changed=normalize_snapshot(capture_screen_text(app)) != before_frame,
        toast_added=len(toast_messages(app)) > before_toasts,
        screen_changed=id(app.screen) != before_screen,
        cursor_moved=_focus_cursor_signature(app) != before_cursor,
    )


async def assert_footer_key_responds(
    pilot: Pilot[object],
    key: str,
    *,
    hint: str | None = None,
) -> FooterKeyResponse:
    """Assert an advertised footer key produces a visible response, else raise.

    The interaction-liveness contract: a key the footer advertises must DO
    something the operator can see -- a frame delta, a toast, a screen change,
    or a selection-cursor move. This lifts the older
    :func:`mutating_action_keys_resolve` check from "an ``action_<name>``
    handler EXISTS" up to "the key VISIBLY RESPONDS", closing the gap a live
    Pilot probe found where an advertised key resolved to a handler that
    silently no-oped.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
        key: The Textual pilot key string to press.
        hint: Optional footer-hint label (e.g. ``"Enter open"``) folded into
            the failure message so a dead key names the affordance it backs.

    Returns:
        The :class:`FooterKeyResponse` on a live key (for further inspection).

    Raises:
        AssertionError: When the key produced no visible response on any of the
            four channels (a silently dead advertised key).
    """
    response = await probe_footer_key_response(pilot, key)
    if not response.responds:
        detail = f"{key!r}" if hint is None else f"{key!r} ({hint})"
        raise AssertionError(
            f"advertised footer key {detail} produced no visible response: "
            f"no frame delta, toast, screen change, or cursor move"
        )
    return response


async def assert_footer_movement_key_moves_selection(
    pilot: Pilot[object],
    key: str,
    *,
    hint: str | None = None,
) -> FooterKeyResponse:
    """Assert an advertised MOVEMENT key moves the selection, not merely repaints.

    Tightens :func:`assert_footer_key_responds` for navigation keys (up / down /
    left / right). The plain frame-delta channel is too weak for a movement key:
    a key that only SCROLLS its pane changes the frame while the selection cursor
    stays put -- the pre-W01 arrow-trap defect, where the roadmap-tree arrows
    scrolled the pane without advancing its cursor, so the frame-delta gate
    counted the scroll as a response and passed a DEAD selection key. This helper
    closes that blind spot: it captures the active surface's selection identity
    (:func:`_selection_signature`) around the press and asserts it CHANGED. When
    the surface exposes no selection identity, it defers to
    :func:`assert_footer_key_responds` so the gate stays total over every mode.

    Args:
        pilot: The live :class:`~textual.pilot.Pilot` from ``app.run_test()``.
        key: The Textual pilot key string to press (a movement / navigation key).
        hint: Optional footer-hint label (e.g. ``"research up-down"``) folded into
            the failure message so a dead key names the affordance it backs.

    Returns:
        The :class:`FooterKeyResponse` for the press (for further inspection).

    Raises:
        AssertionError: When the surface exposes a selection identity and the
            movement key did NOT move it (a scroll-only or dead navigation key),
            or -- through the frame-change fallback on a selection-less surface --
            when the key produced no visible response at all.
    """
    before = _selection_signature(pilot.app)
    if before is None:
        return await assert_footer_key_responds(pilot, key, hint=hint)
    response = await probe_footer_key_response(pilot, key)
    after = _selection_signature(pilot.app)
    if after == before:
        detail = f"{key!r}" if hint is None else f"{key!r} ({hint})"
        raise AssertionError(
            f"advertised movement key {detail} did not move the selection: "
            f"selection identity unchanged at {before!r}; a navigation key that "
            f"only scrolls the pane without advancing the cursor is a dead "
            f"selection key (the pre-W01 arrow-trap)"
        )
    return response


def assert_screen_snapshot(app: App[object], golden_path: Path) -> None:
    """Compare the app's active screen against a golden ASCII fixture.

    Captures the active screen (:func:`capture_screen_text`), normalises
    the volatile clock cell (:func:`normalize_snapshot`), and asserts byte
    equality against *golden_path*. When :data:`SNAPSHOT_REGEN_ENV` is
    ``"1"`` the golden is (re)written from the live capture and no
    assertion runs — the regeneration escape hatch.

    Args:
        app: The live app under a Pilot harness, already settled.
        golden_path: Path to the golden ``.txt`` fixture. Created (with
            parents) on regen if absent.

    Raises:
        AssertionError: When the normalised capture differs from the
            golden and regeneration is not requested. The message embeds a
            line-tagged :func:`difflib.unified_diff` of golden (expected)
            vs capture (actual), capped at :data:`_DRIFT_DIFF_MAX_LINES`
            lines, so a residual CI drift shows the exact differing rows.
    """
    captured = normalize_snapshot(capture_screen_text(app))
    if os.environ.get(SNAPSHOT_REGEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(captured + "\n", encoding="utf-8")
        return
    expected = golden_path.read_text(encoding="utf-8").rstrip("\n")
    if captured != expected:
        raise AssertionError(_drift_message(golden_path, expected, captured))


async def capture_mockup_golden_screen_text(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> str:
    """Mount the TUI through Pilot and capture the target mockup screen.

    The mockup close gate needs the same stable text capture as the snapshot
    suite, but it reaches the target screen from a typed gate row rather than
    a hand-written pytest. This helper launches :class:`EaApp`, waits for the
    bound state to settle, optionally switches to a mode, optionally presses a
    key sequence to reach an overlay / subview, then returns the normalised
    active-screen text.

    Args:
        scope: Launch nav scope (``repo`` / ``workspace`` / ``user``).
        state_path: Fixture or live ``state.json`` path to bind, or ``None``.
        mode: Optional TUI mode to switch to before pressing keys.
        key_sequence: Textual key strings to press after mode switch.
        size: Pilot terminal size as ``(cols, rows)``.

    Returns:
        Normalised ASCII screen text with no trailing newline.
    """
    from eawf.surfaces.tui.app import EaApp

    app = EaApp(scope=scope, state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        if mode is not None:
            await app.switch_mode(mode)
            await settle_screen(pilot)
        for key in key_sequence:
            await pilot.press(key)
            await settle_screen(pilot)
        return await settle_screen(pilot)


def capture_mockup_golden_screen_text_sync(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> str:
    """Run :func:`capture_mockup_golden_screen_text` from a sync caller.

    The audit-DSL runner is synchronous, but daemon close-gate calls can happen
    while an event loop is already running. Mirror the existing TUI gate
    pattern: run inline when no loop is active, otherwise offload to a worker
    thread with its own loop.
    """

    def _run() -> str:
        return asyncio.run(
            capture_mockup_golden_screen_text(
                scope=scope,
                state_path=state_path,
                mode=mode,
                key_sequence=key_sequence,
                size=size,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def resvg_available() -> bool:
    """Whether the pinned ``resvg`` rasterizer CLI is on PATH."""
    return shutil.which(_RESVG) is not None


#: The broad monospace font stack a review SVG declares so a machine WITHOUT
#: Fira Code still renders the frame's box-drawing + reskin sigil glyphs.
#: Textual's ``App.export_screenshot`` hardcodes rich's SVG template, which
#: names only ``Fira Code`` (rich's ``Console.export_svg`` exposes no
#: font-family knob); a viewer lacking that font substitutes one that drops the
#: box corners + cosmic sigils, so the review SVG renders as tofu. This stack
#: tries JetBrains Mono first (the project's rendering font), then Fira Code,
#: then DejaVu Sans Mono, then the generic ``monospace`` -- the first family the
#: viewer has wins, and every listed family carries the glyphs.
_REVIEW_FONT_STACK: str = '"JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace'

#: The two exact ``font-family`` declarations rich's SVG template emits that
#: name Fira Code alone: the ``@font-face`` family name and the terminal
#: ``.matrix`` rule. :func:`export_screenshot_svg` rewrites each to
#: :data:`_REVIEW_FONT_STACK`; the ``arial`` title rule and every other byte of
#: the export are left untouched.
_FIRA_FONT_FAMILY_DECLS: tuple[str, ...] = (
    'font-family: "Fira Code";',
    "font-family: Fira Code, monospace;",
)


def export_screenshot_svg(app: App[object], *, title: str | None = None) -> str:
    """Export the app's active screen as an SVG with a broad monospace font stack.

    Wraps :meth:`~textual.app.App.export_screenshot` and rewrites the Fira
    Code-only ``font-family`` declarations rich's SVG template hardcodes to the
    broad :data:`_REVIEW_FONT_STACK`. rich exposes no font-family parameter, so a
    review SVG shipped as-is declares ``Fira Code`` alone; on a machine without
    that font a viewer substitutes one lacking the box-drawing + reskin sigil
    glyphs and the frame renders as tofu. Broadening the stack lets a common
    machine resolve a family that carries the glyphs. Only the two font-family
    declarations change -- every other byte of the export is preserved.

    Args:
        app: The live :class:`~textual.app.App` under a Pilot harness, already
            settled.
        title: Optional SVG title, forwarded to
            :meth:`~textual.app.App.export_screenshot`.

    Returns:
        The exported SVG string with each Fira Code-only ``font-family``
        declaration rewritten to the broad monospace stack, otherwise
        byte-identical to :meth:`~textual.app.App.export_screenshot`.
    """
    svg = app.export_screenshot(title=title)
    for declaration in _FIRA_FONT_FAMILY_DECLS:
        svg = svg.replace(declaration, f"font-family: {_REVIEW_FONT_STACK};")
    return svg


def _rasterize_svg_to_png(svg: str) -> bytes:
    """Rasterize an SVG string to PNG bytes via the pinned ``resvg`` CLI.

    Args:
        svg: The SVG document (a Textual ``App.export_screenshot`` capture).

    Returns:
        The rendered 8-bit RGBA PNG bytes.

    Raises:
        FileNotFoundError: When ``resvg`` is not installed.
        RuntimeError: When the render exits non-zero or produces no file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "screen.svg"
        out = Path(tmp) / "screen.png"
        src.write_text(svg, encoding="utf-8")
        completed = subprocess.run(
            [_RESVG, str(src), str(out)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not out.is_file():
            diagnostic = completed.stderr.strip() or completed.stdout.strip() or "render failed"
            raise RuntimeError(f"resvg render failed: {diagnostic}")
        return out.read_bytes()


async def capture_mockup_golden_screen_png(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> bytes:
    """Mount the TUI through Pilot and capture the target screen as PNG bytes.

    The image sibling of :func:`capture_mockup_golden_screen_text`: it reaches
    the same target screen through the same selectors (``scope`` /
    ``state_path`` / ``mode`` / ``key_sequence`` / ``size``), but instead of
    dumping normalised ASCII it exports the settled screen as an SVG
    (:meth:`~textual.app.App.export_screenshot`) and rasterises that through
    the pinned ``resvg`` chain -- the live-render side of the VIS-1 image
    oracle.

    Args:
        scope: Launch nav scope (``repo`` / ``workspace`` / ``user``).
        state_path: Fixture or live ``state.json`` path to bind, or ``None``.
        mode: Optional TUI mode to switch to before pressing keys.
        key_sequence: Textual key strings to press after mode switch.
        size: Pilot terminal size as ``(cols, rows)``.

    Returns:
        The rendered 8-bit RGBA PNG bytes of the settled screen.

    Raises:
        FileNotFoundError: When ``resvg`` is not installed.
        RuntimeError: When the render exits non-zero or produces no file.
    """
    from eawf.surfaces.tui.app import EaApp

    app = EaApp(scope=scope, state_path=state_path)
    async with app.run_test(size=size) as raw_pilot:
        pilot = cast("Pilot[object]", raw_pilot)
        await settle_screen(pilot)
        if mode is not None:
            await app.switch_mode(mode)
            await settle_screen(pilot)
        for key in key_sequence:
            await pilot.press(key)
            await settle_screen(pilot)
        await settle_screen(pilot)
        svg = app.export_screenshot(title="mockup golden", simplify=True)
    return _rasterize_svg_to_png(svg)


def capture_mockup_golden_screen_png_sync(
    *,
    scope: Literal["repo", "workspace", "user"],
    state_path: Path | None,
    mode: str | None,
    key_sequence: list[str],
    size: tuple[int, int],
) -> bytes:
    """Run :func:`capture_mockup_golden_screen_png` from a sync caller.

    Mirrors :func:`capture_mockup_golden_screen_text_sync`: run inline when no
    event loop is active, otherwise offload to a worker thread with its own
    loop so a daemon close-gate call already inside a loop still resolves.
    """

    def _run() -> bytes:
        return asyncio.run(
            capture_mockup_golden_screen_png(
                scope=scope,
                state_path=state_path,
                mode=mode,
                key_sequence=key_sequence,
                size=size,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def mockup_golden_diff_detail(golden_path: Path, expected: str, captured: str) -> str:
    """Return a capped unified diff detail for a mockup golden mismatch.

    The first unified-diff hunk marker is surfaced in the opening sentence as
    ``region=...`` so close-gate errors name the changed region even when the
    caller truncates the multiline diff.
    """
    diff = difflib.unified_diff(
        expected.splitlines(),
        captured.splitlines(),
        fromfile=f"{golden_path.name} (expected)",
        tofile=f"{golden_path.name} (actual)",
        lineterm="",
    )
    capped = list(itertools.islice(diff, _DRIFT_DIFF_MAX_LINES + 1))
    if len(capped) > _DRIFT_DIFF_MAX_LINES:
        capped[_DRIFT_DIFF_MAX_LINES] = f"... (diff truncated at {_DRIFT_DIFF_MAX_LINES} lines)"
    region = next((line for line in capped if line.startswith("@@")), "whole-file")
    first_change = next(
        (
            line
            for line in capped
            if (line.startswith("-") and not line.startswith("---"))
            or (line.startswith("+") and not line.startswith("+++"))
        ),
        "none",
    )
    body = "\n".join(capped)
    return (
        f"mockup golden mismatch for {golden_path.name!r}: "
        f"region={region} first_change={first_change}\n{body}"
    )


def _drift_message(golden_path: Path, expected: str, captured: str) -> str:
    """Build the drift ``AssertionError`` message with a unified diff.

    Args:
        golden_path: Path to the golden fixture that drifted.
        expected: The golden text (the ``---`` side of the diff).
        captured: The live, normalised capture (the ``+++`` side).

    Returns:
        A multi-line message: a one-line header naming the fixture and the
        regen hatch, followed by a line-tagged unified diff capped at
        :data:`_DRIFT_DIFF_MAX_LINES` lines.
    """
    diff = difflib.unified_diff(
        expected.splitlines(),
        captured.splitlines(),
        fromfile=f"{golden_path.name} (expected)",
        tofile=f"{golden_path.name} (actual)",
        lineterm="",
    )
    capped = list(itertools.islice(diff, _DRIFT_DIFF_MAX_LINES + 1))
    if len(capped) > _DRIFT_DIFF_MAX_LINES:
        capped[_DRIFT_DIFF_MAX_LINES] = f"... (diff truncated at {_DRIFT_DIFF_MAX_LINES} lines)"
    body = "\n".join(capped)
    return (
        f"snapshot drift for {golden_path.name!r}; "
        f"regenerate with {SNAPSHOT_REGEN_ENV}=1 uv run pytest tests/snapshots/tui/\n"
        f"{body}"
    )


__all__ = [
    "SNAPSHOT_REGEN_ENV",
    "FooterKeyResponse",
    "assert_footer_key_responds",
    "assert_footer_movement_key_moves_selection",
    "assert_screen_snapshot",
    "capture_mockup_golden_screen_png",
    "capture_mockup_golden_screen_png_sync",
    "capture_mockup_golden_screen_text",
    "capture_mockup_golden_screen_text_sync",
    "capture_screen_text",
    "export_screenshot_svg",
    "mockup_golden_diff_detail",
    "mutating_action_keys_resolve",
    "normalize_snapshot",
    "probe_footer_key_response",
    "push_state_revision",
    "quiesce_volatile_chrome",
    "resvg_available",
    "settle_screen",
    "tick_poll_backstop",
    "toast_messages",
]
