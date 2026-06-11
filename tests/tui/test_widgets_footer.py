"""Unit + Pilot tests for the C06 shared ``Footer`` + ``Heartbeat`` (P26-W18).

Covers the pure hint formatter (:func:`format_hints`), the Footer's
default + overridden hint strip, the weekly-burn line builder + its
empty-state fallback, the always-visible mode row
(:func:`build_mode_row` + the mounted Footer's active-mode highlight),
the embedded Heartbeat pulse glyph + degraded-colour class flip (D22),
and a Pilot-driven paint confirming the footer hints + heartbeat dot
render under the real palette.

The footer is **two rows** (the operator-chosen layout): row 1 merges
the key-hint strip (left) with the status cells (weekly-burn + needs_user
badge + heartbeat, right); row 2 is the always-visible mode row derived
from ``MODE_REGISTRY`` with the active mode highlighted. The footer stays
height 2.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from eawf.kernel.state.enums import ActualStatus
from eawf.kernel.state.models import ActualSummary, State
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.registry import MODE_REGISTRY, build_modes
from eawf.surfaces.tui.scopes import RepoScreen, UserScreen, WorkspaceScreen
from eawf.surfaces.tui.snapshot import settle_screen
from eawf.surfaces.tui.widgets import eu_bar
from eawf.surfaces.tui.widgets.footer import (
    CANONICAL_HINT_ACTIONS,
    CANONICAL_HINT_TOKENS,
    DEFAULT_HINTS,
    HEARTBEAT_GLYPH,
    HINT_KEY_PRIORITY,
    WEEKLY_BURN_EMPTY,
    Footer,
    Heartbeat,
    build_mode_row,
    build_weekly_burn_line,
    format_hints,
    order_hints,
    render_hint_label,
)

from ._palette_harness import PaletteHarnessApp

_THEME = Path(__file__).resolve().parents[2] / "src" / "eawf" / "surfaces" / "tui" / "theme.tcss"

#: Fixed clock anchor for the weekly-burn tests. The seeded actual's
#: ``updated_at`` sits at this instant, so injecting ``now=_T0`` keeps the
#: in-window actual inside the trailing-7-day window regardless of
#: wall-clock date (the W24 deterministic-window pattern).
_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _state(*, weekly_eu_target: float | None, actual_eu: float | None) -> State:
    """Build a minimal valid State with an optional target + one actual.

    Args:
        weekly_eu_target: The project's weekly EU budget, or ``None`` to
            leave it unset.
        actual_eu: When set, seeds a single in-window actual carrying this
            ``elapsed_eu``; ``None`` leaves ``actuals`` empty.

    Returns:
        A validated :class:`~eawf.kernel.state.models.State`.
    """
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
            "weekly_eu_target": weekly_eu_target,
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state = State.model_validate(payload)
    if actual_eu is not None:
        state.actuals = {
            "P01-I01-W01": ActualSummary(
                id="ACT-P01-I01-W01",
                scope_id="P01-I01-W01",
                status=ActualStatus.DONE,
                elapsed_eu=actual_eu,
                current_store_record_id="REC-P01-I01-W01",
                updated_at=_T0,
            )
        }
    return state


class _Harness(PaletteHarnessApp):
    """Production-style host loading the real palette CSS."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Footer(id="ftr")


class _HeartbeatHarness(PaletteHarnessApp):
    """Host mounting a standalone Heartbeat for the degraded-flip test."""

    CSS_PATH = str(_THEME)

    def compose(self) -> ComposeResult:
        yield Heartbeat(id="hb")


# --------------------------------------------------------------------------
# build_weekly_burn_line — populated + empty-state paths
# --------------------------------------------------------------------------


def test_build_weekly_burn_line_renders_figure_when_target_and_actuals_set() -> None:
    state = _state(weekly_eu_target=10.0, actual_eu=3.5)
    # Inject the fixture anchor so the trailing-7-day window includes the
    # in-window actual regardless of wall-clock date.
    line = build_weekly_burn_line(state, now=_T0)
    assert line == "weekly burn: 3.5 / 10 EU"


def test_build_weekly_burn_line_none_target_renders_empty_state() -> None:
    # Actuals present but no target set: never a 0/None bar, the placeholder.
    state = _state(weekly_eu_target=None, actual_eu=3.5)
    line = build_weekly_burn_line(state, now=_T0)
    assert line == f"weekly burn: {WEEKLY_BURN_EMPTY}"


def test_build_weekly_burn_line_empty_actuals_renders_empty_state() -> None:
    # Target set but no actuals rolled up yet: the empty-state placeholder.
    state = _state(weekly_eu_target=10.0, actual_eu=None)
    line = build_weekly_burn_line(state, now=_T0)
    assert line == f"weekly burn: {WEEKLY_BURN_EMPTY}"


def test_build_weekly_burn_line_none_state_renders_empty_state() -> None:
    # Boundary: no bound state at all (pre-load) renders the placeholder.
    assert build_weekly_burn_line(None) == f"weekly burn: {WEEKLY_BURN_EMPTY}"


# --------------------------------------------------------------------------
# format_hints — empty + single + many
# --------------------------------------------------------------------------


def test_format_hints_empty_is_blank() -> None:
    assert format_hints(()) == ""


def test_format_hints_single_has_no_separator() -> None:
    assert format_hints(("q quit",)) == "q quit"


def test_format_hints_many_joined_with_bullet() -> None:
    out = format_hints(("a", "b", "c"))
    assert out == "a  ·  b  ·  c"
    assert out.count("·") == 2


# --------------------------------------------------------------------------
# render_hint_label — canonical-vocabulary regression guard (W21)
# --------------------------------------------------------------------------


def test_render_hint_label_joins_canonical_token_and_action() -> None:
    # A canonical token + its canonical action renders ``<token> <action>``.
    assert render_hint_label("↑↓", "select") == "↑↓ select"
    assert render_hint_label("Enter", "open") == "Enter open"
    assert render_hint_label("w/r/u", "scope") == "w/r/u scope"
    assert render_hint_label("q", "quit") == "q quit"
    # A mode-specific token (absent from the action canon) still joins with
    # free action text -- per-mode verbs are not pinned.
    assert render_hint_label("d", "dispatch") == "d dispatch"


def test_render_hint_label_accepts_every_canonical_token() -> None:
    # Every frozen token renders without raising (the full vocabulary is
    # exercised so a removed member is caught). Shared tokens are passed their
    # canonical action; mode-specific tokens take free text.
    for token in CANONICAL_HINT_TOKENS:
        action = CANONICAL_HINT_ACTIONS.get(token, "act")
        assert render_hint_label(token, action) == f"{token} {action}"


def test_render_hint_label_rejects_unknown_token() -> None:
    # An off-vocabulary token raises ValueError naming the offending token —
    # the message substring is part of the regression-guard contract.
    with pytest.raises(ValueError, match="non-canonical hint token"):
        render_hint_label("xyzzy", "nope")


def test_render_hint_label_rejects_drifted_arrow_word() -> None:
    # The exact historical drift forms each raise: the spelled-out arrow word,
    with pytest.raises(ValueError, match="up/down"):
        render_hint_label("up/down", "scroll")


def test_render_hint_label_rejects_lowercase_enter() -> None:
    # the lowercase ``enter``,
    with pytest.raises(ValueError, match="enter"):
        render_hint_label("enter", "peek")


def test_render_hint_label_rejects_truncated_scope_token() -> None:
    # the ``w/u`` typo dropping the repo letter,
    with pytest.raises(ValueError, match="w/u"):
        render_hint_label("w/u", "scope")


def test_render_hint_label_rejects_stale_mode_digit_fragment() -> None:
    # and the stale mode-digit fragment the always-visible mode row supersedes.
    with pytest.raises(ValueError, match="1-6"):
        render_hint_label("1-6", "mode")
    with pytest.raises(ValueError, match="1-8"):
        render_hint_label("1-8", "mode")


def test_canonical_hint_tokens_excludes_digit_ranges() -> None:
    # The digit-range tokens are deliberately absent (the mode row owns them).
    assert "1-6" not in CANONICAL_HINT_TOKENS
    assert "1-8" not in CANONICAL_HINT_TOKENS
    # And the canonical arrow glyph / full key names / three-letter scope are in.
    assert {"↑↓", "←→", "Enter", "Esc", "F5", "w/r/u"} <= CANONICAL_HINT_TOKENS


# --------------------------------------------------------------------------
# render_hint_label — shared-token action canon (W03 regression guard)
# --------------------------------------------------------------------------


def test_canonical_hint_actions_pins_each_shared_token() -> None:
    # The action canon maps exactly the cross-surface shared tokens, each to
    # ONE canonical action word -- ``↑↓`` is the criterion-mandated ``select``.
    assert CANONICAL_HINT_ACTIONS == {
        "↑↓": "select",
        "←→": "collapse",
        "Enter": "open",
        "Esc": "back",
        "w/r/u": "scope",
        "F5": "refresh",
        "/": "palette",
        "?": "help",
        "q": "quit",
        "c": "config",
    }
    # The arrow hint specifically reads ``select`` (the criterion).
    assert CANONICAL_HINT_ACTIONS["↑↓"] == "select"
    # Every key of the action canon is itself a canonical token.
    assert set(CANONICAL_HINT_ACTIONS) <= CANONICAL_HINT_TOKENS
    # Mode-specific tokens are NOT pinned (they keep free per-mode action text).
    for mode_token in ("a", "d", "H", "k", "K", "p", "s", "S", "space"):
        assert mode_token not in CANONICAL_HINT_ACTIONS


def test_render_hint_label_accepts_canonical_shared_action() -> None:
    # Each shared token paired with its canonical action renders cleanly.
    for token, action in CANONICAL_HINT_ACTIONS.items():
        assert render_hint_label(token, action) == f"{token} {action}"


def test_render_hint_label_rejects_noncanonical_shared_action() -> None:
    # The historical per-surface drift forms each raise now that the action
    # half is pinned: ``↑↓ move`` / ``↑↓ scroll`` / ``↑↓ row`` / ``↑↓ tree``
    # (every variant of the arrow that was NOT ``select``), and ``Enter zoom``
    # / ``Enter peek``. The message names the offending token + the canon.
    for bad in ("move", "scroll", "row", "tree"):
        with pytest.raises(ValueError, match=r"non-canonical action for '↑↓'"):
            render_hint_label("↑↓", bad)
    with pytest.raises(ValueError, match="canonical: 'select'"):
        render_hint_label("↑↓", "move")
    for bad in ("zoom", "peek"):
        with pytest.raises(ValueError, match=r"non-canonical action for 'Enter'"):
            render_hint_label("Enter", bad)


def test_render_hint_label_keeps_mode_specific_action_free() -> None:
    # A mode-specific token (absent from the action canon) accepts arbitrary
    # free action text -- the per-mode verb is not governed by the canon.
    assert render_hint_label("d", "dispatch") == "d dispatch"
    assert render_hint_label("d", "brief") == "d brief"
    assert render_hint_label("H", "halt") == "H halt"
    assert render_hint_label("r", "follow-up") == "r follow-up"
    assert render_hint_label("s", "snapshot") == "s snapshot"


# --------------------------------------------------------------------------
# Every mode + scope footer-hint tuple parses to canon (W22 enumeration)
# --------------------------------------------------------------------------

_REPO_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)

#: Scope-screen name (what the Home mode factory returns) -> the screen class
#: that owns the matching ``FOOTER_HINTS``.
_SCOPE_SCREEN_CLASSES = {
    "repo": RepoScreen,
    "workspace": WorkspaceScreen,
    "user": UserScreen,
}


def _hint_token(label: str) -> str:
    """Return the leading key token of a hint label (text before the first space)."""
    return label.split(" ", 1)[0]


def _all_surface_hint_tuples() -> dict[str, tuple[str, ...]]:
    """Collect the FOOTER_HINTS of every registered mode + the 3 scope screens.

    Resolves each :data:`MODE_REGISTRY` factory against a live app (the Home
    factory returns a scope-screen *name*, which maps to its screen class), and
    adds the three scope screens directly, so the assertion spans every footer
    hint tuple the operator can see -- exactly the W22 surface set.
    """
    surfaces: dict[str, tuple[str, ...]] = {}

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_REPO_FIXTURE)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle_screen(pilot)
            modes = build_modes(app)
            for spec in MODE_REGISTRY:
                built = modes[spec.name]()
                if isinstance(built, str):
                    surfaces[f"mode:{spec.name}"] = _SCOPE_SCREEN_CLASSES[built].FOOTER_HINTS
                else:
                    surfaces[f"mode:{spec.name}"] = type(built).FOOTER_HINTS

    asyncio.run(body())
    for scope, cls in _SCOPE_SCREEN_CLASSES.items():
        surfaces[f"scope:{scope}"] = cls.FOOTER_HINTS
    return surfaces


def test_every_mode_and_scope_hint_parses_to_canon() -> None:
    # The load-bearing W22 gate: every hint label on every mode + scope screen
    # has a leading key token drawn from the frozen canonical vocabulary, so a
    # newly-authored per-mode tuple cannot reintroduce a drifted token.
    surfaces = _all_surface_hint_tuples()
    # The surface set actually spans every registered mode + all three scopes
    # (a regression that drops a mode/scope from the sweep is caught here).
    assert len(surfaces) == len(MODE_REGISTRY) + len(_SCOPE_SCREEN_CLASSES)
    for surface, hints in surfaces.items():
        for label in hints:
            token = _hint_token(label)
            assert token in CANONICAL_HINT_TOKENS, (
                f"{surface} hint {label!r} has non-canonical token {token!r}"
            )


def test_no_surface_hint_carries_a_drifted_fragment() -> None:
    # The exact historical drift forms are absent from EVERY surface: the
    # spelled-out arrow word, the lowercase ``enter``, the truncated ``w/u``
    # scope token, and the stale ``1-6``/``1-8 mode`` digit fragment.
    surfaces = _all_surface_hint_tuples()
    forbidden_tokens = {"up/down", "enter", "w/u", "1-6", "1-8"}
    for surface, hints in surfaces.items():
        tokens = {_hint_token(label) for label in hints}
        leaked = tokens & forbidden_tokens
        assert not leaked, f"{surface} carries drifted token(s) {leaked}"
        # The digit-range fragment never appears as a whole label either
        # (defends against a ``1-6 mode`` authored with a leading space etc.).
        joined = " ".join(hints)
        assert "1-6 mode" not in joined
        assert "1-8 mode" not in joined


def test_every_surface_arrow_hint_reads_select() -> None:
    # The W03 criterion: the up/down arrow hint reads ``select`` on EVERY
    # footer surface that advertises an arrow hint -- no surface keeps the old
    # ``move`` / ``row`` / ``scroll`` / ``tree`` wording. (Surfaces without an
    # arrow hint -- e.g. doctor / placeholder -- are simply skipped.)
    surfaces = _all_surface_hint_tuples()
    saw_arrow = False
    for surface, hints in surfaces.items():
        arrow_labels = [label for label in hints if _hint_token(label) == "↑↓"]
        for label in arrow_labels:
            saw_arrow = True
            assert label == "↑↓ select", f"{surface} arrow hint reads {label!r}, not '↑↓ select'"
    # The sweep actually exercised at least one arrow-advertising surface (a
    # regression that drops every arrow hint would otherwise pass vacuously).
    assert saw_arrow


def test_every_shared_token_carries_its_canonical_action_across_surfaces() -> None:
    # Each shared token that appears on any surface carries the ONE canonical
    # action from CANONICAL_HINT_ACTIONS -- the full cross-surface invariant
    # the criterion states (every shared token, all ten surfaces). Mode-specific
    # tokens are not asserted (their action text is free).
    surfaces = _all_surface_hint_tuples()
    for surface, hints in surfaces.items():
        for label in hints:
            token = _hint_token(label)
            if token in CANONICAL_HINT_ACTIONS:
                expected = f"{token} {CANONICAL_HINT_ACTIONS[token]}"
                assert label == expected, (
                    f"{surface} shared-token hint {label!r} drifts from {expected!r}"
                )


def test_research_board_scope_hint_uses_all_three_letters() -> None:
    # The specific research_board.py typo fix: ``w/u scope`` -> ``w/r/u scope``
    # (all three of workspace / repo / user).
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen

    hints = ResearchBoardModeScreen.FOOTER_HINTS
    assert "w/r/u scope" in hints
    assert "w/u scope" not in hints


# --------------------------------------------------------------------------
# HINT_KEY_PRIORITY + order_hints — central footer ordering canon (W04)
# --------------------------------------------------------------------------

#: The canonical fragments ``order_hints`` guarantees on every footer surface.
_C_HINT = render_hint_label("c", "config")
_F5_HINT = render_hint_label("F5", "refresh")


#: Every footer surface class that carries a ``FOOTER_HINTS`` tuple: the three
#: ``ScopeScreen`` scope subclasses plus the nine mode subclasses. Imported
#: here so the enumeration test pins the canon across the WHOLE surface set --
#: a new surface that forgets the chokepoint is caught the moment it lands.
def _all_surface_classes() -> dict[str, type]:
    """Import + collect every footer surface class (3 scopes + 9 modes)."""
    from eawf.surfaces.tui.modes.agent_watch import AgentWatchModeScreen
    from eawf.surfaces.tui.modes.autopilot import AutopilotModeScreen
    from eawf.surfaces.tui.modes.doctor import DoctorModeScreen
    from eawf.surfaces.tui.modes.evidence import EvidenceModeScreen
    from eawf.surfaces.tui.modes.feed import FeedModeScreen
    from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen
    from eawf.surfaces.tui.modes.sandbox_events import SandboxEventsModeScreen
    from eawf.surfaces.tui.modes.trust import TrustModeScreen

    return {
        "scope:repo": RepoScreen,
        "scope:workspace": WorkspaceScreen,
        "scope:user": UserScreen,
        "mode:autopilot": AutopilotModeScreen,
        "mode:research_board": ResearchBoardModeScreen,
        "mode:trust": TrustModeScreen,
        "mode:doctor": DoctorModeScreen,
        "mode:evidence": EvidenceModeScreen,
        "mode:feed": FeedModeScreen,
        "mode:agent_watch": AgentWatchModeScreen,
        "mode:sandbox_events": SandboxEventsModeScreen,
    }


def _token(label: str) -> str:
    """Return the leading key token of a hint label (text before first space)."""
    return label.split(" ", 1)[0]


def _position(ordered: tuple[str, ...], token: str) -> int:
    """Return the index of the fragment whose leading token is *token* (-1 if absent)."""
    for index, label in enumerate(ordered):
        if _token(label) == token:
            return index
    return -1


def test_hint_key_priority_is_the_frozen_canon_order() -> None:
    # Pin the exact frozen left-to-right canon: primary navigation, then the
    # per-mode action keys, then the global affordances in their fixed tail
    # order. A reorder of the strip canon must update this assertion (and the
    # goldens), so a silent drift cannot slip through.
    assert HINT_KEY_PRIORITY == (
        "↑↓",
        "←→",
        "Enter",
        "Esc",
        "a",
        "d",
        "H",
        "k",
        "K",
        "n",
        "o",
        "p",
        "r",
        "s",
        "S",
        "t",
        "v",
        "x",
        "space",
        "w/r/u",
        "c",
        "F5",
        "/",
        "?",
        "q",
    )
    # Every priority token is itself a canonical key token (no priority entry
    # names a key the vocabulary guard would reject).
    assert set(HINT_KEY_PRIORITY) <= CANONICAL_HINT_TOKENS


def test_order_hints_sorts_scrambled_input_into_priority_order() -> None:
    # A scrambled strip is reordered to the canon: ``↑↓`` leads, ``q`` trails,
    # the globals land in their fixed tail order.
    scrambled = (
        render_hint_label("q", "quit"),
        render_hint_label("?", "help"),
        render_hint_label("/", "palette"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("Enter", "open"),
        render_hint_label("↑↓", "select"),
    )
    ordered = order_hints(scrambled)
    tokens = [_token(label) for label in ordered]
    # Navigation leads, the global tail order holds, c/F5 are injected between
    # the scope switch and the palette/help/quit glyphs.
    assert tokens == ["↑↓", "Enter", "w/r/u", "c", "F5", "/", "?", "q"]


def test_order_hints_injects_c_and_f5_when_absent() -> None:
    # A surface whose authored tuple omits BOTH globals gets them injected as
    # the canonical fragments (the doctor/trust-shaped case: no c, no F5).
    sparse = (
        render_hint_label("↑↓", "select"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("q", "quit"),
    )
    ordered = order_hints(sparse)
    assert _C_HINT in ordered
    assert _F5_HINT in ordered
    # They land in the canonical tail band, between scope and quit.
    assert _position(ordered, "w/r/u") < _position(ordered, "c")
    assert _position(ordered, "c") < _position(ordered, "F5")
    assert _position(ordered, "F5") < _position(ordered, "q")


def test_order_hints_does_not_duplicate_present_globals() -> None:
    # A surface that already advertises c + F5 (the repo/workspace/user-shaped
    # case) is not double-injected -- each appears exactly once.
    present = (
        render_hint_label("↑↓", "select"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("c", "config"),
        render_hint_label("F5", "refresh"),
        render_hint_label("q", "quit"),
    )
    ordered = order_hints(present)
    assert ordered.count(_C_HINT) == 1
    assert ordered.count(_F5_HINT) == 1


def test_order_hints_is_idempotent() -> None:
    # The load-bearing chokepoint invariant: re-canonicalising an already-canon
    # strip is a no-op (no second c/F5 inject, no reshuffle). Holds for the
    # sparse case, the dense case, and the raw default.
    from eawf.surfaces.tui.modes.autopilot import _AUTOPILOT_HINTS

    for sample in (
        DEFAULT_HINTS,
        _AUTOPILOT_HINTS,
        (render_hint_label("q", "quit"),),
        (),
    ):
        once = order_hints(sample)
        assert order_hints(once) == once


def test_order_hints_keeps_unknown_tokens_at_tail_in_stable_order() -> None:
    # A token absent from HINT_KEY_PRIORITY (a future per-mode key) is never
    # dropped: it sorts AFTER every known token, preserving the original
    # relative order of the unknowns.
    mixed = (
        render_hint_label("q", "quit"),
        "zzz first-unknown",
        render_hint_label("↑↓", "select"),
        "yyy second-unknown",
    )
    ordered = order_hints(mixed)
    # The known tokens precede both unknowns...
    assert _position(ordered, "↑↓") < _position(ordered, "q")
    assert _position(ordered, "q") < ordered.index("zzz first-unknown")
    # ...and the two unknowns keep their original relative order (stable sort).
    assert ordered.index("zzz first-unknown") < ordered.index("yyy second-unknown")


def test_default_hints_reactive_seeds_canonical_strip() -> None:
    # A Footer that is never overridden still exposes the canonical strip: the
    # reactive default is order_hints(DEFAULT_HINTS), so c/F5 are present and
    # the order is canon even before any set_hints call. Read it off a fresh
    # (unmounted) Footer -- the reactive returns its default before assignment.
    default = Footer().hints
    assert default == order_hints(DEFAULT_HINTS)
    assert _C_HINT in default
    assert _F5_HINT in default


def test_set_hints_canonicalises_through_chokepoint() -> None:
    # set_hints routes through order_hints: a scrambled, c/F5-less override is
    # stored canonicalised on the reactive (the chokepoint the surfaces use).
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(
                (
                    render_hint_label("q", "quit"),
                    render_hint_label("↑↓", "select"),
                    render_hint_label("w/r/u", "scope"),
                )
            )
            await pilot.pause()
            # Stored canonicalised: c/F5 injected, ordered ↑↓ ... w/r/u c F5 q.
            assert footer.hints == order_hints(
                (
                    render_hint_label("q", "quit"),
                    render_hint_label("↑↓", "select"),
                    render_hint_label("w/r/u", "scope"),
                )
            )
            assert _C_HINT in footer.hints
            assert _F5_HINT in footer.hints

    asyncio.run(body())


def test_every_surface_orders_identically_with_c_and_f5_present() -> None:
    # The criterion-mandated enumeration gate: every footer surface (3 scope
    # screens + 8 mode screens) run through order_hints (a) advertises BOTH
    # ``c config`` and ``F5 refresh``, and (b) lays the shared keys out in the
    # ONE canon order -- ``↑↓`` leads and ``w/r/u`` < ``c`` < ``F5`` < ``/`` <
    # ``?`` < ``q``. This pins canon order + shared-key positions across the
    # whole surface set, so no surface can drift its footer ordering.
    surfaces = _all_surface_classes()
    # The sweep spans every distinct footer surface: the three scope screens
    # plus one screen class per non-Home mode (Home reuses the resolved scope
    # screen rather than owning its own subclass, so it carries no separate
    # FOOTER_HINTS). That is 3 scopes + (len(MODE_REGISTRY) - 1) mode classes.
    assert len(surfaces) == 3 + (len(MODE_REGISTRY) - 1)
    for name, cls in surfaces.items():
        ordered = order_hints(cls.FOOTER_HINTS)  # type: ignore[attr-defined]
        # (a) both globals present on every surface.
        assert _C_HINT in ordered, f"{name} missing {_C_HINT!r}"
        assert _F5_HINT in ordered, f"{name} missing {_F5_HINT!r}"
        # (b) the shared-key relative positions match the canon. Every surface
        # advertises the full global tail, so all six are present + ordered.
        for earlier, later in (
            ("w/r/u", "c"),
            ("c", "F5"),
            ("F5", "/"),
            ("/", "?"),
            ("?", "q"),
        ):
            assert _position(ordered, earlier) < _position(ordered, later), (
                f"{name} shared keys out of canon order: {earlier!r} !< {later!r} in {ordered}"
            )
        # The fragments are sorted by HINT_KEY_PRIORITY: the priority index of
        # each known token is non-decreasing left to right (the unknown-token
        # tail, if any, sorts last and is skipped here).
        priorities = [
            HINT_KEY_PRIORITY.index(_token(label))
            for label in ordered
            if _token(label) in HINT_KEY_PRIORITY
        ]
        assert priorities == sorted(priorities), f"{name} not in priority order: {ordered}"


def test_every_surface_with_arrow_leads_with_it() -> None:
    # The ``↑↓`` navigation key, when a surface advertises it, leads the strip
    # (the operator's most-used key sits first). Surfaces without an arrow hint
    # (e.g. doctor) are skipped -- their first fragment is the next-priority key.
    surfaces = _all_surface_classes()
    saw_arrow = False
    for name, cls in surfaces.items():
        ordered = order_hints(cls.FOOTER_HINTS)  # type: ignore[attr-defined]
        if any(_token(label) == "↑↓" for label in ordered):
            saw_arrow = True
            assert _token(ordered[0]) == "↑↓", f"{name} does not lead with the arrow: {ordered}"
    # At least one arrow-advertising surface was exercised (a regression that
    # drops every arrow hint would otherwise pass vacuously).
    assert saw_arrow


# --------------------------------------------------------------------------
# Footer hints — default + override via set_hints
# --------------------------------------------------------------------------


def test_footer_paints_default_hints() -> None:
    async def body() -> None:
        app = _Harness()
        # Wide canvas: the default hint strip now carries the global
        # w/r/u scope-switch + c config + F5 refresh affordances (the W04
        # chokepoint injects c config + F5 refresh into the default), so the
        # strip overflows 80 cols; the real scope screens render at 120.
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            rendered = app.export_screenshot()
            # W13: fit_hints fits the strip to the allocated width. At 120 with
            # the burn cell the default strip overflows, so the lowest-priority
            # global (w/r/u scope) is shed with an ellipsis while the rest of the
            # global tail stays pinned -- and q quit now survives to the last
            # cell rather than clipping off the right edge.
            assert "config" in rendered
            assert "refresh" in rendered
            assert "palette" in rendered
            # ``quit`` (single word -- the SVG screenshot splits "q quit" on the
            # non-breaking space) now survives at the visible tail rather than
            # clipping off the right edge.
            assert "quit" in rendered
            from textual.widgets import Static

            footer = app.query_one("#ftr", Footer)
            assert "q quit" in str(footer.query_one(".footer-hints", Static).render())

    asyncio.run(body())


def test_footer_set_hints_repaints_strip() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(("xyzzy custom",))
            await pilot.pause()
            # set_hints canonicalises through order_hints: the unknown override
            # token survives (sorted to the tail) and c/F5 are injected, so the
            # stored value is the canonicalised form -- still carrying ``xyzzy``.
            assert footer.hints == order_hints(("xyzzy custom",))
            assert "xyzzy custom" in footer.hints
            assert "xyzzy" in app.export_screenshot()

    asyncio.run(body())


def test_footer_default_hints_use_full_key_names() -> None:
    # Operator convention: full key names (no "PgUp" abbreviations).
    joined = format_hints(DEFAULT_HINTS)
    assert "PgUp" not in joined
    assert "PgDn" not in joined


# --------------------------------------------------------------------------
# Footer owns a Heartbeat — D3 shared-chassis bundling
# --------------------------------------------------------------------------


def test_footer_owns_heartbeat() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(80, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            assert footer.query(Heartbeat)
            rendered = app.export_screenshot()
            assert HEARTBEAT_GLYPH in rendered

    asyncio.run(body())


# --------------------------------------------------------------------------
# Footer weekly-burn cell — paints the figure / empty-state from state
# --------------------------------------------------------------------------


def _burn_text(app: App[None]) -> str:
    """Read the footer burn cell's rendered text.

    Goes through the widget's own ``Static`` content rather than the SVG
    screenshot so the assertion is independent of how ``export_screenshot``
    encodes inter-word spacing.
    """
    burn = app.query_one(".footer-burn", Static)
    return str(burn.render())


def test_footer_paints_burn_empty_state_without_state() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            # The bare harness exposes no ``state`` attribute, so the burn
            # cell falls back to the empty-state placeholder.
            assert WEEKLY_BURN_EMPTY in _burn_text(app)

    asyncio.run(body())


def test_footer_paints_burn_figure_when_state_populated() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            # The widget anchors the rollup on wall-clock (no ``now``
            # override), so assert the figure *form* — the target + ``EU``
            # unit — not the window-dependent consumed value (covered by
            # the pure ``build_weekly_burn_line`` unit tests).
            text = _burn_text(app)
            assert text.startswith("weekly burn:")
            assert "/ 10 EU" in text
            assert WEEKLY_BURN_EMPTY not in text

    asyncio.run(body())


def test_footer_repaints_burn_to_empty_state_on_state_change() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            assert "/ 10 EU" in _burn_text(app)
            # A revision dropping the target flips the cell back to the
            # placeholder — the watcher repaints in place.
            footer.state = _state(weekly_eu_target=None, actual_eu=None)
            await pilot.pause()
            text = _burn_text(app)
            assert "EU" not in text
            assert WEEKLY_BURN_EMPTY in text

    asyncio.run(body())


# --------------------------------------------------------------------------
# Heartbeat — pulse glyph + degraded class + ack
# --------------------------------------------------------------------------


def test_heartbeat_paints_glyph_when_lit() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())


def test_heartbeat_degraded_flag_sets_class() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            assert not hb.has_class("-degraded")
            hb.degraded = True
            await pilot.pause()
            assert hb.has_class("-degraded")

    asyncio.run(body())


def test_heartbeat_ack_forces_lit_frame() -> None:
    async def body() -> None:
        app = _HeartbeatHarness()
        async with app.run_test(size=(20, 3)) as pilot:
            await pilot.pause()
            hb = app.query_one("#hb", Heartbeat)
            hb._lit = False
            await pilot.pause()
            hb.ack()
            await pilot.pause()
            assert hb._lit is True
            assert HEARTBEAT_GLYPH in app.export_screenshot()

    asyncio.run(body())


# --------------------------------------------------------------------------
# WEEKLY_BURN_EMPTY DRY — sourced from the canonical eu_bar sentinel
# --------------------------------------------------------------------------


def test_weekly_burn_empty_is_canonical_eu_bar_sentinel() -> None:
    # The footer's empty marker must be the SAME object as the canonical
    # eu_bar sentinel (DRY): both "no data" surfaces stay in lockstep.
    assert WEEKLY_BURN_EMPTY is eu_bar.EMPTY_STATE


# --------------------------------------------------------------------------
# Two-row footer — hints + status share row 1, mode row is row 2, height 2
# --------------------------------------------------------------------------


def test_footer_is_two_rows_tall() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            # The operator-chosen layout: the footer occupies two terminal
            # rows -- the merged hints+status row and the always-visible mode
            # row -- and stays height 2 (it does NOT grow to 3 rows).
            assert footer.size.height == 2

    asyncio.run(body())


def test_footer_hints_carry_repo_set_at_120() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.scopes.repo import _REPO_HINTS

        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(_REPO_HINTS)
            await pilot.pause()
            # The hint lane shares row 1 with the auto-width status cells, so
            # the whole repo hint set (incl. ``q quit``) lives in the hint
            # Static's content (Textual clips at paint time, not in the
            # renderable, so the tail is never lost from the source string).
            assert "q quit" in str(footer.query_one(".footer-hints", Static).render())

    asyncio.run(body())


def test_footer_hints_carry_workspace_set_at_120() -> None:
    async def body() -> None:
        from eawf.surfaces.tui.scopes.workspace import _WORKSPACE_HINTS

        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.set_hints(_WORKSPACE_HINTS)
            await pilot.pause()
            assert "q quit" in str(footer.query_one(".footer-hints", Static).render())

    asyncio.run(body())


def test_footer_hints_and_status_share_first_row() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            footer.state = _state(weekly_eu_target=10.0, actual_eu=3.5)
            await pilot.pause()
            # The operator merged the status cell onto row 1: the hints, the
            # burn cell, and the heartbeat all sit on the same row, with the
            # mode row alone on row 2 below them.
            hints = app.query_one(".footer-hints", Static)
            burn = app.query_one(".footer-burn", Static)
            modes = app.query_one(".footer-modes", Static)
            assert burn.region.y == hints.region.y
            assert modes.region.y > hints.region.y
            assert "/ 10 EU" in str(burn.render())
            assert footer.query(Heartbeat)

    asyncio.run(body())


# --------------------------------------------------------------------------
# build_mode_row — every mode, digit + lowercased title, active highlighted
# --------------------------------------------------------------------------


def _mode_row_plain(markup: str) -> str:
    """Strip content-markup tags from a mode-row string for token assertions."""
    import re

    return re.sub(r"\[[^\]]*\]", "", markup).replace("\\[", "[")


def test_build_mode_row_lists_all_modes_in_registry_order() -> None:
    # Every registered mode renders as ``<digit> <title-lowercased>`` in
    # registry (digit) order, joined by the bullet separator.
    plain = _mode_row_plain(build_mode_row(None))
    tokens = [tok.strip() for tok in plain.split("·")]
    expected = [f"{spec.digit} {spec.title.lower()}" for spec in MODE_REGISTRY]
    assert tokens == expected
    # The operator example lead/tail tokens are present + lowercased.
    assert tokens[0] == "1 home"
    assert "2 autopilot" in tokens


def test_build_mode_row_highlights_active_mode_only() -> None:
    # The active mode's token carries the bold accent span; the others are
    # muted. Pick a non-first mode so the assertion is not order-trivial.
    active = MODE_REGISTRY[1]  # autopilot
    markup = build_mode_row(active.name)
    active_token = f"{active.digit} {active.title.lower()}"
    assert f"[$accent][b]{active_token}[/b][/]" in markup
    # A different, non-active mode renders muted (no accent/bold span).
    other = MODE_REGISTRY[0]
    other_token = f"{other.digit} {other.title.lower()}"
    assert f"[$muted]{other_token}[/]" in markup
    assert f"[$accent][b]{other_token}[/b][/]" not in markup


def test_build_mode_row_none_highlights_nothing() -> None:
    # No active mode (or a name matching no mode) leaves every token muted.
    markup = build_mode_row(None)
    assert "[$accent][b]" not in markup
    # Every registered mode still appears, muted.
    for spec in MODE_REGISTRY:
        assert f"[$muted]{spec.digit} {spec.title.lower()}[/]" in markup


def test_build_mode_row_unknown_active_highlights_nothing() -> None:
    # A current_mode that names no registered mode (e.g. Textual's bare
    # "_default") highlights nothing rather than raising.
    markup = build_mode_row("_default")
    assert "[$accent][b]" not in markup


# --------------------------------------------------------------------------
# Mounted Footer mode row — row 2, active highlight seeds + updates
# --------------------------------------------------------------------------


def test_footer_mounts_mode_row_on_second_row() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            hints = app.query_one(".footer-hints", Static)
            modes = app.query_one(".footer-modes", Static)
            # The mode row sits on row 2 (below the merged hints+status row)
            # and lists every mode token; the footer stays height 2.
            assert modes.region.y > hints.region.y
            assert footer.size.height == 2
            rendered = _mode_row_plain(str(modes.render()))
            for spec in MODE_REGISTRY:
                assert f"{spec.digit} {spec.title.lower()}" in rendered

    asyncio.run(body())


def _token_styles(modes: Static, token: str) -> set[str]:
    """Collect the content-markup styles applied to *token*'s text range.

    The mounted Static renders a Textual ``Content`` whose ``str()`` strips
    the markup, so a markup-substring assertion does not work; instead this
    locates *token* in the rendered plain text and returns the set of span
    styles (e.g. ``{"$accent", "b"}`` for the highlighted active token,
    ``{"$muted"}`` for a muted one) that cover it.
    """
    content = modes.render()
    plain = content.plain  # type: ignore[attr-defined]
    start = plain.index(token)
    end = start + len(token)
    return {
        span.style  # type: ignore[attr-defined]
        for span in content.spans  # type: ignore[attr-defined]
        if span.start <= start and span.end >= end
    }


def test_footer_mode_row_highlight_updates_on_active_mode_change() -> None:
    async def body() -> None:
        app = _Harness()
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            # Drive the active mode directly (the standalone-test seam), the
            # same way the other footer tests drive ``state`` /
            # ``pending_pauses``.
            first = MODE_REGISTRY[1]  # autopilot
            footer.active_mode = first.name
            await pilot.pause()
            modes = app.query_one(".footer-modes", Static)
            first_token = f"{first.digit} {first.title.lower()}"
            # The active token carries the accent + bold spans; a different
            # mode stays muted.
            assert {"$accent", "b"} <= _token_styles(modes, first_token)
            other_token = f"{MODE_REGISTRY[3].digit} {MODE_REGISTRY[3].title.lower()}"
            assert "$accent" not in _token_styles(modes, other_token)
            # A change repaints the highlight onto the new active mode.
            second = MODE_REGISTRY[3]  # trust
            footer.active_mode = second.name
            await pilot.pause()
            modes = app.query_one(".footer-modes", Static)
            second_token = f"{second.digit} {second.title.lower()}"
            assert {"$accent", "b"} <= _token_styles(modes, second_token)
            # The previously-active mode is no longer highlighted.
            assert "$accent" not in _token_styles(modes, first_token)

    asyncio.run(body())


def test_footer_mode_row_seeds_highlight_from_app_current_mode() -> None:
    """A host exposing ``current_mode`` seeds the mode-row highlight on mount.

    Mirrors the live path: each mode owns its own scope screen, so the footer
    mounts fresh on a mode switch and reads ``app.current_mode``. A bare
    harness whose host exposes a registry mode name highlights that mode
    without a manual ``active_mode`` assignment.
    """

    class _ModeHarness(PaletteHarnessApp):
        CSS_PATH = str(_THEME)

        def __init__(self, current_mode: str) -> None:
            super().__init__()
            self._seed_mode = current_mode

        @property
        def current_mode(self) -> str:  # type: ignore[override]
            return self._seed_mode

        def compose(self) -> ComposeResult:
            yield Footer(id="ftr")

    async def body() -> None:
        target = MODE_REGISTRY[3]  # trust
        app = _ModeHarness(target.name)
        async with app.run_test(size=(120, 6)) as pilot:
            await pilot.pause()
            footer = app.query_one("#ftr", Footer)
            assert footer.active_mode == target.name
            modes = app.query_one(".footer-modes", Static)
            token = f"{target.digit} {target.title.lower()}"
            assert {"$accent", "b"} <= _token_styles(modes, token)

    asyncio.run(body())
