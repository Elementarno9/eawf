"""Gate-fire proof: no live journey frame carries a fixture id (CR-02).

The prototype's literals are meant to be reachable only through the golden
harness (``prototype.py``'s own docstring: "the branch a console takes when
it holds the prototype registers and no read model"). This suite proves that
holds live: it captures the same five journeys ``test_console_live_smoke.py``
walks against the real canary daemon, and asserts none of them carry a
fixture-only literal. It then proves the check itself is not vacuous by
injecting the prototype registers into the console (the same construction
the golden harness uses) and showing the check reds on that seeded defect.

CR-02 names ``MLS-0001`` explicitly as a fixture id to check for, and the
canary walk (W37) happens to key its own Milestone the same way
(``_canary_acceptance_walk.py``'s ``_plan()`` calls ``walker.create(
"milestone", "MLS-0001", ...)``). Before W42 (``Eawf-Wave: P34-I01-W42``)
that was a safe collision -- the walked Milestone reached a terminal status
and was compacted out of the live document before ``scope.home`` could read
it at all, so a bare ``"MLS-0001"`` check never risked a false positive. W42
taught ``scope.home`` to merge the Milestone ledger's terminal rows back in,
so the canary's own MLS-0001 legitimately renders now -- checking for it
here would fail on the console's own honest, correct behaviour. This suite
therefore keys the check on tokens that are fixture-only regardless: the
prototype's Run ids, its hardcoded revision (``41,208`` / ``41208``), its
scope name (``eawf-core``) and its own ``MLS-0004`` (a different id, opened
when a Milestone frame names no subject, that the canary walk never
produces). ``test_console_live_smoke.py`` carries the positive proof that
the real MLS-0001 row -- its ``urn``, its ``COMPLETED`` status -- is what
actually renders, not this suite.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    JOURNEYS,
    live_console,
    render_setup,
    walk_canary_isolated,
)

#: The tracked golden fixture: the same prototype registers a fixture-drawn
#: console (and the golden contract) replays.
FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"

#: Any Run id in the prototype's shape; scanned from the module's own source so
#: a literal added there is picked up without hand-copying it here too.
_RUN_ID_SHAPE = re.compile(r"\bRUN-[0-9a-f]{8}\b")


def _prototype_run_ids() -> frozenset[str]:
    """Return every Run id the prototype module's source spells out."""
    source = Path(pt.__file__).read_text(encoding="utf-8")
    return frozenset(_RUN_ID_SHAPE.findall(source))


#: Every prototype Run id; none collide with the canary's own ``RUN-00000001``.
PROTOTYPE_RUN_IDS: frozenset[str] = _prototype_run_ids()

#: Fixture Task ids drawn by Activity's and Run's own fixture paths
#: (``QUEUE``/``SEARCH_HITS`` and the ``run_detail.py`` breadcrumb in
#: ``prototype.py``); none collide with the canary's ``W37CANARY-000N`` tasks.
FIXTURE_TASK_IDS: frozenset[str] = frozenset({"EAWF-0001", "EAWF-0042", "EAWF-0044"})

#: The fixture's scope name, printed in every fixture-path header crumb
#: (``renderers/scope_home.py`` etc. all interpolate ``fixture.proto.scope``).
#: A native frame prints the daemon's own digest-derived scope id instead, so
#: this string appearing anywhere is proof a route fell back to the fixture.
FIXTURE_SCOPE = "eawf-core"

#: The fixture's hardcoded cursor, spelled out in ``reads.py``'s prototype path.
FIXTURE_REVISION_LITERALS: frozenset[str] = frozenset({"41,208", "41208"})

#: The prototype's "own" Milestone, opened when a Milestone frame names no
#: subject. Distinct from the canary's MLS-0001 (which legitimately renders
#: live since W42 -- see the module docstring), so a bare check is safe here.
FIXTURE_MILESTONE_IDS: frozenset[str] = frozenset({"MLS-0004"})


def assert_no_fixture_literal(frames: Mapping[str, str]) -> None:
    """Raise, naming every frame and literal, when a fixture-only token renders.

    Args:
        frames: Rendered frame text keyed by a ``route@WxH`` label.

    Raises:
        AssertionError: at least one frame carries a fixture-only literal.
    """
    literals = (
        *PROTOTYPE_RUN_IDS,
        *FIXTURE_TASK_IDS,
        *FIXTURE_REVISION_LITERALS,
        *FIXTURE_MILESTONE_IDS,
        FIXTURE_SCOPE,
    )
    hits = [
        f"{label}: {literal!r}"
        for label, text in frames.items()
        for literal in literals
        if literal in text
    ]
    if hits:
        raise AssertionError("fixture literal(s) rendered live: " + "; ".join(hits))


def test_no_live_journey_frame_carries_a_fixture_literal(tmp_path: Path) -> None:
    """CR-02: the five journeys against the real canary daemon carry no fixture id.

    MLS-0001 is deliberately not in the checked literal set -- see the module
    docstring -- because it legitimately renders on ``scope.home`` since W42.
    """

    walk, runtime_root = walk_canary_isolated(tmp_path)

    async def body() -> dict[str, str]:
        frames: dict[str, str] = {}
        async with (
            live_console(walk, runtime_root) as (app, _seam),
            app.run_test(size=SIZES[0]) as pilot,
        ):
            for size_index, (w, h) in enumerate(SIZES):
                for route in JOURNEYS:
                    text = await render_setup(
                        app, pilot, SessionSetup(route=route, size=size_index)
                    )
                    frames[f"{route}@{w}x{h}"] = text
        return frames

    frames = asyncio.run(body())

    assert_no_fixture_literal(frames)


def test_check_reds_when_the_prototype_registers_are_injected(tmp_path: Path) -> None:
    """Gate-fire proof: the same check reds on a console holding the prototype registers.

    This is the seeded defect the gate must catch: a :class:`ConsoleApp` built
    from the tracked golden fixture (``ConsoleApp(load_fixture(...), FakeClock())``,
    holding no seam) is exactly the shape the honesty-floor bug drew before P34 --
    every unheld route falling back to the prototype's invented rows. No daemon
    is needed here; the defect is the fixture itself. This also proves the check
    is not vacuous the other way: ``eawf-core`` and a prototype Run id genuinely
    do render on this fixture-drawn console (``run.detail``'s own fixture path
    opens on ``prototype.OWN_RUN`` when no subject is set), so the live suite's
    clean pass is not because these tokens are unreachable in general.
    """

    async def body() -> dict[str, str]:
        frames: dict[str, str] = {}
        app = ConsoleApp(load_fixture(FIXTURE_DIR), FakeClock())
        async with app.run_test(size=SIZES[0]) as pilot:
            for route in JOURNEYS:
                text = await render_setup(app, pilot, SessionSetup(route=route, size=2))
                frames[f"{route}@160x40"] = text
        return frames

    fixture_frames = asyncio.run(body())

    joined = "".join(fixture_frames.values())
    assert FIXTURE_SCOPE in joined
    assert any(run_id in joined for run_id in PROTOTYPE_RUN_IDS), (
        "at least one prototype Run id must be reachable to prove this"
    )

    with pytest.raises(AssertionError, match="fixture literal"):
        assert_no_fixture_literal(fixture_frames)


def test_assert_no_fixture_literal_names_every_hit() -> None:
    """Boundary: a frame carrying two distinct literals is reported once per hit."""
    frames = {"scope.home@80x24": f"{FIXTURE_SCOPE} {next(iter(PROTOTYPE_RUN_IDS))}"}

    with pytest.raises(AssertionError) as excinfo:
        assert_no_fixture_literal(frames)

    message = str(excinfo.value)
    assert message.count("scope.home@80x24:") == 2


def test_assert_no_fixture_literal_passes_an_empty_frame_map() -> None:
    """Boundary: nothing rendered is trivially clean."""
    assert_no_fixture_literal({})  # must not raise
