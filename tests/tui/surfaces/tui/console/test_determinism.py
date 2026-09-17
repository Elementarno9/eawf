"""Console frames and values are identical on every machine at one revision.

Numbers group with a comma, times are UTC with no zone suffix, dates use the console's own
month abbreviations, and nothing asks the locale or the local timezone. A bundle of chrome
rendered by the header, keybar, palette and action menu, plus formatted values, comes out
the same twice in a row and with the process timezone and locale varied around it. No
console module imports ``locale``, calls ``strftime`` or converts through the local zone.
"""

from __future__ import annotations

import ast
import locale
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from eawf.surfaces.tui.console import format as console_format
from eawf.surfaces.tui.console.action_menu import (
    ActionMenus,
    Availability,
    MenuVerb,
    VerbWeight,
    menu_rows,
)
from eawf.surfaces.tui.console.format import (
    MONTHS,
    clock_minute,
    clock_time,
    day,
    fixed,
    group,
    seconds,
    span,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS, route_bar
from eawf.surfaces.tui.console.palette import PaletteEntity, hits, palette_rows
from eawf.surfaces.tui.console.session import SIZES, Session

CONSOLE_DIR = Path(console_format.__file__).resolve().parent
STAMP = datetime(2026, 7, 28, 16, 0, 12, tzinfo=timezone(timedelta(hours=2)))
LOCALES = ("de_DE.UTF-8", "fr_FR.UTF-8", "hi_IN.UTF-8", "ar_EG.UTF-8")
ZONES = ("Pacific/Chatham", "America/St_Johns", "Asia/Kathmandu")
MENUS = ActionMenus(
    {
        "run.detail": [
            MenuVerb(
                key="l",
                verb="open transcript",
                available=True,
                weight=VerbWeight.LIGHT,
                target="transcript",
            ),
            MenuVerb(key="c", verb="cancel", available=True, authority="control"),
            MenuVerb(key="t", verb="retry", available=False, reason="the run has not ended"),
        ]
    }
)
ENTITIES = [
    PaletteEntity(id=f"RUN-{n:04d}", route="run.detail", what=f"task {n}") for n in range(40)
]


def _as_declared(verb: MenuVerb) -> Availability:
    return Availability(verb.available, verb.reason)


def _bundle() -> list[str]:
    """Render every console surface this suite pins, plus one of each formatted value."""
    out: list[str] = []
    for w, h in SIZES:
        session = Session(route="activity", conn="LIVE / PARTIAL", sel=17)
        session.back.push(route="run.detail", sel=3, subj="RUN-0007")
        out.append(
            header_row(session, crumb=" Eä ▸ … ▸ Activity", scope="eawf-core", needs=41208, w=w)
        )
        out.extend(route_bar(route, w) for route in sorted(ROUTE_KEYS))
        out.extend(palette_rows(session, hits("run", ENTITIES), w=w, h=h))
        out.extend(menu_rows(MENUS.verbs("run.detail"), guard=_as_declared, w=w))
    out += [
        group(41208),
        fixed(0.31, places=2),
        seconds(1.5),
        span(128),
        clock_time(STAMP),
        clock_minute(STAMP),
        day(STAMP),
        day(STAMP, year=True),
    ]
    return out


BASELINE = _bundle()


@pytest.fixture
def restore_locale() -> Iterator[None]:
    saved = locale.setlocale(locale.LC_ALL)
    try:
        yield
    finally:
        locale.setlocale(locale.LC_ALL, saved)


@pytest.fixture
def restore_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    try:
        yield monkeypatch
    finally:
        monkeypatch.undo()
        time.tzset()


def test_render_bundle_is_identical_across_two_runs() -> None:
    assert _bundle() == _bundle() == BASELINE


@pytest.mark.parametrize("zone", ZONES)
def test_render_bundle_is_identical_under_a_varied_timezone(
    zone: str, restore_zone: pytest.MonkeyPatch
) -> None:
    restore_zone.setenv("TZ", zone)
    time.tzset()
    assert time.localtime(STAMP.timestamp()).tm_hour != STAMP.astimezone(UTC).hour
    assert _bundle() == BASELINE


@pytest.mark.parametrize("name", LOCALES)
@pytest.mark.usefixtures("restore_locale")
def test_render_bundle_is_identical_under_a_varied_locale(name: str) -> None:
    try:
        locale.setlocale(locale.LC_ALL, name)
    except locale.Error:
        pytest.skip(f"locale {name} is not installed here")
    assert _bundle() == BASELINE


@pytest.mark.usefixtures("restore_locale")
def test_day_month_names_ignore_a_locale_that_renames_them() -> None:
    try:
        locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("locale de_DE.UTF-8 is not installed here")
    march = datetime(2026, 3, 2, tzinfo=UTC)
    if march.strftime("%b") == "Mar":
        pytest.skip("this platform's de_DE locale does not rename March")
    assert day(march) == "Mar 2"
    assert group(41208) == "41,208"


@pytest.mark.parametrize(
    ("n", "expected"),
    [(0, "0"), (999, "999"), (1000, "1,000"), (41208, "41,208"), (-1234567, "-1,234,567")],
)
def test_group_uses_a_comma_for_thousands(n: int, expected: str) -> None:
    assert group(n) == expected


@pytest.mark.parametrize("n", [True, 1.0, "41208", None])
def test_group_non_integer_raises_type_error(n: object) -> None:
    with pytest.raises(TypeError, match="takes an int"):
        group(n)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("x", "places", "expected"),
    [(0.31, 2, "0.31"), (20, 2, "20.00"), (7.0, 1, "7.0"), (2.5, 0, "2"), (1234.5, 1, "1234.5")],
)
def test_fixed_uses_a_point_and_exact_places(x: float, places: int, expected: str) -> None:
    assert fixed(x, places=places) == expected


def test_fixed_negative_places_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        fixed(1.0, places=-1)


@pytest.mark.parametrize(
    ("x", "expected"), [(1.5, "1.5s"), (5.0, "5s"), (0, "0s"), (0.08, "0.08s")]
)
def test_seconds_drops_trailing_zeros(x: float, expected: str) -> None:
    assert seconds(x) == expected


def test_seconds_negative_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        seconds(-0.5)


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (128, "2m 08s"),
        (3599, "59m 59s"),
        (3600, "1h"),
        (3659, "1h"),
        (3900, "1h 05m"),
        (4 * 3600, "4h"),
        (100 * 3600 + 60, "100h 01m"),
    ],
)
def test_span_picks_the_reader_grain(total: int, expected: str) -> None:
    assert span(total) == expected


def test_span_negative_raises_value_error() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        span(-1)


def test_clock_time_converts_to_utc_without_a_suffix() -> None:
    assert clock_time(STAMP) == "14:00:12"
    assert clock_minute(STAMP) == "14:00"
    assert clock_time(datetime(2026, 1, 1, tzinfo=UTC)) == "00:00:00"
    assert clock_minute(datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)) == "23:59"


@pytest.mark.parametrize("render", [clock_time, clock_minute, day])
def test_naive_time_raises_value_error(render: Callable[[datetime], str]) -> None:
    with pytest.raises(ValueError, match="must carry its zone"):
        render(datetime(2026, 7, 28, 14, 0))


@pytest.mark.parametrize(
    ("at", "year", "expected"),
    [
        (STAMP, False, "Jul 28"),
        (STAMP, True, "Jul 28 2026"),
        (datetime(2026, 6, 2, 1, 0, tzinfo=timezone(timedelta(hours=2))), False, "Jun 1"),
        (datetime(2025, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-2))), True, "Jan 1 2026"),
        (datetime(2026, 1, 9, tzinfo=UTC), False, "Jan 9"),
    ],
)
def test_day_renders_the_utc_month_and_day(at: datetime, year: bool, expected: str) -> None:
    assert day(at, year=year) == expected


def test_months_table_is_twelve_english_abbreviations() -> None:
    assert len(MONTHS) == 12
    assert all(len(name) == 3 and name.isascii() and name.istitle() for name in MONTHS)
    assert MONTHS[0] == "Jan" and MONTHS[-1] == "Dec"


def _platform_formatting(tree: ast.AST) -> list[int]:
    """Return the lines that ask the locale or the local zone how to write a value."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [alias.name for alias in node.names]
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if "locale" in names or module == "locale" or module == "zoneinfo":
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            zone_free = method == "astimezone" and not node.args
            if method in {"strftime", "localtime", "ctime", "tzname"} or zone_free:
                hits.append(node.lineno)
        elif isinstance(node, ast.FormattedValue) and _locale_spec(node):
            hits.append(node.lineno)
    return hits


def _locale_spec(node: ast.FormattedValue) -> bool:
    """Return whether an f-string field uses the locale-aware ``n`` presentation type."""
    spec = node.format_spec
    if not isinstance(spec, ast.JoinedStr):
        return False
    literal = "".join(
        part.value
        for part in spec.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    return literal.endswith("n")


def test_platform_formatting_scan_flags_a_locale_or_zone_call() -> None:
    # the scan must red on the defect it exists for, or its green result proves nothing
    bad = ast.parse(
        "import locale\n"
        "def cell(at, n):\n"
        "    return at.strftime('%b') + at.astimezone().isoformat() + f'{n:n}'\n"
    )
    assert sorted(set(_platform_formatting(bad))) == [1, 3]
    good = ast.parse("def cell(at, n):\n    return at.astimezone(UTC).hour + f'{n:,}'\n")
    assert _platform_formatting(good) == []


def test_console_modules_make_no_locale_or_zone_call() -> None:
    offenders = {
        path.relative_to(CONSOLE_DIR).as_posix(): hits
        for path in sorted(CONSOLE_DIR.rglob("*.py"))
        if (hits := _platform_formatting(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert offenders == {}
