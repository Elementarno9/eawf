"""Compare the shipped price files with the vendor pricing pages.

Each price row names the page its rates were read from. The online check
fetches every such page once, reads the prices on it and compares each priced
class of each row with the page. It only reads: the price files are never
written, so a difference is a prompt for a person to update a row.

The check calls the file current only when it compared every priced class of
every row in effect, tiers and modifiers included, and found each equal. A
class it could not compare, because the page does not list the row, the page
changed shape or a part of it could not be read, is reported as unchecked and
ends the check with status 2, the same as an unreadable page.

What is read on each page:

* Anthropic: the price table (a table whose header names a model column and a
  cache-write column; a row one cell short continues the model above with its
  larger-prompt rates), the table under the "Fast mode pricing" heading, and
  the US-region multiplier the page states in prose.
* OpenAI: the server-rendered props of the standard-tier price table (batch,
  fast and other tiers are not list prices), with the 12-hour write derived as
  2.5 times input where a write is offered, as the page derives it; and the
  larger-prompt rates, which only the page's own script states.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.kernel.config.schema import PriceRates
from eawf.observability.telemetry.pricing import PriceRow, Vendor, load_price_book

logger = logging.getLogger(__name__)

__all__ = [
    "PageParseError",
    "PageRow",
    "PriceCheckReport",
    "PriceDifference",
    "UnreadablePage",
    "VendorPage",
    "check_prices_online",
    "fetch_page",
    "read_anthropic_page",
    "read_openai_long_context",
    "read_openai_page",
    "render_price_check",
]

#: Seconds one page fetch may take before the page counts as unreadable.
_FETCH_TIMEOUT_SECONDS: Final = 30

#: The User-Agent of a page fetch; it names the tool and nothing about the caller.
_USER_AGENT: Final = "eawf-price-check"

#: Header label, lowercased, to the price class its column states.
_COLUMN_CLASSES: Final[dict[str, str]] = {
    "input": "input",
    "output": "output",
    "cached input": "cache_read",
    "hits and refreshes": "cache_read",
    "5m writes": "cache_write.5m",
    "1h writes": "cache_write.1h",
    "30m write": "cache_write.30m",
    "12h write": "cache_write.12h",
}

#: Header labels, lowercased, of the column that names the model.
_MODEL_LABELS: Final = frozenset({"model", "name"})

#: The heading, lowercased, the Anthropic fast-mode table sits under.
_FAST_HEADING: Final = "fast mode pricing"

#: The OpenAI page bills a 12-hour cache write at this multiple of input.
_OPENAI_12H_WRITE_FACTOR: Final = Decimal("2.5")

_DOLLARS: Final = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")
_ANTHROPIC_NAME: Final = re.compile(r"\bClaude\s+([A-Z][a-z]+)\s+(\d+(?:\.\d+)?)\b")
_OPENAI_NAME: Final = re.compile(r"\s*([a-z][a-z0-9.\-]*)")
_GEO_MULTIPLIER: Final = re.compile(r"inference_geo\b[^.]*?\b(\d+(?:\.\d+)?)x\b[^.]*?multiplier")
_STANDARD_MAP: Final = re.compile(r"\bstandard:\{(?=\")")
_SCRIPT_ENTRY: Final = re.compile(r'"([^"]+)":\{([^{}]*)\}')
_JS_NUMBER: Final = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)$")
_JS_NOT_OFFERED: Final = frozenset({"`-`", '"-"', "null"})
_SCRIPT_KEYS: Final = {
    "input": "input",
    "output": "output",
    "cachedInput": "cache_read",
    "cacheWrite": "cache_write.30m",
}

Rates = Mapping[str, Decimal | None]
"""Price class (``input``, ``cache_write.5m``, ...) to its rate; ``None`` where not offered."""


class PageParseError(ValueError):
    """A page or its script no longer has the structure the check reads."""


@dataclass(frozen=True, slots=True)
class PageRow:
    """The rates a vendor page states for one model.

    Attributes:
        model: The model id the row names.
        rates: The standard rates by class.
        tier: The larger-prompt rates, when the page states them.
        fast: The fast-mode input and output rates, when the page states them.
    """

    model: str
    rates: Rates
    tier: Rates | None = None
    fast: Rates | None = None


@dataclass(frozen=True, slots=True)
class VendorPage:
    """What one vendor page states.

    Attributes:
        rows: Model id to its rates.
        geo_us: The US-region multiplier the page states, if it states one.
        misses: Part of the page (``tiers``, ``fast``, ``geo_us``) to why it
            could not be read; every priced class of that part stays unchecked.
    """

    rows: Mapping[str, PageRow]
    geo_us: Decimal | None = None
    misses: Mapping[str, str] = field(default_factory=dict)


class _PageReader(HTMLParser):
    """Collect each table with the heading above it, and the page's visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._heading: list[str] | None = None
        self._last_heading = ""
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._hidden += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = []
        elif tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
                self.headings.append(self._last_heading)
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._row is not None and tag in {"td", "th"}:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._hidden = max(self._hidden - 1, 0)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading is not None:
            self._last_heading = " ".join(" ".join(self._heading).split())
            self._heading = None
        elif tag == "table":
            self._depth = max(self._depth - 1, 0)
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._depth == 1:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        self.text.append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._cell is not None:
            self._cell.append(data)


def _anthropic_models(cell: str) -> list[str]:
    """Return ``claude-opus-4-8`` for each ``Claude Opus 4.8`` a cell names."""
    return [
        f"claude-{found.group(1).lower()}-{found.group(2).replace('.', '-')}"
        for found in _ANTHROPIC_NAME.finditer(cell)
    ]


def _rate(cell: str) -> Decimal | None:
    """Return the first dollar amount in *cell*, or ``None`` when it states none."""
    found = _DOLLARS.search(cell)
    return None if found is None else Decimal(found.group(1).replace(",", ""))


def _price_tables(reader: _PageReader) -> dict[str, PageRow]:
    """Return the rows of the tables whose header names a model and a cache-write column.

    A model listed in several such tables keeps the first; an empty dict means
    the page holds no price table.
    """
    found: dict[str, PageRow] = {}
    for table in reader.tables:
        header_at = max(
            (i for i, row in enumerate(table) if _MODEL_LABELS & {c.lower() for c in row}),
            default=None,
        )
        if header_at is None:
            continue
        labels = [cell.lower() for cell in table[header_at]]
        classes = {
            i: _COLUMN_CLASSES[label] for i, label in enumerate(labels) if label in _COLUMN_CLASSES
        }
        if not any(name.startswith("cache_write.") for name in classes.values()):
            continue
        name_at = next(i for i, label in enumerate(labels) if label in _MODEL_LABELS)
        previous: list[str] = []
        for cells in table[header_at + 1 :]:
            if len(cells) == len(labels):
                rates = {name: _rate(cells[i]) for i, name in classes.items()}
                previous = [m for m in _anthropic_models(cells[name_at]) if m not in found]
                for model in previous:
                    found[model] = PageRow(model, rates)
            elif previous and len(cells) == len(labels) - 1 and name_at == 0:
                # A row one cell short continues the model above: its
                # larger-prompt rates, with the name cell spanning both rows.
                tier = {name: _rate(cells[i - 1]) for i, name in classes.items()}
                for model in previous:
                    found[model] = PageRow(model, found[model].rates, tier)
                previous = []
            else:
                previous = []
    return found


def _fast_rates(reader: _PageReader) -> dict[str, Rates]:
    """Return the fast-mode rates of the one table under the fast-mode heading.

    Raises:
        PageParseError: No such table, or more than one, or one without a
            model, input and output column.
    """
    tables = [
        table
        for table, heading in zip(reader.tables, reader.headings, strict=True)
        if heading.lower().startswith(_FAST_HEADING) and table
    ]
    if len(tables) != 1:
        raise PageParseError(f"found {len(tables)} tables under '{_FAST_HEADING}', expected 1")
    header, *body = tables[0]
    labels = [cell.lower() for cell in header]
    if "input" not in labels or "output" not in labels or not _MODEL_LABELS & set(labels):
        raise PageParseError(f"the fast-mode table has columns {header}")
    name_at = next(i for i, label in enumerate(labels) if label in _MODEL_LABELS)
    at_input, at_output = labels.index("input"), labels.index("output")
    found: dict[str, Rates] = {}
    for cells in body:
        if len(cells) != len(labels):
            continue
        for model in _anthropic_models(cells[name_at]):
            found.setdefault(
                model, {"input": _rate(cells[at_input]), "output": _rate(cells[at_output])}
            )
    if not found:
        raise PageParseError("the fast-mode table names no model")
    return found


def _geo_multiplier(text: str) -> Decimal:
    """Return the one US-region multiplier the page's prose states.

    Raises:
        PageParseError: The page states none, or states two that disagree.
    """
    stated = {Decimal(value) for value in _GEO_MULTIPLIER.findall(text)}
    if len(stated) != 1:
        raise PageParseError(f"found {len(stated)} distinct US-region multipliers, expected 1")
    return stated.pop()


def read_anthropic_page(html: str) -> VendorPage:
    """Read the price table, fast-mode table and US-region multiplier of an Anthropic page.

    Args:
        html: The page.

    Returns:
        The page; a part that could not be read is named in ``misses``.
    """
    reader = _PageReader()
    reader.feed(html)
    reader.close()
    rows = _price_tables(reader)
    misses: dict[str, str] = {}
    try:
        fast = _fast_rates(reader)
    except PageParseError as exc:
        misses["fast"] = str(exc)
        fast = {}
    geo: Decimal | None = None
    try:
        geo = _geo_multiplier(" ".join(" ".join(reader.text).split()))
    except PageParseError as exc:
        misses["geo_us"] = str(exc)
    merged = {
        model: PageRow(model, row.rates, row.tier, fast.get(model)) for model, row in rows.items()
    }
    return VendorPage(rows=merged, geo_us=geo, misses=misses)


class _IslandReader(HTMLParser):
    """Collect the attributes of every ``astro-island`` element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.islands: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "astro-island":
            self.islands.append(dict(attrs))


def _astro(value: object) -> object:
    """Decode Astro's prop encoding: ``[0, value]`` is a value, ``[1, [...]]`` an array.

    Raises:
        PageParseError: *value* is not in that encoding.
    """
    if isinstance(value, list) and len(value) == 2:
        tag, inner = value
        if tag == 0:
            return inner
        if tag == 1 and isinstance(inner, list):
            return [_astro(item) for item in inner]
    raise PageParseError(f"unknown prop encoding: {str(value)[:80]}")


def _page_rate(value: object) -> Decimal | None:
    """Return a props rate: a number, or ``None`` for ``-`` or null.

    Raises:
        PageParseError: The value is neither.
    """
    if value is None or value == "-":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return Decimal(str(value))
    raise PageParseError(f"unknown rate {value!r}")


def _with_12h(rates: dict[str, Decimal | None]) -> Rates:
    """Add the 12-hour write the OpenAI page derives from input where a write is offered."""
    rate_in, write = rates.get("input"), rates.get("cache_write.30m")
    twelve_hour = None
    if rate_in is not None and write is not None:
        twelve_hour = rate_in * _OPENAI_12H_WRITE_FACTOR
    return {**rates, "cache_write.12h": twelve_hour}


def read_openai_page(html: str) -> tuple[dict[str, PageRow], str]:
    """Read the standard-tier price rows from the OpenAI page's server-rendered props.

    A row of five values is ``model, input, cached input, 30m write, output``;
    a row of four has no write column, so it offers no write.

    Args:
        html: The page.

    Returns:
        Model id to its standard rates, and the URL of the script the table
        is rendered by, which states the larger-prompt rates.

    Raises:
        PageParseError: The page holds no standard-tier table, or more than
            one, or its rows are not in the encoding read here.
    """
    reader = _IslandReader()
    reader.feed(html)
    reader.close()
    standard: list[tuple[dict[str, object], str | None]] = []
    for island in reader.islands:
        try:
            props = json.loads(island.get("props") or "null")
        except ValueError:
            continue
        if isinstance(props, dict) and props.get("tier") == [0, "standard"] and "rows" in props:
            standard.append((props, island.get("component-url")))
    if len(standard) != 1:
        raise PageParseError(f"found {len(standard)} standard-tier price tables, expected 1")
    props, script = standard[0]
    if not script:
        raise PageParseError("the standard-tier table names no script")
    decoded = _astro(props["rows"])
    if not isinstance(decoded, list):
        raise PageParseError("the standard-tier rows are not a list")
    found: dict[str, PageRow] = {}
    for row in decoded:
        if not isinstance(row, list) or len(row) not in {4, 5} or not isinstance(row[0], str):
            raise PageParseError(f"a standard-tier row has an unknown shape: {str(row)[:80]}")
        name = _OPENAI_NAME.match(row[0])
        if name is None:
            raise PageParseError(f"a standard-tier row names no model: {row[0]!r}")
        values = [_page_rate(value) for value in row[1:]]
        write = values[2] if len(values) == 4 else None
        rates = {
            "input": values[0],
            "cache_read": values[1],
            "cache_write.30m": write,
            "output": values[-1],
        }
        found.setdefault(name.group(1), PageRow(name.group(1), _with_12h(rates)))
    return found, script


def _script_rate(raw: str) -> Decimal | None:
    """Return a script literal's rate: a number, or ``None`` for ``-`` or null.

    Raises:
        PageParseError: The literal is neither.
    """
    if raw in _JS_NOT_OFFERED:
        return None
    if _JS_NUMBER.match(raw):
        return Decimal(f"0{raw}" if raw.startswith(".") else raw)
    raise PageParseError(f"unknown rate literal {raw!r}")


def _object_end(text: str, opening: int) -> int:
    """Return the index of the brace closing the one at *opening*.

    Raises:
        PageParseError: The braces do not close.
    """
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise PageParseError("the standard larger-prompt map does not close")


def read_openai_long_context(script: str) -> dict[str, Rates]:
    """Read the standard-tier larger-prompt rates from the OpenAI page's script.

    The script holds one ``standard:{"<model>":{input:..,output:..,cachedInput:..,
    cacheWrite:..},...}`` map beside the batch, fast and other tiers' maps.

    Args:
        script: The script's source.

    Returns:
        Model id to its larger-prompt rates, the 12-hour write derived as on
        the page.

    Raises:
        PageParseError: The script holds no such map, or more than one, or an
            entry the reader does not know.
    """
    starts = [found.end() - 1 for found in _STANDARD_MAP.finditer(script)]
    if len(starts) != 1:
        raise PageParseError(f"found {len(starts)} standard larger-prompt maps, expected 1")
    body = script[starts[0] + 1 : _object_end(script, starts[0])]
    if _SCRIPT_ENTRY.sub("", body).strip(", "):
        raise PageParseError("the standard larger-prompt map holds an entry of unknown shape")
    found: dict[str, Rates] = {}
    for entry in _SCRIPT_ENTRY.finditer(body):
        pairs = dict(part.split(":", 1) for part in entry.group(2).split(",") if ":" in part)
        if set(pairs) != set(_SCRIPT_KEYS):
            raise PageParseError(f"{entry.group(1)} states {sorted(pairs)}")
        rates = {_SCRIPT_KEYS[key]: _script_rate(value.strip()) for key, value in pairs.items()}
        found[entry.group(1)] = _with_12h(rates)
    return found


class PriceDifference(BaseModel):
    """One class whose rate in the file is not the rate on the vendor page.

    Attributes:
        model_id: The price row.
        field: The class: ``tiers[0].`` prefixed for the larger-prompt tier,
            ``modifiers.`` for fast mode and the US region.
        file: The rate in the file; ``None`` where the file states none.
        page: The rate on the page; ``None`` where the page states none.
        source_url: The page.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str
    field: str
    file: Decimal | None
    page: Decimal | None
    source_url: str


class UnreadablePage(BaseModel):
    """A page the check could not compare against.

    Attributes:
        source_url: The page.
        reason: Why: the fetch error, a changed page shape, or that no price
            table names a row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_url: str
    reason: str


class PriceCheckReport(BaseModel):
    """What comparing the price files with the vendor pages found.

    Attributes:
        checked_rows: Rows found on their page.
        checked_classes: Classes compared with the page, equal or not.
        differences: Classes whose rates disagree.
        unchecked: Priced classes the check could not compare, one line per
            row and cause.
        unreadable: Pages that could not be compared.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_rows: int
    checked_classes: int
    differences: tuple[PriceDifference, ...]
    unchecked: tuple[str, ...]
    unreadable: tuple[UnreadablePage, ...]

    @property
    def exit_status(self) -> Literal[0, 1, 2]:
        """Return 2 when a page or a priced class could not be checked, else 1 on a difference."""
        if self.unreadable or self.unchecked:
            return 2
        return 1 if self.differences else 0


def fetch_page(url: str) -> str:
    """Return the page at *url* as text.

    Raises:
        OSError: The page could not be fetched (``urllib.error.URLError``
            and timeouts are ``OSError``).
        ValueError: The page is not UTF-8.
        http.client.HTTPException: The answer broke off or was malformed.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as answer:
        body: bytes = answer.read()
    return body.decode("utf-8")


_FETCH_ERRORS: Final = (OSError, ValueError, http.client.HTTPException)


def _same_site(page_url: str, script: str) -> str:
    """Return *script* resolved against *page_url*, refusing another site.

    Raises:
        PageParseError: The script is on another site.
    """
    resolved = urllib.parse.urljoin(page_url, script)
    page, found = urllib.parse.urlsplit(page_url), urllib.parse.urlsplit(resolved)
    if (found.scheme, found.netloc) != (page.scheme, page.netloc):
        raise PageParseError(f"the price script is on another site: {found.netloc}")
    return resolved


def _read_page(url: str, vendor: Vendor) -> VendorPage:
    """Fetch and read one vendor page, and for OpenAI its price script.

    Raises:
        OSError, ValueError, http.client.HTTPException: The page could not be
            fetched or read; :class:`PageParseError` is a ``ValueError``.
    """
    if vendor == "anthropic":
        return read_anthropic_page(fetch_page(url))
    rows, script = read_openai_page(fetch_page(url))
    try:
        tiers = read_openai_long_context(fetch_page(_same_site(url, script)))
    except _FETCH_ERRORS as exc:
        return VendorPage(rows=rows, misses={"tiers": f"price script unreadable: {exc}"})
    return VendorPage(
        rows={model: PageRow(model, row.rates, tiers.get(model)) for model, row in rows.items()}
    )


def _classes(rates: PriceRates | None) -> dict[str, Decimal | None]:
    """Return a row's rates keyed by the classes a page states."""
    if rates is None:
        return {}
    return {
        "input": rates.input,
        "output": rates.output,
        "cache_read": rates.cache_read,
        **{f"cache_write.{name}": rate for name, rate in rates.cache_write.items()},
    }


@dataclass(slots=True)
class _Tally:
    """The differences, unchecked classes and compared count of one check."""

    url: str
    differences: list[PriceDifference] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)
    compared: int = 0

    def side(self, row_id: str, prefix: str, own: Rates, stated: Rates | None, absent: str) -> None:
        """Compare one set of classes; with *stated* ``None`` each priced class is unchecked."""
        if stated is None:
            priced = [f"{prefix}{name}" for name, rate in sorted(own.items()) if rate is not None]
            if priced:
                self.unchecked.append(f"{row_id}: {absent}: {', '.join(priced)}")
            return
        missing: list[str] = []
        for name in sorted(own.keys() | stated.keys()):
            file_rate = own.get(name)
            if name not in stated:
                if file_rate is not None:
                    missing.append(f"{prefix}{name}")
                continue
            page_rate = stated[name]
            if file_rate is None and page_rate is None:
                continue
            self.compared += 1
            if file_rate != page_rate:
                self.differences.append(
                    PriceDifference(
                        model_id=row_id,
                        field=f"{prefix}{name}",
                        file=file_rate,
                        page=page_rate,
                        source_url=self.url,
                    )
                )
        if missing:
            self.unchecked.append(f"{row_id}: the page states no {', '.join(missing)}")


def _priced(row: PriceRow) -> bool:
    """Return whether the row prices any class, base, tier or modifier."""
    return (
        any(rate is not None for rate in _classes(row.rates).values())
        or bool(row.tiers)
        or row.modifiers.fast is not None
        or row.modifiers.geo_us is not None
    )


def _compare(tally: _Tally, row: PriceRow, found: PageRow, page: VendorPage) -> None:
    """Compare every class of *row* with *found*: base rates, tiers, then modifiers."""
    tally.side(row.id, "", _classes(row.rates), found.rates, "the page states no rates")
    tier_miss = page.misses.get("tiers")
    tally.side(
        row.id,
        "tiers[0].",
        _classes(row.tiers[0].rates) if row.tiers else {},
        None if tier_miss else found.tier,
        f"could not read the larger-prompt rates ({tier_miss})"
        if tier_miss
        else "the page states no larger-prompt tier",
    )
    for index, extra in enumerate(row.tiers[1:], start=1):
        tally.side(
            row.id, f"tiers[{index}].", _classes(extra.rates), None, "the page states one tier"
        )
    fast = row.modifiers.fast
    fast_miss = page.misses.get("fast")
    tally.side(
        row.id,
        "modifiers.fast.",
        {"input": fast.input, "output": fast.output} if fast is not None else {},
        None if fast_miss else found.fast,
        f"could not read the fast-mode rates ({fast_miss})"
        if fast_miss
        else "the page states no fast-mode rates",
    )
    if row.modifiers.geo_us is not None:
        geo_miss = page.misses.get("geo_us")
        tally.side(
            row.id,
            "modifiers.",
            {"geo_us": row.modifiers.geo_us},
            None if page.geo_us is None else {"geo_us": page.geo_us},
            f"could not read the US-region multiplier ({geo_miss})"
            if geo_miss
            else "the page states no US-region multiplier",
        )


def check_prices_online() -> PriceCheckReport:
    """Compare every row in effect today with the page it names.

    The shipped files are read without the operator's overrides, since the
    check is about the files. Pages are fetched through :func:`fetch_page`.

    Returns:
        The report; its ``exit_status`` is 0 only when every priced class of
        every row was compared and equal.
    """
    book = load_price_book(repo_root=None, overrides={})
    today = datetime.now(tz=UTC).date()
    differences: list[PriceDifference] = []
    unchecked: list[str] = []
    unreadable: list[UnreadablePage] = []
    rows_checked = 0
    classes_checked = 0
    for file in book.files:
        rows = [row for row in file.models if row.in_effect(today)]
        for url in sorted({row.source_url for row in rows}):
            try:
                page = _read_page(url, file.vendor)
            except PageParseError as exc:
                unreadable.append(UnreadablePage(source_url=url, reason=f"page changed: {exc}"))
                continue
            except _FETCH_ERRORS as exc:
                unreadable.append(UnreadablePage(source_url=url, reason=f"fetch failed: {exc}"))
                continue
            own = [row for row in rows if row.source_url == url]
            matched = {row.id: _on_page(row, page.rows) for row in own}
            if not any(matched.values()):
                unreadable.append(
                    UnreadablePage(source_url=url, reason="no price table names a row of the file")
                )
                continue
            tally = _Tally(url=url)
            for row in own:
                found = matched[row.id]
                if found is None:
                    if _priced(row):
                        tally.unchecked.append(f"{row.id}: not on the page")
                    continue
                _compare(tally, row, found, page)
                rows_checked += 1
            differences.extend(tally.differences)
            unchecked.extend(tally.unchecked)
            classes_checked += tally.compared
    report = PriceCheckReport(
        checked_rows=rows_checked,
        checked_classes=classes_checked,
        differences=tuple(differences),
        unchecked=tuple(unchecked),
        unreadable=tuple(unreadable),
    )
    logger.info(
        f"check_prices_online rows={rows_checked} classes={classes_checked} "
        f"differences={len(differences)} unchecked={len(unchecked)} "
        f"unreadable={len(unreadable)}"
    )
    return report


def _on_page(row: PriceRow, page: Mapping[str, PageRow]) -> PageRow | None:
    """Return the page row naming *row*'s id or one of its ``match`` ids."""
    return next((page[name] for name in (row.id, *row.match) if name in page), None)


def render_price_check(report: PriceCheckReport) -> str:
    """Render the report as Markdown, for a terminal and a CI job summary alike."""
    if report.exit_status == 2:
        head = "Prices: could not check every priced class; the file is not confirmed current."
    elif report.differences:
        head = f"Prices: {len(report.differences)} rates differ from the vendor pages."
    else:
        head = (
            f"Prices: the file matches the vendor pages ({report.checked_rows} rows, "
            f"{report.checked_classes} classes compared)."
        )
    lines = [head]
    lines.extend(f"- unreadable: {page.source_url}: {page.reason}" for page in report.unreadable)
    lines.extend(
        f"- {diff.model_id} {diff.field}: file {diff.file if diff.file is not None else 'none'}, "
        f"page {diff.page if diff.page is not None else 'none'} ({diff.source_url})"
        for diff in report.differences
    )
    lines.extend(f"- unchecked: {line}" for line in report.unchecked)
    return "\n".join(lines)
