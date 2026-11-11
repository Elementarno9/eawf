"""Partial pricing, the unpriced-model banner, the API-equivalent headline and the strip."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from eawf.observability.reflect.analysis import HistorySession, Metric, analyze_sessions
from eawf.observability.reflect.html import PageSources, WrittenPages, write_pages
from eawf.observability.reflect.html import pages as pages_module
from eawf.observability.reflect.html.pages import reflect_headline
from eawf.observability.reflect.html.session import cost_text, unpriced_banner, unpriced_tokens
from eawf.observability.reflect.html.shared import compact, pct, usd
from eawf.observability.reflect.rates import pricing_quality
from eawf.observability.telemetry.models import ObservedRequest, PriceSourceKind
from eawf.observability.telemetry.pricing import PriceBook, TokenUsage, load_price_book
from eawf.observability.telemetry.sources.session_history import (
    ParsedTranscript,
    RequestLedger,
    TranscriptFold,
    fold_history_lines,
    observed_rows,
)

_T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 11, tzinfo=UTC)
_SESSION = "5d1c7a2e-0f3b-4b6a-8c9d-1e2f3a4b5c6d"
_OPUS = "claude-opus-5-5"
_STRANGER = "claude-vendor-unknown-9"
_ADVICE = "Add a rate under reflect.pricing.overrides or update the price file."
_BOOK = load_price_book(repo_root=None, overrides={})


# ---- page parsing ---------------------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node]
    text: list[str]

    def all_text(self) -> str:
        return "".join([*self.text, *(child.all_text() for child in self.children)])

    def walk(self) -> list[_Node]:
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found


class _Tree(HTMLParser):
    _VOID = frozenset({"meta", "link", "br", "hr", "img", "input", "col", "source", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, [], [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs}, [], [])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].text.append(data)


def _parse(path: Path) -> _Node:
    tree = _Tree()
    tree.feed(path.read_text(encoding="utf-8"))
    tree.close()
    return tree.root


def _where(root: _Node, name: str, value: str) -> list[_Node]:
    return [node for node in root.walk() if node.attrs.get(name) == value]


def _banner(root: _Node) -> str | None:
    found = _where(root, "data-banner", "unpriced")
    assert len(found) <= 1
    return found[0].all_text() if found else None


# ---- fixtures -------------------------------------------------------------------------


def _ref(name: str) -> str:
    """Return *name* as a session ref, keeping one that already is."""
    if re.fullmatch(r"vsid-[0-9a-f]{32}", name):
        return name
    return f"vsid-{hashlib.sha256(name.encode()).hexdigest()[:32]}"


def _request(
    ref: str,
    ordinal: int,
    model: str | None,
    *,
    tokens: int = 1_000,
    write_5m: int = 0,
    write_1h: int = 0,
    read: int = 0,
    book: PriceBook = _BOOK,
) -> ObservedRequest:
    """One Claude request, priced by *book* the way the sweep prices it."""
    usage = TokenUsage(
        input=tokens,
        output=tokens // 4,
        cache_read=read,
        cache_write={"5m": write_5m, "1h": write_1h},
    )
    quote = None if model is None else book.quote(model, usage, on=_T0.date())
    priced = quote is not None and quote.priced
    return ObservedRequest(
        vendor_session_ref=_ref(ref),
        runtime="claude",
        request_ref=f"req-{hashlib.sha256(f'{ref}:{ordinal}'.encode()).hexdigest()[:32]}",
        ordinal=ordinal,
        ts=_T0 + timedelta(minutes=ordinal),
        model=model,
        input_tokens=usage.input,
        output_tokens=usage.output,
        cache_read_tokens=read,
        cache_write_tokens=write_5m + write_1h,
        cache_write_1h_tokens=write_1h,
        total_tokens=usage.input + usage.output + read + write_5m + write_1h,
        cost_usd=quote.cost_usd if priced and quote is not None else None,
        price_source=PriceSourceKind.LIST_RECONSTRUCTED if priced else PriceSourceKind.UNPRICED,
        rate_table_version=book.version if priced else None,
    )


def _session(ref: str, requests: Sequence[ObservedRequest], index: int) -> HistorySession:
    model = requests[0].model or "unknown"
    costs = [row.cost_usd for row in requests if row.cost_usd is not None]
    return HistorySession(
        session_ref=_ref(ref),
        runtime="claude",
        project_key="cwd-a",
        project_label="SYN",
        started_at=_T0 + timedelta(hours=index),
        ended_at=_T0 + timedelta(hours=index, minutes=30),
        model=model,
        model_family=model,
        orchestrated=False,
        metrics={
            Metric.TOTAL_TOKENS: float(sum(row.total_tokens for row in requests)),
            Metric.FRESH_INPUT_TOKENS: float(sum(row.input_tokens for row in requests)),
            Metric.OUTPUT_TOKENS: float(sum(row.output_tokens for row in requests)),
            Metric.CACHE_READ_TOKENS: float(sum(row.cache_read_tokens for row in requests)),
            Metric.CACHE_WRITE_TOKENS: float(sum(row.cache_write_tokens for row in requests)),
            Metric.MODEL_STEPS: float(len(requests)),
        },
        cost_usd=sum(costs, Decimal(0)) if costs else None,
        pricing_quality=pricing_quality(requests[0].model),
        context=None,
    )


def _pages(
    tmp_path: Path, sessions: dict[str, list[ObservedRequest]], **options: Any
) -> WrittenPages:
    history = [_session(ref, rows, index) for index, (ref, rows) in enumerate(sessions.items())]
    requests = tuple(row for rows in sessions.values() for row in rows)
    sources = PageSources(
        analysis=analyze_sessions(history, requests=requests, now=_NOW),
        session_refs=frozenset(_ref(ref) for ref in sessions),
        requests=requests,
        details={},
        contexts={},
        history_files=len(sessions),
        scope_label="SYN",
    )
    return write_pages(tmp_path / "pages", sources, **options)


def _session_page(pages: WrittenPages, ref: str) -> _Node:
    return _parse(pages.overview.parent / f"session-{_ref(ref).removeprefix('vsid-')}.html")


def _reply(seconds: int, message_id: str, model: str, tokens: int) -> dict[str, Any]:
    stamp = (_T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    return {
        "type": "assistant",
        "sessionId": _SESSION,
        "timestamp": stamp,
        "message": {
            "id": message_id,
            "model": model,
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": tokens, "output_tokens": tokens // 4},
        },
    }


def _fold(tmp_path: Path, records: list[dict[str, Any]]) -> ParsedTranscript:
    path = tmp_path / f"{_SESSION}.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    fold = TranscriptFold(runtime="claude")
    ledger = RequestLedger(prices=_BOOK)
    with path.open("rb") as handle:
        fold_history_lines(handle, fold, ledger, fallback_session_id=_SESSION)
    return ParsedTranscript.merged([fold], ledger.rows.values())


# ---- a session cost sums its priced requests, never blank -----------------------------


def test_partial_price(tmp_path: Path) -> None:
    # partial_price: an unknown model beside Opus 5.5 in one transcript.
    parsed = _fold(
        tmp_path,
        [_reply(10, "msg-a", _STRANGER, 2_000), _reply(20, "msg-b", _OPUS, 1_000)],
    )
    stranger, opus = sorted(parsed.requests, key=lambda row: row.ordinal)
    assert stranger.cost_usd is None and opus.cost_usd is not None

    session, _durations = observed_rows(parsed, project_id="SYN")

    assert session.cost_usd == opus.cost_usd
    assert session.price_source is PriceSourceKind.LIST_RECONSTRUCTED
    expected = f"≥ {usd(opus.cost_usd)} + {compact(stranger.total_tokens)} unpriced tokens"
    assert cost_text(parsed.requests) == expected

    ref = session.vendor_session_ref
    page = _session_page(_pages(tmp_path, {ref: list(parsed.requests)}), ref)
    cells = {
        node.children[0].all_text(): node.children[1].all_text()
        for node in page.walk()
        if node.tag == "div" and [child.tag for child in node.children] == ["dt", "dd"]
    }
    assert cells["API-equivalent"] == expected
    requests_chip = _where(page, "data-section", "requests")[0].all_text()
    assert expected in requests_chip


def test_partial_price_boundaries() -> None:
    ref = "vsid-boundary"
    priced = _request(ref, 1, _OPUS)
    unpriced = _request(ref, 2, _STRANGER, tokens=400)
    empty = _request(ref, 3, _STRANGER, tokens=0)

    assert cost_text([]) == "$0"
    assert unpriced_tokens([]) == 0
    assert cost_text([priced]) == usd(priced.cost_usd)
    assert cost_text([unpriced]) == f"≥ $0 + {compact(unpriced.total_tokens)} unpriced tokens"
    assert cost_text([priced, empty]) == usd(priced.cost_usd)
    assert unpriced_tokens([priced, unpriced, empty]) == unpriced.total_tokens


def test_partial_price_session_with_no_priced_request_stays_null(tmp_path: Path) -> None:
    parsed = _fold(tmp_path, [_reply(10, "msg-a", _STRANGER, 500)])

    session, _durations = observed_rows(parsed, project_id="SYN")

    assert session.cost_usd is None
    assert session.price_source is PriceSourceKind.UNPRICED


def test_partial_price_refuses_a_request_unpriced_with_a_cost() -> None:
    with pytest.raises(ValidationError):
        ObservedRequest(
            vendor_session_ref=_ref("x"),
            runtime="claude",
            request_ref=f"req-{'0' * 32}",
            ordinal=1,
            model=_STRANGER,
            total_tokens=1,
            input_tokens=1,
            cost_usd=Decimal("0.5"),
            price_source=PriceSourceKind.UNPRICED,
        )


# ---- the banner names every unpriced model -------------------------------------------


def test_banner(tmp_path: Path) -> None:
    mixed = "vsid-mixed"
    clean = "vsid-clean"
    rows = [
        _request(mixed, 1, _OPUS, tokens=4_000),
        *(_request(mixed, 2 + n, "gpt-5-codex", tokens=100) for n in range(2)),
        *(_request(mixed, 4 + n, "codex-auto-review", tokens=100) for n in range(5)),
        _request(mixed, 9, None, tokens=700),
        _request(mixed, 10, "<synthetic>", tokens=300),
    ]
    priced_only = [_request(clean, 1, _OPUS)]
    named = 7 * 125
    share = pct(named, sum(row.total_tokens for row in [*rows, *priced_only]))
    expected = (
        f"Unpriced: codex-auto-review (5 requests), gpt-5-codex (2 requests), {share} of"
        f" tokens. {_ADVICE}"
    )

    pages = _pages(tmp_path, {mixed: rows, clean: priced_only})

    assert _banner(_parse(pages.overview)) == expected
    assert _banner(_parse(pages.finder)) == expected
    own = pct(named, sum(row.total_tokens for row in rows))
    assert _banner(_session_page(pages, mixed)) == expected.replace(share, own)
    assert _banner(_session_page(pages, clean)) is None


def test_banner_boundaries() -> None:
    ref = "vsid-banner"
    assert unpriced_banner([]) == ""
    assert unpriced_banner([_request(ref, 1, _OPUS)]) == ""
    # A request with no tokens costs nothing, and a non-model is no model.
    assert unpriced_banner([_request(ref, 1, _STRANGER, tokens=0)]) == ""
    assert unpriced_banner([_request(ref, 1, None), _request(ref, 2, "<synthetic>")]) == ""
    one = unpriced_banner([_request(ref, 1, _STRANGER)])
    assert f"Unpriced: {_STRANGER} (1 request), 100.0% of tokens." in one


def test_banner_breaks_ties_by_model_name() -> None:
    ref = "vsid-tie"
    rows = [_request(ref, 1, "zeta-model"), _request(ref, 2, "alpha-model")]
    assert "Unpriced: alpha-model (1 request), zeta-model (1 request)," in unpriced_banner(rows)


# ---- the API-equivalent hero, its date, the switch and the cache-read copy --------------


def _hero(pages: WrittenPages) -> _Node:
    return _where(_parse(pages.overview), "data-section", "hero")[0]


def _checked(hero: _Node) -> list[str]:
    return [
        node.attrs["id"] for node in hero.walk() if node.tag == "input" and "checked" in node.attrs
    ]


def _book_with(tmp_path: Path, dates: dict[str, str]) -> PriceBook:
    """A price file of Opus 5.5 and Haiku 4.5 rows verified on the given dates."""
    rates = {
        _OPUS: {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": {"5m": 5, "1h": 8}},
        "claude-haiku-4-5": {
            "input": 1,
            "output": 5,
            "cache_read": 0.1,
            "cache_write": {"5m": 1.25, "1h": 2},
        },
    }
    models = [
        {
            "id": model,
            "match": [model],
            "effective_from": None,
            "effective_to": None,
            "source_url": "https://example.invalid/pricing",
            "verified_on": dates[model],
            "verified_by": "fetch",
            "rates": rates[model],
            "tiers": [],
            "modifiers": {"fast": None, "geo_us": 1.1},
        }
        for model in rates
    ]
    directory = tmp_path / "prices"
    directory.mkdir()
    document = {"vendor": "anthropic", "aliases": [], "models": models}
    (directory / "anthropic.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_price_book(repo_root=None, directory=directory, overrides={})


def test_headline_and_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = "vsid-head"
    rows = [_request(ref, 1, _OPUS, read=8_000), _request(ref, 2, _OPUS, tokens=3_000)]
    total = sum((row.cost_usd or Decimal(0) for row in rows), Decimal(0))

    hero = _hero(_pages(tmp_path / "default", {ref: rows}))

    text = hero.all_text()
    assert f"{usd(total)} API-equivalent · list prices verified 2026-11-10" in text
    assert _checked(hero) == ["headline-usd"]
    assert "Cache reads bill at 5% of base input" in text
    assert "a tenth" not in text

    tokens = _hero(_pages(tmp_path / "tokens", {ref: rows}, headline="tokens"))
    assert _checked(tokens) == ["headline-tokens"]
    assert tokens.attrs.get("data-section") == "hero"
    assert any(node.attrs.get("data-headline") == "tokens" for node in tokens.walk())

    # Fable 5.2 reads at 2.5% of input and Haiku 4.5 at 10%.
    spread = {
        "vsid-fable": [_request("vsid-fable", 1, "claude-fable-5-2", read=1_000)],
        "vsid-haiku": [_request("vsid-haiku", 1, "claude-haiku-4-5", read=1_000)],
    }
    mixed = _hero(_pages(tmp_path / "mixed", spread)).all_text()
    assert "Cache reads bill at 2.5% to 10% of base input, by model" in mixed

    # The date is the oldest verified_on among the rows the priced requests used.
    book = _book_with(tmp_path, {_OPUS: "2026-11-10", "claude-haiku-4-5": "2026-10-01"})
    monkeypatch.setattr(pages_module, "active_price_book", lambda: book)
    both = {
        "vsid-o": [_request("vsid-o", 1, _OPUS, book=book)],
        "vsid-h": [_request("vsid-h", 1, "claude-haiku-4-5", book=book)],
    }
    assert "list prices verified 2026-10-01" in _hero(_pages(tmp_path / "old", both)).all_text()
    only_opus = {"vsid-o": both["vsid-o"]}
    dated = _hero(_pages(tmp_path / "new", only_opus)).all_text()
    assert "list prices verified 2026-11-10" in dated


def test_headline_setting_reads_the_layered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)

    assert reflect_headline(None) == "api_equivalent"
    assert reflect_headline(repo) == "api_equivalent"

    (repo / ".ea" / "config.yaml").write_text("reflect:\n  headline: tokens\n", encoding="utf-8")
    assert reflect_headline(repo) == "tokens"

    (repo / ".ea" / "config.yaml").write_text("reflect:\n  headline: euros\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        reflect_headline(repo)


def test_headline_with_nothing_priced_says_so(tmp_path: Path) -> None:
    ref = "vsid-none"
    hero = _hero(_pages(tmp_path, {ref: [_request(ref, 1, _STRANGER)]}))
    assert "not priced" in hero.all_text()
    assert "API-equivalent ·" not in hero.all_text()


# ---- the strip prices writes by class and sums to the hero -----------------------------


def test_strip_total(tmp_path: Path) -> None:
    # strip_total: Opus 5.5 requests writing at 5 minutes, at 1 hour and at both.
    ref = "vsid-strip"
    rows = [
        _request(ref, 1, _OPUS, write_5m=200_000, read=50_000),
        _request(ref, 2, _OPUS, write_1h=300_000, read=20_000),
        _request(ref, 3, _OPUS, write_5m=40_000, write_1h=60_000),
    ]
    hero_total = sum((row.cost_usd or Decimal(0) for row in rows), Decimal(0))

    pages = _pages(tmp_path, {ref: rows})

    hero = _hero(pages)
    cells = {node.attrs["data-cls"]: node for node in hero.walk() if "data-cls" in node.attrs}
    strip = {key: Decimal(node.attrs["data-usd"]) for key, node in cells.items()}
    assert float(sum(strip.values(), Decimal(0))) == pytest.approx(float(hero_total))
    assert f"{usd(hero_total)} API-equivalent" in hero.all_text()
    writes = 240_000 * 5 + 360_000 * 8
    assert float(strip["cache-writes"]) == pytest.approx(writes / 1_000_000)
    assert "5-minute and 1-hour writes at their own rates" in cells["cache-writes"].attrs["title"]

    rates = _where(_parse(pages.overview), "data-section", "rates")[0]
    heads = [node.all_text() for node in rates.walk() if node.tag == "th"]
    assert "$/1M 5-minute write" in heads and "$/1M 1-hour write" in heads
    assert "at the 5-minute rate" not in rates.all_text()
    opus_row = next(node for node in rates.walk() if node.tag == "tr" and _OPUS in node.all_text())
    assert re.search(r"\$5\.00.*\$8\.00", opus_row.all_text())
