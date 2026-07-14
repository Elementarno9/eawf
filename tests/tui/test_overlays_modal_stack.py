"""Tests for the C06 modal-stack depth cap (P26-W19) + singleton dedup (P29-W26).

The App enforces a single modal-stack gate (``MAX_MODAL_DEPTH == 6`` per
C06 §5.7 / failure mode F6, raised from 3 in P29-I09-W01 so a research
brief plus a chain of reference drills fits): every overlay-opening path
routes through :meth:`EaApp.push_modal`, which rejects the push past the
cap and toasts rather than mutating the stack. These tests drive the cap
directly and through the palette + detail-drill paths.

The W26 dedup adds a top-only singleton guard to the same gate: a modal
whose class sets ``dedupe_singleton = True`` (the palette / config / help /
inbox / init-wizard overlays) is rejected when the current top-of-stack
overlay is the same class, so a re-fired open key / palette verb cannot
stack a second identical overlay. Non-singleton drill-ins (DetailModal /
ConfirmModal) still stack freely, and a singleton over a *different*
singleton still stacks. The dedup tests below pin all three behaviours.

P29-I09-W02 consolidates the W25 per-path entity dedup into the same
chokepoint via a second ``dedupe_key`` mode: a DetailModal exposing a
non-``None`` ``dedupe_key`` (its originating ``entity_id``) is rejected
when the top-of-stack overlay carries the same key, so re-choosing the
entity already on top is a no-op on *every* push path (the row drill, the
``/find`` palette verb, a re-choose from inside the open modal). The mode
is top-only and entity-scoped: a same-entity modal over a *different* top
stacks, and a DetailModal built without an ``entity_id`` (``dedupe_key is
None``) stays stackable. The chokepoint tests below pin those edges.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.screen import ModalScreen
from textual.widgets._toast import Toast

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.palette.command_palette import CommandPalette
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.screens.overlays.config_modal import ConfigModal
from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal
from eawf.surfaces.tui.screens.overlays.detail import DetailCard, DetailModal

#: A wave / phase entity id present in the active-wave fixture, used to drill
#: the row-detail path. ``_WAVE_ID`` and ``_PHASE_ID`` are distinct entities.
_WAVE_ID = "P01-I01-W01"
_PHASE_ID = "P01"

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"

_CARD = DetailCard(title="t", rows=(("a", "b"),))


def test_max_modal_depth_is_six() -> None:
    assert EaApp.MAX_MODAL_DEPTH == 6


def test_push_modals_to_cap_succeeds() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            results = []
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                results.append(app.push_modal(DetailModal(_CARD)))
                await pilot.pause()
            assert results == [True] * EaApp.MAX_MODAL_DEPTH
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def test_push_past_cap_rejected() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(DetailModal(_CARD))
                await pilot.pause()
            accepted = app.push_modal(DetailModal(_CARD))
            await pilot.pause()
            assert accepted is False
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def test_cap_frees_after_pop() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            for _ in range(EaApp.MAX_MODAL_DEPTH):
                app.push_modal(DetailModal(_CARD))
                await pilot.pause()
            assert app.push_modal(DetailModal(_CARD)) is False
            await pilot.pause()
            # Pop one (Esc on the top DetailModal), then a push fits again.
            await pilot.press("escape")
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH - 1
            assert app.push_modal(ConfirmModal("ok?")) is True
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


def test_modal_depth_zero_on_scope_screen() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The base scope screen is a plain Screen, not a ModalScreen.
            assert app.modal_depth() == 0

    asyncio.run(body())


def test_palette_then_fill_then_cap() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # Open the palette via the keypress (one modal), then stack
            # drill-ins up to the cap and confirm the next push is rejected.
            await pilot.press("slash")
            await pilot.pause()
            assert app.modal_depth() == 1
            for _ in range(EaApp.MAX_MODAL_DEPTH - 1):
                assert app.push_modal(DetailModal(_CARD)) is True
                await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH
            assert app.push_modal(DetailModal(_CARD)) is False
            await pilot.pause()
            assert app.modal_depth() == EaApp.MAX_MODAL_DEPTH

    asyncio.run(body())


# --------------------------------------------------------------------------
# Singleton dedup (P29-W26) — a duplicate of the top overlay is rejected
# --------------------------------------------------------------------------


def test_duplicate_singleton_modal_is_deduped_to_one() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # A singleton overlay (ConfigModal) pushed twice keeps ONE on
            # the stack: the second push duplicates the top, so it no-ops.
            assert app.push_modal(ConfigModal()) is True
            await pilot.pause()
            assert app.modal_depth() == 1
            assert app.push_modal(ConfigModal()) is False
            await pilot.pause()
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_distinct_singleton_modals_still_stack() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # Two *different* singletons coexist: the dedup is top-only by
            # class, so config then palette stacks to depth 2.
            assert app.push_modal(ConfigModal()) is True
            await pilot.pause()
            assert app.push_modal(CommandPalette()) is True
            await pilot.pause()
            assert app.modal_depth() == 2
            # The palette is now the top, so a second palette is deduped.
            assert app.push_modal(CommandPalette()) is False
            await pilot.pause()
            assert app.modal_depth() == 2

    asyncio.run(body())


def test_non_singleton_modal_stacks_duplicates() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # DetailModal does not opt into dedup, so legitimate repeat
            # drills (the same card from different contexts) still stack.
            results = [app.push_modal(DetailModal(_CARD)) for _ in range(3)]
            await pilot.pause()
            assert results == [True, True, True]
            assert app.modal_depth() == 3

    asyncio.run(body())


def test_open_config_action_twice_keeps_one_modal() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # The end-to-end vector the W26 dedup targets: re-firing the
            # App-level config action (e.g. a double palette-verb dispatch)
            # must not stack a second identical ConfigModal.
            app.action_open_config()
            await pilot.pause()
            app.action_open_config()
            await pilot.pause()
            assert app.modal_depth() == 1
            modal_names = [
                type(screen).__name__
                for screen in app.screen_stack
                if isinstance(screen, ModalScreen)
            ]
            assert modal_names == ["ConfigModal"]

    asyncio.run(body())


# --------------------------------------------------------------------------
# Entity dedup (P29-I08-W25) — re-opening the entity already on top no-ops
# --------------------------------------------------------------------------


def _repo_scope_screen(app: EaApp) -> ScopeScreen:
    """Return the live repo scope screen (the row-drill host) for *app*."""
    screen = app.screen
    assert isinstance(screen, ScopeScreen)
    return screen


def test_reopen_top_entity_leaves_stack_identity_and_length_unchanged() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _repo_scope_screen(app)
            # Drill into the wave entity once.
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            assert top.entity_id == _WAVE_ID
            stack_before = list(app.screen_stack)

            # Re-opening the SAME entity (a double-Enter / re-selection) is a
            # no-op: the screen stack keeps its exact identity AND length, and
            # the top modal is the very same instance (not a re-pushed twin).
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            stack_after = list(app.screen_stack)
            assert [id(s) for s in stack_after] == [id(s) for s in stack_before]
            assert app._top_modal() is top

    asyncio.run(body())


def test_reopen_top_entity_mounts_no_toast() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _repo_scope_screen(app)
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            # The dedup-skip is a benign no-op: no Toast notification mounts
            # (it is logged, not surfaced -- contrast the depth-cap toast).
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            assert not list(app.query(Toast))

    asyncio.run(body())


def test_reopen_different_entity_still_stacks() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _repo_scope_screen(app)
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 1
            # A drill into a DIFFERENT entity is NOT a duplicate -- it stacks
            # a new card (+1), so the dedup is strictly entity-scoped.
            screen._open_detail(_PHASE_ID)
            await pilot.pause()
            assert app.modal_depth() == 2
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            assert top.entity_id == _PHASE_ID

    asyncio.run(body())


def test_reopen_same_entity_after_a_different_one_restacks() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _repo_scope_screen(app)
            # wave -> phase stacks two cards; re-drilling the wave is now a
            # DIFFERENT entity than the phase on top, so it stacks again (the
            # dedup is top-only, not a global "already open anywhere" guard).
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            screen._open_detail(_PHASE_ID)
            await pilot.pause()
            assert app.modal_depth() == 2
            screen._open_detail(_WAVE_ID)
            await pilot.pause()
            assert app.modal_depth() == 3
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            assert top.entity_id == _WAVE_ID

    asyncio.run(body())


# --------------------------------------------------------------------------
# Entity-key dedup at the push_modal chokepoint (P29-I09-W02) — the dedup
# now lives on the gate, so EVERY push path (not just the row drill) no-ops
# a re-choose of the entity already on top.
# --------------------------------------------------------------------------


def _detail(entity_id: str | None) -> DetailModal:
    """Build a DetailModal carrying *entity_id* (its ``dedupe_key``)."""
    return DetailModal(_CARD, entity_id=entity_id)


def test_detail_modal_exposes_entity_id_as_dedupe_key() -> None:
    # The chokepoint reads ``dedupe_key`` off the modal; for a DetailModal it
    # is the originating entity id, and ``None`` when the card opted out.
    assert _detail(_WAVE_ID).dedupe_key == _WAVE_ID
    assert _detail(None).dedupe_key is None


def test_push_modal_dedups_same_entity_on_top_to_one() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # A DetailModal carrying an entity id pushed twice keeps ONE: the
            # second push duplicates the top key, so the chokepoint no-ops it
            # and returns False without mutating the stack.
            assert app.push_modal(_detail(_WAVE_ID)) is True
            await pilot.pause()
            assert app.modal_depth() == 1
            assert app.push_modal(_detail(_WAVE_ID)) is False
            await pilot.pause()
            assert app.modal_depth() == 1

    asyncio.run(body())


def test_push_modal_dedup_skip_mounts_no_toast() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.push_modal(_detail(_WAVE_ID))
            await pilot.pause()
            # The entity-key dedup-skip is a benign no-op: it is logged, not
            # surfaced -- contrast the depth-cap toast.
            app.push_modal(_detail(_WAVE_ID))
            await pilot.pause()
            assert not list(app.query(Toast))

    asyncio.run(body())


def test_push_modal_different_entity_stacks() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.push_modal(_detail(_WAVE_ID)) is True
            await pilot.pause()
            # A DIFFERENT entity key is not a duplicate -- it stacks (+1).
            assert app.push_modal(_detail(_PHASE_ID)) is True
            await pilot.pause()
            assert app.modal_depth() == 2
            top = app._top_modal()
            assert isinstance(top, DetailModal)
            assert top.entity_id == _PHASE_ID

    asyncio.run(body())


def test_push_modal_none_dedupe_key_is_not_entity_deduped() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # A DetailModal built without an entity id carries ``dedupe_key is
            # None`` and so opts out of the entity dedup: repeat pushes stack.
            results = [app.push_modal(_detail(None)) for _ in range(3)]
            await pilot.pause()
            assert results == [True, True, True]
            assert app.modal_depth() == 3

    asyncio.run(body())


def test_push_modal_same_entity_over_different_top_restacks() -> None:
    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            # wave -> phase stacks two; re-pushing the wave is a DIFFERENT key
            # than the phase on top, so it stacks again (the dedup is top-only,
            # not a global "already open anywhere" guard).
            assert app.push_modal(_detail(_WAVE_ID)) is True
            await pilot.pause()
            assert app.push_modal(_detail(_PHASE_ID)) is True
            await pilot.pause()
            assert app.modal_depth() == 2
            assert app.push_modal(_detail(_WAVE_ID)) is True
            await pilot.pause()
            assert app.modal_depth() == 3

    asyncio.run(body())
