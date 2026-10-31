"""The console builds a route's read model once per held answer, however often it draws.

A frame asks for its read model several times (the view, the caret's rest, the key
context), and every ask used to rebuild it from the held projection. A held projection is
frozen and the seam replaces each answer it holds, so the read model built from the same
answers is the one to draw; a new answer is built afresh.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.spine import SpineView
from eawf.surfaces.tui.console import app as app_module
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.live_reads import RECEIPTS_READ
from tests.tui.surfaces.tui.console.test_derived_counts import _projection


class _Seam:
    """A seam holding one projection per route key, one acceptance read and live answers."""

    def __init__(self) -> None:
        self.held: dict[str, RouteProjection] = {}
        self.acceptance: Any = None
        self.answers: dict[str, Any] = {}

    def watch(self, _callback: Callable[..., Any]) -> None:
        """Take the patch callback the console registers; this seam never patches."""

    def watch_appends(self, _callback: Callable[..., Any]) -> None:
        """Take the append callback the console registers; this seam never appends."""

    def retarget(self, _route: str) -> None:
        """Follow the console's route; this seam holds every route it is given."""

    def about(self, _subject: str | None) -> None:
        """Follow the console's subject; this seam reads nothing for it."""

    def page_history(self, _cursor: object) -> None:
        """Follow the history page; this seam reads no history."""

    def projection_for(self, route: str) -> RouteProjection | None:
        return self.held.get(route)

    def acceptance_for(self, _key: str | None) -> Any:
        return self.acceptance

    def live(self, name: str) -> Any:
        return self.answers.get(name)


def _counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[RouteProjection]:
    """Count each build of ``name`` the console module makes, by the projection built."""
    built: list[RouteProjection] = []
    real = getattr(app_module, name)

    def build(projection: RouteProjection, **kwargs: Any) -> Any:
        built.append(projection)
        return real(projection, **kwargs)

    monkeypatch.setattr(app_module, name, build)
    return built


def _record() -> SimpleNamespace:
    """Return an acceptance read holding no bundle, approval, journey or evidence."""
    return SimpleNamespace(
        bundle=None,
        approval=None,
        journey=(),
        evidence=(),
        waiting_approval_urn=None,
        accepted_binding=None,
    )


def _console(route: str) -> tuple[ConsoleApp, _Seam]:
    seam = _Seam()
    console = ConsoleApp(seam=seam)  # type: ignore[arg-type]
    console.session.route = route
    return console, seam


def test_a_route_with_no_held_projection_has_no_read_model() -> None:
    console, _seam = _console("scope.home")
    assert console.route_view() is None
    assert console.route_view() is None


def test_one_held_projection_is_built_once_however_often_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _counting(monkeypatch, "build_spine_view")
    console, seam = _console("scope.home")
    seam.held["scope.home"] = _projection("scope.home")
    first = console.route_view()
    assert first is not None
    assert all(console.route_view() is first for _ in range(3))
    assert built == [seam.held["scope.home"]]


def test_a_new_projection_is_built_afresh(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _counting(monkeypatch, "build_spine_view")
    console, seam = _console("scope.home")
    seam.held["scope.home"] = _projection("scope.home", cursor=1)
    before = console.route_view()
    seam.held["scope.home"] = _projection("scope.home", cursor=2)
    after = console.route_view()
    assert before is not after
    assert isinstance(after, SpineView) and after.source_cursor == "2"
    assert len(built) == 2


def test_an_equal_but_new_projection_is_still_built_afresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _counting(monkeypatch, "build_spine_view")
    console, seam = _console("scope.home")
    seam.held["scope.home"] = _projection("scope.home")
    console.route_view()
    seam.held["scope.home"] = _projection("scope.home")
    console.route_view()
    assert len(built) == 2


def test_moving_between_routes_draws_each_routes_own_read_model() -> None:
    console, seam = _console("scope.home")
    seam.held["scope.home"] = _projection("scope.home")
    seam.held["track"] = _projection("track")
    home = console.route_view()
    console.session.route = "track"
    track = console.route_view()
    console.session.route = "scope.home"
    again = console.route_view()
    assert home is not None and track is not None and again is not None
    assert (home.route, track.route, again.route) == ("scope.home", "track", "scope.home")


def test_an_acceptance_route_is_built_once_per_set_of_held_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _counting(monkeypatch, "build_acceptance_view")
    console, seam = _console("milestone")
    seam.held["milestone"] = _projection("milestone")
    first = console.route_view()
    assert first is not None
    assert console.route_view() is first
    assert len(built) == 1
    seam.answers[RECEIPTS_READ] = ()
    assert console.route_view() is first, "an empty answer draws as no answer"
    seam.acceptance = _record()
    second = console.route_view()
    assert second is not first, "a new acceptance read is drawn afresh"
    assert console.route_view() is second
    seam.acceptance = _record()
    third = console.route_view()
    assert third is not second, "an equal but new acceptance read is drawn afresh"
    seam.held["milestone"] = _projection("milestone", cursor=7)
    assert console.route_view() is not third, "a new projection is drawn afresh"
    assert len(built) == 4
