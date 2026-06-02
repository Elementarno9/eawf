"""Unit + Pilot tests for the C06 shared ``Header`` widget.

Covers the pure render source (:func:`build_breadcrumb`,
:func:`active_runtime_id`, :func:`runtime_cell_text`,
:func:`render_header`): the full-location ``scope > code > phase > iter >
mode`` order with the optional trailing entity segment, the per-segment
``[@click=...]`` nav wiring (clickable only for existing actions), the
None-state fallback frame, the real runtime cell (runtime id + running
count vs honest idle), and a Pilot-driven paint under the real palette
confirming the ``Eä`` brand reaches the rendered screen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
from textual.app import ComposeResult

from eawf.kernel.state.enums import ScopeKind
from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.widgets.header import (
    BRAND,
    CRUMB_SEP,
    DEFAULT_PROJECT_CODE,
    RUNTIME_IDLE,
    Header,
    active_runtime_id,
    build_breadcrumb,
    render_header,
    runtime_cell_text,
)

from ._palette_harness import PaletteHarnessApp

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_EMPTY_REPO = _FIXTURES / "01-empty-repo.json"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"
_WORKSPACE = _FIXTURES / "05-workspace-state.json"
_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

#: The active fixture's facts, pinned so the assertions read clearly.
_CODE = "QR"
_PHASE = "P01"
_ITER = "P01-I01"
_WAVE = "P01-I01-W01"


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Header(id="hdr")


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


def _state_with_active_wave() -> State:
    """Return the active fixture with one wave id pinned into ``current``.

    The fixture carries no agent session, so this exercises the
    runtime-unknown path (count without a resolvable runtime id).
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = [_WAVE]
    return State.model_validate(payload)


def _state_with_active_runtime(*, runtime: str = "claude", count: int = 2) -> State:
    """Return the active fixture with *count* active waves + ACTIVE sessions.

    Injects *count* ACTIVE agent sessions on *runtime* so the runtime cell
    resolves a real runtime id alongside the running count.
    """
    payload = orjson.loads(_PHASE_ITER_WAVE.read_bytes())
    payload["current"]["active_wave_ids"] = [f"P01-I01-W{n:02d}" for n in range(1, count + 1)]
    payload["agent_sessions"] = {
        f"S{n:02d}": {
            "id": f"S{n:02d}",
            "role": "executor",
            "runtime": runtime,
            "scope_id": _CODE,
            "status": "active",
            "started_at": f"2026-05-08T00:0{n}:00Z",
        }
        for n in range(1, count + 1)
    }
    return State.model_validate(payload)


# --------------------------------------------------------------------------
# build_breadcrumb — full-location order, segments, fallbacks
# --------------------------------------------------------------------------


def test_build_breadcrumb_none_state_falls_back_to_default_code() -> None:
    assert build_breadcrumb(None) == DEFAULT_PROJECT_CODE


def test_build_breadcrumb_repo_fixture_includes_scope_and_code() -> None:
    crumb = build_breadcrumb(_load(_EMPTY_REPO))
    assert "repo" in crumb
    assert _CODE in crumb
    assert CRUMB_SEP in crumb


def test_build_breadcrumb_scope_leads_code_follows() -> None:
    # scope > code: the broad-to-specific full-location order leads with scope.
    crumb = build_breadcrumb(_load(_EMPTY_REPO))
    assert crumb.index("repo") < crumb.index(_CODE)


def test_build_breadcrumb_full_order_scope_code_phase_iter_mode() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home")
    segments = [seg.strip() for seg in crumb.split(CRUMB_SEP.strip())]
    assert segments == ["repo", _CODE, _PHASE, _ITER, "Home"]


def test_build_breadcrumb_includes_iter_when_active() -> None:
    # The active fixture has an iter pinned -> iter segment present, after phase.
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE))
    assert _ITER in crumb
    assert crumb.index(_PHASE) < crumb.index(_ITER)


def test_build_breadcrumb_omits_iter_when_no_iter_active() -> None:
    # The empty-repo fixture has no active iter -> no iter segment.
    crumb = build_breadcrumb(_load(_EMPTY_REPO))
    assert "-I" not in crumb


def test_build_breadcrumb_mode_trails_not_leads() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home")
    # Mode now trails the location, not leads it.
    assert crumb.index("Home") > crumb.index(_ITER)


def test_build_breadcrumb_entity_segment_trails_when_supplied() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home", entity="ART-7")
    segments = [seg.strip() for seg in crumb.split(CRUMB_SEP.strip())]
    assert segments == ["repo", _CODE, _PHASE, _ITER, "Home", "ART-7"]


def test_build_breadcrumb_entity_segment_absent_when_not_supplied() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home")
    assert "ART-7" not in crumb


def test_build_breadcrumb_workspace_uses_default_code_when_no_project() -> None:
    crumb = build_breadcrumb(_load(_WORKSPACE))
    assert "workspace" in crumb
    assert DEFAULT_PROJECT_CODE in crumb


def test_build_breadcrumb_user_scope_override_leads_user() -> None:
    # The user screen passes scope="user" over a workspace-shaped state.
    crumb = build_breadcrumb(_load(_WORKSPACE), "user")
    assert crumb.startswith("user")


def test_build_breadcrumb_none_state_mode_trails_default_code() -> None:
    crumb = build_breadcrumb(None, mode="Home")
    segments = [seg.strip() for seg in crumb.split(CRUMB_SEP.strip())]
    assert segments == [DEFAULT_PROJECT_CODE, "Home"]


# --------------------------------------------------------------------------
# build_breadcrumb — clickable @click wiring (app.-namespaced existing actions)
# --------------------------------------------------------------------------


def test_build_breadcrumb_plain_by_default_has_no_click_markup() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home", mode_name="home")
    assert "@click=" not in crumb


def test_build_breadcrumb_clickable_wires_scope_to_switch_scope() -> None:
    # The action is app.-namespaced so Textual resolves it against the host
    # App (which defines action_switch_scope), not the Static owning the link.
    crumb = build_breadcrumb(_load(_EMPTY_REPO), "repo", clickable=True)
    assert "[@click=app.switch_scope('repo')]repo[/]" in crumb


def test_build_breadcrumb_clickable_wires_code_to_home_mode() -> None:
    crumb = build_breadcrumb(_load(_EMPTY_REPO), clickable=True)
    assert f"[@click=app.switch_mode('home')]{_CODE}[/]" in crumb


def test_build_breadcrumb_clickable_wires_phase_and_iter_to_ref_actions() -> None:
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), clickable=True)
    assert f"[@click=app.open_phase_ref('{_PHASE}')]{_PHASE}[/]" in crumb
    assert f"[@click=app.open_iter_ref('{_ITER}')]{_ITER}[/]" in crumb


def test_build_breadcrumb_clickable_wires_mode_by_name_not_title() -> None:
    # research_board's title is "Research" but the click target is the name.
    crumb = build_breadcrumb(
        _load(_PHASE_ITER_WAVE), mode="Research", mode_name="research_board", clickable=True
    )
    assert "[@click=app.switch_mode('research_board')]Research[/]" in crumb


def test_build_breadcrumb_clickable_namespaces_every_action_to_app() -> None:
    # Regression guard for the no-op-click bug: a bare markup-link action
    # resolves against the Static owning the link (which defines none of
    # these), silently no-opping the click. Every clickable action MUST be
    # app.-prefixed so it resolves against the host App that owns the action
    # methods. This pins all four breadcrumb action verbs in one assertion.
    crumb = build_breadcrumb(
        _load(_PHASE_ITER_WAVE),
        "repo",
        mode="Research",
        mode_name="research_board",
        clickable=True,
    )
    assert "@click=app.switch_scope" in crumb
    assert "@click=app.switch_mode" in crumb
    assert "@click=app.open_phase_ref" in crumb
    assert "@click=app.open_iter_ref" in crumb
    # And no bare (non-namespaced) action leaks through -- every @click is app.-scoped.
    for verb in ("switch_scope", "switch_mode", "open_phase_ref", "open_iter_ref"):
        assert f"@click={verb}" not in crumb


def test_build_breadcrumb_clickable_mode_plain_without_name() -> None:
    # No mode_name => the mode segment stays plain text (no dangling action).
    # (The code segment still links to app.switch_mode('home'); only the
    # trailing mode segment must be plain.)
    crumb = build_breadcrumb(_load(_PHASE_ITER_WAVE), mode="Home", clickable=True)
    last_segment = crumb.rsplit(CRUMB_SEP, 1)[-1]
    assert last_segment == "Home"
    assert "@click" not in last_segment


def test_build_breadcrumb_clickable_entity_segment_is_plain() -> None:
    # The entity segment has no generic nav action => never clickable.
    crumb = build_breadcrumb(
        _load(_PHASE_ITER_WAVE), mode="Home", mode_name="home", entity="ART-7", clickable=True
    )
    assert crumb.endswith("ART-7")
    assert "@click" not in crumb.rsplit(CRUMB_SEP, 1)[-1]


def test_build_breadcrumb_clickable_escapes_entity_brackets() -> None:
    # A bracketed entity label is markup-escaped so it renders literally.
    crumb = build_breadcrumb(
        _load(_PHASE_ITER_WAVE), mode="Home", mode_name="home", entity="[P01-W01]", clickable=True
    )
    assert "\\[P01-W01]" in crumb


# --------------------------------------------------------------------------
# active_runtime_id + runtime_cell_text — real runtime cell
# --------------------------------------------------------------------------


def test_active_runtime_id_none_state_is_none() -> None:
    assert active_runtime_id(None) is None


def test_active_runtime_id_no_sessions_is_none() -> None:
    # The empty-repo fixture has no agent sessions.
    assert active_runtime_id(_load(_EMPTY_REPO)) is None


def test_active_runtime_id_returns_active_session_runtime() -> None:
    assert active_runtime_id(_state_with_active_runtime(runtime="codex")) == "codex"


def test_runtime_cell_text_none_state_is_idle() -> None:
    assert runtime_cell_text(None) == f"runtime: {RUNTIME_IDLE}"


def test_runtime_cell_text_no_active_wave_is_idle() -> None:
    assert runtime_cell_text(_load(_EMPTY_REPO)) == f"runtime: {RUNTIME_IDLE}"


def test_runtime_cell_text_active_with_runtime_shows_id_and_count() -> None:
    cell = runtime_cell_text(_state_with_active_runtime(runtime="claude", count=2))
    assert cell == "runtime: claude - 2 running"


def test_runtime_cell_text_active_without_resolved_runtime_shows_count() -> None:
    # One active wave but no ACTIVE session => honest count, no runtime id.
    cell = runtime_cell_text(_state_with_active_wave())
    assert cell == "runtime: 1 running"


# --------------------------------------------------------------------------
# render_header — brand prefix, click markup, runtime cell, UTC clock
# --------------------------------------------------------------------------


def test_render_header_none_state_has_brand_and_default_code() -> None:
    rendered = render_header(None)
    assert BRAND in rendered
    assert DEFAULT_PROJECT_CODE in rendered
    assert RUNTIME_IDLE in rendered


def test_render_header_populated_has_brand_left_of_breadcrumb() -> None:
    rendered = render_header(_load(_PHASE_ITER_WAVE))
    assert rendered.index(BRAND) < rendered.index(_CODE)


def test_render_header_carries_clickable_segments() -> None:
    rendered = render_header(_load(_PHASE_ITER_WAVE), "repo", "Home", mode_name="home")
    assert "[@click=app.switch_scope('repo')]repo[/]" in rendered
    assert f"[@click=app.open_phase_ref('{_PHASE}')]" in rendered


def test_render_header_shows_runtime_cell_with_count() -> None:
    rendered = render_header(_state_with_active_runtime(runtime="claude", count=2))
    assert "runtime: claude - 2 running" in rendered


def test_render_header_includes_utc_clock() -> None:
    rendered = render_header(_load(_EMPTY_REPO))
    assert "UTC" in rendered


# --------------------------------------------------------------------------
# Pilot paint — brand + breadcrumb render under the real palette
# --------------------------------------------------------------------------


def test_header_paints_brand_and_breadcrumb() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            app.query_one("#hdr", Header).state = _load(_PHASE_ITER_WAVE)
            await pilot.pause()
            rendered = app.export_screenshot()
            assert BRAND in rendered
            assert _CODE in rendered

    asyncio.run(body())


def test_header_repaints_on_state_revision() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            # Fresh frame falls back to the default code.
            assert DEFAULT_PROJECT_CODE in app.export_screenshot()
            header.state = _load(_EMPTY_REPO)
            await pilot.pause()
            assert _CODE in app.export_screenshot()

    asyncio.run(body())


def test_header_state_is_read_only_to_fixture(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(_EMPTY_REPO.read_bytes())
    before = target.read_bytes()
    # Building a breadcrumb never touches the file.
    build_breadcrumb(_load(target))
    assert target.read_bytes() == before


def test_header_render_returns_brand_after_assignment() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            header = app.query_one("#hdr", Header)
            header.state = _load(_EMPTY_REPO)
            await pilot.pause()
            assert ScopeKind.REPO.value in str(header.render())

    asyncio.run(body())


# --------------------------------------------------------------------------
# breadcrumb @click actions resolve against the host EaApp (the live half)
# --------------------------------------------------------------------------
#
# The build_breadcrumb tests above pin the *string* the markup emits; these
# pin that the App actually owns the action methods those strings name, so a
# click resolves + fires instead of silently no-opping. ``run_action`` is
# Textual's own dispatcher: it parses the ``app.`` namespace, checks the
# action exists, and fires it -- returning ``True`` only when handled.

_REF_FIXTURE = _PHASE_ITER_WAVE


def test_app_resolves_breadcrumb_switch_scope_action() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REF_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert await app.run_action("app.switch_scope('repo')") is True

    asyncio.run(body())


def test_app_resolves_breadcrumb_switch_mode_action() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REF_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # The code segment links here (return to Home); home is a real mode.
            assert await app.run_action("app.switch_mode('home')") is True

    asyncio.run(body())


def test_app_resolves_breadcrumb_phase_and_iter_ref_actions() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REF_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert await app.run_action(f"app.open_phase_ref('{_PHASE}')") is True
            await pilot.pause()
            assert await app.run_action(f"app.open_iter_ref('{_ITER}')") is True

    asyncio.run(body())


def test_app_bare_breadcrumb_action_against_header_does_not_resolve() -> None:
    # The bug being fixed: a *bare* action resolved against the Header (a
    # Static, which defines none of these) does NOT resolve -> the click was a
    # silent no-op. The app.-namespaced form (asserted above) is what fixes it.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REF_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            header = app.query(Header).first()
            handled = await app.run_action("switch_mode('home')", default_namespace=header)
            assert handled is False

    asyncio.run(body())


def test_action_switch_mode_guards_against_stale_mode_name() -> None:
    # A breadcrumb link can carry a stale mode name (e.g. after a rename);
    # action_switch_mode must drop it (no UnknownModeError) and leave the
    # current mode untouched, while a real mode still switches.
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REF_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            start_mode = app.current_mode
            await app.action_switch_mode("definitely_not_a_registered_mode")
            await pilot.pause()
            assert app.current_mode == start_mode

    asyncio.run(body())

    asyncio.run(body())
