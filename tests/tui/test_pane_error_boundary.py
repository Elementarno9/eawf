"""Pilot tests for the per-pane render-exception boundary (P30-I02-W26).

Contract 5 of the cosmic-terminal reskin is the ONE I00 wave that adds
BEHAVIOUR: an app-authored render-exception boundary. Textual has no native
per-widget error boundary -- an uncaught widget content-build / ``render()``
exception propagates to :meth:`~textual.app.App._handle_exception` and takes
the whole App into a fatal :meth:`~textual.app.App.panic` screen. The
boundary generalises the codebase's existing honest-empty fallback-mount
idiom from a *data* failure to a *render* exception: it runs a pane's
content builder guarded and, on any exception, mounts a calm crash frame
(the ``✕`` FAIL sigil + the literal recovery copy + the ``r`` / ``l`` /
``Esc`` affordances) in place of the content rather than escalating.

These tests:

* pin the crash-frame copy (the FAIL sigil + literal copy) as a golden;
* mount several panes wrapped in boundaries, inject a render exception into
  ONE, and assert the boundary frame renders, the neighbouring panes still
  render, the failed pane never escalates to ``App.panic`` (the App captures
  no exception), and a healthy pane renders no boundary frame;
* assert ``affordance_parity`` -- the ``r`` / ``l`` / ``Esc`` keys resolve
  to live ``Binding`` actions on the boundary AND fire (retry rebuilds, view
  log switches to the feed mode, dismiss clears the frame).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Static

from eawf.surfaces.tui.app import (
    PANE_CRASH_HEADLINE,
    PANE_CRASH_HINTS,
    EaApp,
    PaneErrorBoundary,
    render_pane_crash_frame,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

#: EaApp's default render mode is the unicode column, so the asserted FAIL
#: sigil is the unicode cross.
_FAIL_UNICODE = glyph(Sigil.FAILED, mode="unicode")
_FAIL_ASCII = glyph(Sigil.FAILED, mode="ascii")


def _healthy_builder(text: str) -> Widget:
    """Return a builder that yields a plain healthy content widget."""
    return Static(text, classes="healthy-content")


def _exploding_builder() -> Widget:
    """A builder that raises a render exception while building the content.

    Mirrors a widget that raises mid-paint: the boundary runs the builder,
    the builder raises, and the boundary must mount the crash frame in place
    rather than letting the exception escalate to ``App.panic``.

    Raises:
        RuntimeError: Always -- the injected render exception.
    """
    raise RuntimeError("injected render explosion")


def _binding_for(widget: object, key: str) -> Binding:
    """Return the live ``Binding`` *key* resolves to on *widget*.

    Args:
        widget: The boundary whose ``BINDINGS`` to scan.
        key: The bound key string (e.g. ``"r"``).

    Returns:
        The first ``Binding`` declared for *key*.

    Raises:
        AssertionError: When *key* has no live binding on *widget*.
    """
    for binding in widget.BINDINGS:  # type: ignore[attr-defined]
        if isinstance(binding, Binding) and binding.key == key:
            return binding
    raise AssertionError(f"no live binding for key {key!r}")


# --------------------------------------------------------------------------
# render_pane_crash_frame -- the crash-frame copy golden
# --------------------------------------------------------------------------


def test_render_pane_crash_frame_unicode_golden() -> None:
    """The crash frame leads with the unicode FAIL cross + the literal copy."""
    frame = render_pane_crash_frame("roadmap", mode="unicode")
    expected = (
        f"[$err]{_FAIL_UNICODE} {PANE_CRASH_HEADLINE}[/]\n"
        "roadmap raised mid-paint. Your work is safe -- the daemon kept running.\n"
        f"[$muted]{PANE_CRASH_HINTS}[/]"
    )
    assert frame == expected


def test_render_pane_crash_frame_ascii_uses_ascii_fail_glyph() -> None:
    """The ASCII column swaps the cross for the ``x`` fail glyph."""
    frame = render_pane_crash_frame("status", mode="ascii")
    assert _FAIL_ASCII in frame
    assert _FAIL_UNICODE not in frame
    assert PANE_CRASH_HEADLINE in frame


def test_render_pane_crash_frame_names_the_offending_widget() -> None:
    """The reassurance line names the failed pane so the operator sees which."""
    frame = render_pane_crash_frame("git-pane", mode="unicode")
    assert "git-pane raised mid-paint" in frame


def test_render_pane_crash_frame_carries_the_literal_affordance_hints() -> None:
    """The frame trails the literal r / l / Esc recovery affordance copy."""
    frame = render_pane_crash_frame("backlog", mode="unicode")
    assert "r retry" in frame
    assert "l view log" in frame
    assert "Esc dismiss" in frame


def test_render_pane_crash_frame_escapes_a_bracketed_widget_name() -> None:
    """A bracketed widget name renders literally (no markup-tag swallow)."""
    frame = render_pane_crash_frame("[P30-W26]", mode="unicode")
    assert "\\[P30-W26]" in frame


# --------------------------------------------------------------------------
# PaneErrorBoundary -- inject a render exception into one pane
# --------------------------------------------------------------------------


def test_boundary_mounts_crash_frame_when_builder_raises() -> None:
    """A boundary whose builder raises mounts the crash frame, not the content."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_exploding_builder, pane_id="roadmap")
            await app.screen.mount(boundary)
            await pilot.pause()
            # The crash frame is mounted in place of the (never-built) content.
            frames = boundary.query(".pane-crash-frame")
            assert len(frames) == 1
            rendered = str(frames.first(Static).render())
            # The FAIL sigil LEADS the calm headline.
            assert _FAIL_UNICODE in rendered
            assert rendered.index(_FAIL_UNICODE) < rendered.index(PANE_CRASH_HEADLINE)
            assert "roadmap raised mid-paint" in rendered
            assert "Your work is safe" in rendered
            # No healthy content widget was mounted.
            assert not boundary.query(".healthy-content")

    asyncio.run(body())


def test_one_pane_exception_does_not_escalate_to_app_panic() -> None:
    """A single failed pane never panics the App: it captures no exception."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_exploding_builder, pane_id="roadmap")
            await app.screen.mount(boundary)
            await pilot.pause()
            # The boundary swallowed the render exception: the App captured
            # none, so run_test will not re-raise on exit and no panic fired.
            assert app._exception is None
            assert boundary.query(".pane-crash-frame")

    asyncio.run(body())


def test_neighbouring_panes_keep_rendering_when_one_pane_fails() -> None:
    """A failed pane's exception does not propagate -- neighbours still render."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            healthy_left = app.pane_boundary(
                builder=lambda: _healthy_builder("left pane"), pane_id="status"
            )
            broken = app.pane_boundary(builder=_exploding_builder, pane_id="roadmap")
            healthy_right = app.pane_boundary(
                builder=lambda: _healthy_builder("right pane"), pane_id="backlog"
            )
            await app.screen.mount(healthy_left)
            await app.screen.mount(broken)
            await app.screen.mount(healthy_right)
            await pilot.pause()
            # The broken pane shows the crash frame...
            assert broken.query(".pane-crash-frame")
            # ...while BOTH neighbours rendered their healthy content and NO
            # crash frame, so the one exception did not bleed across siblings.
            for neighbour, label in ((healthy_left, "left pane"), (healthy_right, "right pane")):
                content = neighbour.query(".healthy-content")
                assert len(content) == 1
                assert label in str(content.first(Static).render())
                assert not neighbour.query(".pane-crash-frame")
            # And the App never panicked.
            assert app._exception is None

    asyncio.run(body())


def test_healthy_pane_renders_no_boundary_crash_frame() -> None:
    """A healthy builder mounts its content with no crash frame at all."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(
                builder=lambda: _healthy_builder("all good"), pane_id="status"
            )
            await app.screen.mount(boundary)
            await pilot.pause()
            assert not boundary.query(".pane-crash-frame")
            content = boundary.query(".healthy-content")
            assert len(content) == 1
            assert "all good" in str(content.first(Static).render())

    asyncio.run(body())


# --------------------------------------------------------------------------
# affordance_parity -- r / l / Esc resolve to live Bindings AND fire
# --------------------------------------------------------------------------


def test_boundary_affordance_parity_keys_resolve_to_live_bindings() -> None:
    """affordance_parity: r / l / Esc resolve to live Bindings naming handlers."""
    assert _binding_for(PaneErrorBoundary, "r").action == "retry"
    assert _binding_for(PaneErrorBoundary, "l").action == "view_log"
    assert _binding_for(PaneErrorBoundary, "escape").action == "dismiss"
    for action in ("action_retry", "action_view_log", "action_dismiss"):
        assert callable(getattr(PaneErrorBoundary, action))


def test_boundary_retry_key_rebuilds_the_content() -> None:
    """The r affordance fires: a builder that fails once then succeeds recovers."""
    attempts = {"n": 0}

    def _flaky_builder() -> Widget:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return _healthy_builder("recovered")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_flaky_builder, pane_id="roadmap")
            await app.screen.mount(boundary)
            await pilot.pause()
            # First build failed -> crash frame.
            assert boundary.query(".pane-crash-frame")
            boundary.focus()
            await pilot.press("r")
            await pilot.pause()
            # The retry rebuilt clean: the crash frame is gone, content shows.
            assert not boundary.query(".pane-crash-frame")
            content = boundary.query(".healthy-content")
            assert len(content) == 1
            assert "recovered" in str(content.first(Static).render())
            assert app._exception is None

    asyncio.run(body())


def test_boundary_dismiss_key_clears_the_crash_frame() -> None:
    """The Esc affordance fires: a focused-boundary Esc clears the crash frame.

    With the boundary focused, ``Esc`` resolves to the boundary's own
    ``dismiss`` binding (the focused widget's binding map is consulted before
    the app-wide quit), so the real key press clears the frame.
    """

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_exploding_builder, pane_id="roadmap")
            await app.screen.mount(boundary)
            await pilot.pause()
            assert boundary.query(".pane-crash-frame")
            boundary.focus()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not boundary.query(".pane-crash-frame")
            assert len(boundary.children) == 0

    asyncio.run(body())


def test_boundary_view_log_key_switches_to_the_feed_mode() -> None:
    """The l affordance fires: a focused-boundary l switches to the feed mode."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_exploding_builder, pane_id="roadmap")
            await app.screen.mount(boundary)
            await pilot.pause()
            boundary.focus()
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            assert app.current_mode == "feed"
            assert app._exception is None

    asyncio.run(body())


def test_boundary_handles_a_non_runtime_render_exception() -> None:
    """Error-path: a builder raising ValueError is caught the same way.

    The boundary catches the broad ``Exception`` family, so a render failure
    of any concrete type degrades to the crash frame rather than escalating.
    """

    def _value_error_builder() -> Widget:
        raise ValueError("bad render value")

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            boundary = app.pane_boundary(builder=_value_error_builder, pane_id="status")
            await app.screen.mount(boundary)
            await pilot.pause()
            assert boundary.query(".pane-crash-frame")
            assert app._exception is None

    asyncio.run(body())


@pytest.mark.parametrize("pane_id", ["roadmap", "status", "git-pane", "backlog"])
def test_boundary_id_is_attributable_to_its_pane(pane_id: str) -> None:
    """The boundary id carries its pane id so a failure is attributable."""
    boundary = PaneErrorBoundary(builder=_exploding_builder, pane_id=pane_id)
    assert boundary.id == f"pane-boundary-{pane_id}"
