"""The console builds from its packaged chrome alone and never draws a prototype row.

The chrome (bucket labels, menus, connection states, entry states, the settings catalog)
ships inside the package. A console built from it holds no prototype register, so a route
whose read model the seam does not hold draws the unknown token and no row, and nothing
the console loads comes from the test tree. The prototype registers stay the golden
harness's: the fixture carries its own copy of the chrome, and the packaged copy must keep
the same shape as that copy so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import eawf
from eawf.kernel.projection.compute import RouteProjection, build_route_projection
from eawf.surfaces.tui.console import chrome as chrome_module
from eawf.surfaces.tui.console.app import CHROME_OVERLAYS, ConsoleApp, compose_frame
from eawf.surfaces.tui.console.chrome import CHROME_RESOURCE, ConsoleChrome, load_chrome
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.fixture import UNKNOWN_SCOPE, Fixture, Proto, load_fixture
from eawf.surfaces.tui.console.overlays import OVERLAY_RENDERERS
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from eawf.surfaces.tui.console.tokens import TRUTH

CONSOLE = Path(chrome_module.__file__).parent
FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture"
TESTS_ROOT = Path(__file__).resolve().parents[4]
UNKNOWN = TRUTH["unknown"].unicode
AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"

#: Where every renderer read of the fixture's core registers lands. A chrome read is
#: served by the packaged chrome; a prototype read exists only in the golden harness.
CHROME_READS: frozenset[str] = frozenset({"buckets", "xbuckets", "actions", "states", "entry"})
PROTOTYPE_READS: frozenset[str] = frozenset(
    {"scope", "revision", "tracks", "fleet", "attention", "timeline"}
)
_PROTO_READ = re.compile(r"\b(?:fixture|fx)\.proto\.([a-z_]+)")
_FIXTURE_READ = re.compile(r"\b(?:fixture|fx)\.(settings|detail|menus|registers)\b")


@pytest.fixture(scope="module")
def prototype_fixture() -> Fixture:
    """Return the tracked prototype registers the golden contract replays."""
    return load_fixture(FIXTURE_DIR)


@pytest.fixture(scope="module")
def prototype_literals(prototype_fixture: Fixture) -> frozenset[str]:
    """Return every id, name and revision only the prototype registers hold."""
    proto = prototype_fixture.proto
    ids = {row.run for row in proto.fleet} | {track.id for track in proto.tracks}
    ids |= {m.id for track in proto.tracks for m in track.milestones}
    ids |= {row.id for row in proto.attention}
    ids |= set(prototype_fixture.detail)
    return frozenset({*ids, proto.scope, f"{proto.revision:,}"})


def _frame(app: ConsoleApp, route: str, overlay: str | None = None) -> list[str]:
    app.reset(SessionSetup(route=route, overlay=overlay, size=1))
    return compose_frame(app.view())


def _leaks(rows: list[str], literals: frozenset[str]) -> list[str]:
    text = "\n".join(rows)
    return sorted(literal for literal in literals if literal in text)


def _held_seam(route: str) -> ProjectionSeam:
    projection: RouteProjection = build_route_projection(
        route=route,
        document={
            "track": {
                "TRK-7001": {
                    "urn": f"urn:eawf:{SCOPE}:track:TRK-7001",
                    "revision": 1,
                    "status": "ACTIVE",
                }
            }
        },
        cursor=7,
        scope_id=SCOPE,
        generated_at=AT,
    )
    seam = ProjectionSeam(route=route, scope_id=SCOPE, state_path=None, clock=lambda: AT)
    seam._projection = projection
    return seam


def test_load_chrome_reads_the_packaged_file() -> None:
    resource = CONSOLE / "data" / CHROME_RESOURCE
    assert resource.is_file()
    assert TESTS_ROOT not in resource.resolve().parents
    chrome = load_chrome()
    assert chrome.entry
    assert chrome.settings.section_order


def test_load_chrome_returns_an_independent_copy_each_call() -> None:
    first, second = load_chrome(), load_chrome()
    first.settings.stored["ui.color"] = {"repo": "never"}
    assert "ui.color" not in second.settings.stored


def test_console_chrome_rejects_an_unknown_key() -> None:
    raw = json.loads((CONSOLE / "data" / CHROME_RESOURCE).read_text(encoding="utf-8"))
    raw["fleet"] = []
    with pytest.raises(ValidationError, match="fleet"):
        ConsoleChrome.model_validate(raw)


def test_console_chrome_rejects_a_missing_table() -> None:
    raw = json.loads((CONSOLE / "data" / CHROME_RESOURCE).read_text(encoding="utf-8"))
    del raw["entry"]
    with pytest.raises(ValidationError, match="entry"):
        ConsoleChrome.model_validate(raw)


def test_console_app_refuses_a_fixture_and_a_chrome_together(
    prototype_fixture: Fixture,
) -> None:
    with pytest.raises(ValueError, match="pass a fixture or a chrome"):
        ConsoleApp(prototype_fixture, FakeClock(), chrome=load_chrome())


def test_console_app_builds_from_the_packaged_chrome_by_default() -> None:
    app = ConsoleApp(clock=FakeClock())
    assert app.fixture.prototype is False
    assert app.fixture.scope == UNKNOWN_SCOPE
    assert app.fixture.proto.fleet == ()
    assert app.fixture.detail == {}
    assert app.fixture.chrome.entry == load_chrome().entry


def test_console_app_given_a_fixture_keeps_the_prototype_registers(
    prototype_fixture: Fixture,
) -> None:
    app = ConsoleApp(prototype_fixture, FakeClock())
    assert app.fixture is prototype_fixture
    assert app.fixture.prototype is True


def test_console_app_constructs_with_nothing_from_the_tests_tree(tmp_path: Path) -> None:
    """A fresh interpreter builds and draws every route without touching ``tests/``.

    The child runs outside the repository with only the package's own source on its
    path, and an audit hook records every file it opens, so a chrome that quietly reads
    a fixture would name the file it read.
    """
    source_root = Path(eawf.__file__).resolve().parents[1]
    script = textwrap.dedent(
        """
        import sys
        opened = []
        sys.addaudithook(lambda event, args: opened.append(str(args[0]))
                         if event == "open" and isinstance(args[0], str) else None)
        from eawf.surfaces.tui.console.app import ConsoleApp, compose_frame
        from eawf.surfaces.tui.console.clock import FakeClock
        from eawf.surfaces.tui.console.registry import REGISTRY
        from eawf.surfaces.tui.console.session import SIZES, SessionSetup
        app = ConsoleApp(clock=FakeClock())
        for route in REGISTRY.ids:
            app.reset(SessionSetup(route=route, size=1))
            compose_frame(app.view())
        loaded = [getattr(m, "__file__", None) or "" for m in list(sys.modules.values())]
        for path in [*sys.path, *loaded, *opened]:
            print(path)
        """
    )
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    tests_tree = str(TESTS_ROOT)
    touched = [line for line in done.stdout.splitlines() if line.startswith(tests_tree)]
    assert touched == []
    assert any(CHROME_RESOURCE in line for line in done.stdout.splitlines())


@pytest.mark.parametrize("route", REGISTRY.ids)
def test_unheld_route_draws_the_unknown_frame(
    route: str, prototype_literals: frozenset[str]
) -> None:
    rows = _frame(ConsoleApp(clock=FakeClock()), route)
    assert rows[1].strip() == f"NOT HELD · {route} · no read model is held for this route"
    assert any(f" ROWS      {UNKNOWN} " in row for row in rows)
    assert _leaks(rows, prototype_literals) == []


@pytest.mark.parametrize("overlay", sorted(OVERLAY_RENDERERS))
def test_chrome_console_overlay_draws_chrome_or_the_unknown_frame(
    overlay: str, prototype_literals: frozenset[str]
) -> None:
    rows = _frame(ConsoleApp(clock=FakeClock()), "activity", overlay)
    if overlay in CHROME_OVERLAYS:
        assert "NOT HELD" not in rows[1]
    else:
        assert rows[1].strip().startswith("NOT HELD · activity")
    assert _leaks(rows, prototype_literals) == []


@pytest.mark.parametrize("drawer", ["actions", "inspect", "raw"])
def test_chrome_console_row_drawer_draws_the_unknown_frame(
    drawer: str, prototype_literals: frozenset[str]
) -> None:
    rows = _frame(ConsoleApp(clock=FakeClock()), "run.detail", drawer)
    assert rows[1].strip().startswith("NOT HELD · run.detail")
    assert _leaks(rows, prototype_literals) == []


def test_chrome_console_draws_the_held_route_natively(
    prototype_literals: frozenset[str],
) -> None:
    app = ConsoleApp(clock=FakeClock(), seam=_held_seam("scope.home"))
    rows = _frame(app, "scope.home")
    assert "NOT HELD" not in "\n".join(rows)
    assert "TRK-7001" in "\n".join(rows)
    assert _leaks(rows, prototype_literals - {UNKNOWN_SCOPE}) == []


def test_chrome_console_route_keys_claim_nothing_while_unheld() -> None:
    """Enter on an unheld queue opens no Run: the frame drew no row for it to name."""

    async def drive() -> tuple[str, str | None]:
        app = ConsoleApp(clock=FakeClock())
        async with app.run_test(size=SIZES[1]):
            app.reset(SessionSetup(route="unattended", size=1))
            app.press_key("Enter")
            return app.session.route, app.session.subj_id

    assert asyncio.run(drive()) == ("unattended", None)


def test_prototype_console_still_draws_the_prototype_frame(prototype_fixture: Fixture) -> None:
    rows = _frame(ConsoleApp(prototype_fixture, FakeClock()), "activity")
    assert "NOT HELD" not in "\n".join(rows)
    assert prototype_fixture.proto.fleet[0].run in "\n".join(rows)


def test_packaged_chrome_holds_no_prototype_record(prototype_literals: frozenset[str]) -> None:
    text = (CONSOLE / "data" / CHROME_RESOURCE).read_text(encoding="utf-8")
    assert sorted(literal for literal in prototype_literals if literal in text) == []


def test_packaged_chrome_keeps_the_shape_of_the_fixture_chrome(
    prototype_fixture: Fixture,
) -> None:
    packaged, replayed = load_chrome(), prototype_fixture.chrome
    assert packaged.buckets == replayed.buckets
    assert packaged.xbuckets == replayed.xbuckets
    assert packaged.states == replayed.states
    assert {
        route: [(r[0], r[1], r[4]) for r in rows] for route, rows in packaged.actions.items()
    } == {route: [(r[0], r[1], r[4]) for r in rows] for route, rows in replayed.actions.items()}
    assert [(e.id, e.state, e.glyph, e.keys, e.paths) for e in packaged.entry] == [
        (e.id, e.state, e.glyph, e.keys, e.paths) for e in replayed.entry
    ]
    for field in ("layers", "writable", "sections", "types", "doc", "choices", "cats", "rail"):
        assert getattr(packaged.settings, field) == getattr(replayed.settings, field), field
    assert packaged.settings.section_order == replayed.settings.section_order
    assert packaged.settings.stored == {}


def test_core_register_fields_split_into_chrome_and_prototype() -> None:
    chrome_fields = set(ConsoleChrome.model_fields) - {"settings"}
    assert chrome_fields == CHROME_READS
    assert set(Proto.model_fields) == CHROME_READS | PROTOTYPE_READS
    assert not CHROME_READS & PROTOTYPE_READS


def test_renderer_read_inventory_is_classified() -> None:
    """Every read of the core registers in the console is a known chrome or prototype read.

    A read the inventory does not name is a new register reaching a frame, and it has to
    be classified -- chrome ships in the package, a prototype read stays in the harness --
    before it can land.
    """
    proto_reads: dict[str, set[str]] = {}
    fixture_reads: dict[str, set[str]] = {}
    for path in sorted(CONSOLE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        ast.parse(text)
        rel = path.relative_to(CONSOLE).as_posix()
        for name in _PROTO_READ.findall(text):
            proto_reads.setdefault(name, set()).add(rel)
        for name in _FIXTURE_READ.findall(text):
            fixture_reads.setdefault(name, set()).add(rel)
    unclassified = set(proto_reads) - CHROME_READS - PROTOTYPE_READS - {"model_copy"}
    assert unclassified == set(), {name: proto_reads[name] for name in unclassified}
    assert set(fixture_reads) == {"settings", "detail", "menus", "registers"}
    assert "renderers/settings.py" in fixture_reads["settings"]
