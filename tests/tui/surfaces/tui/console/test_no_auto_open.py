"""No projection event opens an overlay, takes focus or changes route (CON-023).

Every arrival -- a keyed patch, an owed read, a daemon's answer to a verb -- repaints
through :meth:`ConsoleApp.arrive`, which compares the session's stance before and after.
A move is put back and counted in ``session.auto_opens``, the observable counter the golden
journeys assert stays zero for the whole session whatever arrives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console import app as app_module
from eawf.surfaces.tui.console.app import ConsoleApp, stance
from eawf.surfaces.tui.console.attention import ATTENTION_ROUTE
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import load_fixture
from eawf.surfaces.tui.console.operations import OperationResult, OperationStatus
from eawf.surfaces.tui.console.session import Session

FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "console" / "golden" / "fixture"


def _app() -> ConsoleApp:
    return ConsoleApp(load_fixture(FIXTURE_ROOT), FakeClock())


def test_con023_stance_is_route_subject_overlay_focus_prefix_and_input() -> None:
    """CON-023: the stance is what only the operator may move."""
    session = Session()
    assert set(stance(session)) == {"route", "subj_id", "overlay", "region", "prefix", "typing"}


def test_con023_a_quiet_arrival_repaints_and_counts_nothing() -> None:
    """CON-023: an arrival that moves nothing only repaints."""

    async def drive() -> tuple[int, int]:
        app = _app()
        async with app.run_test(size=(80, 24)):
            before = app.render_count
            app.arrive()
            return app.session.auto_opens, app.render_count - before

    opens, renders = asyncio.run(drive())
    assert opens == 0
    assert renders == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("overlay", "question"),
        ("route", ATTENTION_ROUTE),
        ("region", "detail"),
        ("prefix", "g"),
        ("typing", True),
        ("subj_id", "RUN-538453eb"),
    ],
)
def test_con023_an_arrival_that_moves_the_stance_is_put_back_and_counted(
    field: str, value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CON-023: whatever an arrival moved is restored, and the counter reads one."""

    async def drive() -> tuple[int, object]:
        app = _app()
        async with app.run_test(size=(80, 24)):
            original = ConsoleApp.render_frame
            moved = {"done": False}

            def render(self: ConsoleApp) -> None:
                if not moved["done"]:
                    moved["done"] = True
                    setattr(self.session, field, value)
                original(self)

            monkeypatch.setattr(ConsoleApp, "render_frame", render)
            before = getattr(app.session, field)
            app.arrive()
            assert getattr(app.session, field) == before
            return app.session.auto_opens, app.session.log[0].note

    opens, note = asyncio.run(drive())
    assert opens == 1
    assert note == "an arriving event moved the focus · put back"


def test_con023_a_seam_patch_on_screen_arrives_through_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CON-023: the seam's patch callback repaints only through ``arrive``."""
    calls: list[str] = []

    async def drive() -> None:
        app = _app()
        async with app.run_test(size=(80, 24)):
            monkeypatch.setattr(app, "arrive", lambda: calls.append("arrive"))
            app._on_seam_patched((app.route_key,))
            app._on_seam_patched((ATTENTION_ROUTE,))
            app._on_seam_patched(("elsewhere",))

    asyncio.run(drive())
    assert calls == ["arrive", "arrive"]


def test_con023_a_daemon_answer_arrives_through_the_guard() -> None:
    """CON-023: a verb's answer raises a toast and never opens anything."""

    async def drive() -> tuple[int, str | None, int]:
        app = _app()
        async with app.run_test(size=(80, 24)):
            result = OperationResult(
                operation_id="op-1",
                target="ACT-0031",
                status=OperationStatus.APPLIED,
                detail="answered",
                disposition=ControlDisposition.CONFIRMED,
            )
            app.announce(result)
            return app.session.auto_opens, app.session.overlay, len(app.session.toasts)

    opens, overlay, toasts = asyncio.run(drive())
    assert opens == 0
    assert overlay is None
    assert toasts == 1


def test_con023_arrive_is_the_only_repaint_on_the_arrival_paths() -> None:
    """CON-023: the patch, owed-read and answer paths call ``arrive``, never a bare repaint."""
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    for method in ("_on_seam_patched", "_load_owed", "announce"):
        body = source.split(f"def {method}(", 1)[1].split("\n    def ", 1)[0]
        body = body.split("\n    async def ", 1)[0]
        assert "self.arrive()" in body, method
        assert "self.render_frame()" not in body, method
