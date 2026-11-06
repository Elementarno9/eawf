"""The reflect overview, finder and session pages, parsed from the files written."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.reflect.analysis import (
    HistoryAnalysis,
    HistorySession,
    Metric,
    analyze_sessions,
)
from eawf.observability.reflect.generations import SessionContext, session_context
from eawf.observability.reflect.html import PageSources, WrittenPages, write_pages
from eawf.observability.reflect.rates import PricingQuality
from eawf.observability.telemetry.models import DurationKind, ObservedRequest, PriceSourceKind
from eawf.observability.telemetry.sources.session_history import (
    BlockKind,
    CompactionBoundary,
    ContentBlock,
    SessionDetail,
    ToolSpan,
)

_T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 1, tzinfo=UTC)
_MODEL = "claude-opus-4-7"
_VOID = frozenset({"meta", "link", "br", "hr", "img", "input", "col", "source", "wbr"})
_URL = re.compile(r"^\s*https?:", re.IGNORECASE)


# ---- a minimal element tree -----------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def all_text(self) -> str:
        return "".join([*self.text, *(child.all_text() for child in self.children)])

    def walk(self) -> list[_Node]:
        found = [self]
        for child in self.children:
            found.extend(child.walk())
        return found

    def find(self, *, tag: str | None = None, cls: str | None = None) -> list[_Node]:
        return [
            node
            for node in self.walk()[1:]
            if (tag is None or node.tag == tag)
            and (cls is None or cls in node.attrs.get("class", "").split())
        ]


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs})
        self.count += 1
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].text.append(data)


def _parse(path: Path) -> _Tree:
    tree = _Tree()
    tree.feed(path.read_text(encoding="utf-8"))
    tree.close()
    return tree


def _section(tree: _Tree, name: str) -> _Node:
    found = [node for node in tree.root.walk() if node.attrs.get("data-section") == name]
    assert len(found) == 1, f"section {name}: {len(found)}"
    return found[0]


def _sections(tree: _Tree) -> list[str]:
    return [node.attrs["data-section"] for node in tree.root.walk() if "data-section" in node.attrs]


# ---- a synthetic history --------------------------------------------------------------


def _ref(index: int) -> str:
    return f"vsid-{index:032x}"


def _requests(index: int, count: int, start: datetime) -> list[ObservedRequest]:
    return [
        ObservedRequest(
            vendor_session_ref=_ref(index),
            runtime="claude",
            request_ref=f"req-{index:016x}{ordinal:016x}",
            ordinal=ordinal,
            ts=start + timedelta(seconds=30 * ordinal),
            model=_MODEL,
            input_tokens=100 + ordinal,
            output_tokens=50,
            cache_read_tokens=1_000,
            cache_write_tokens=200,
            reasoning_tokens=10 if ordinal % 2 else None,
            total_tokens=1_350 + ordinal,
            cost_usd=Decimal("0.01") * ordinal,
            price_source=PriceSourceKind.LIST_RECONSTRUCTED,
            rate_table_version="synthetic",
        )
        for ordinal in range(1, count + 1)
    ]


def _block(
    ordinal: int, kind: BlockKind, request: int, start: datetime, chars: int
) -> ContentBlock:
    return ContentBlock(
        ordinal=ordinal,
        kind=kind,
        lane="main",
        request_ordinal=request,
        ts=start + timedelta(seconds=30 * request + 1),
        size_chars=chars,
    )


def _detail(index: int, start: datetime) -> SessionDetail:
    """Twelve requests, ten reads, two subagents and one compaction after request six."""
    kinds: list[tuple[BlockKind, int, int]] = [("user", 0, 400)]
    for request in range(1, 7):
        kinds += [("assistant", request, 200), ("tool_call", request, 120)]
        kinds += [("tool_result", request, 8_000)]
    cut = len(kinds)
    kinds += [("compaction_summary", 6, 4_000), ("user", 6, 300)]
    for request in range(7, 13):
        kinds += [("assistant", request, 200), ("tool_result", request, 3_000)]
    blocks = tuple(
        _block(ordinal, kind, request, start, chars)
        for ordinal, (kind, request, chars) in enumerate(kinds, start=1)
    )
    reads = tuple(
        ToolSpan(
            label="Read",
            started_at=start + timedelta(seconds=30 * step + 5),
            ended_at=start + timedelta(seconds=30 * step + 15),
            duration_ms=10_000,
        )
        for step in range(1, 11)
    )
    agents = (
        ToolSpan(
            label="Explore",
            started_at=start + timedelta(seconds=60),
            ended_at=start + timedelta(seconds=200),
            duration_ms=140_000,
        ),
        ToolSpan(
            label="general-purpose",
            started_at=start + timedelta(seconds=120),
            ended_at=start + timedelta(seconds=300),
            duration_ms=180_000,
        ),
    )
    return SessionDetail(
        vendor_session_ref=_ref(index),
        runtime="claude",
        cwd_digest=None,
        steps=tuple(_requests(index, 12, start)),
        tool_spans={DurationKind.TOOL_CALL: reads, DurationKind.SUBAGENT: agents},
        blocks=blocks,
        compactions=(
            CompactionBoundary(
                block_ordinal=cut,
                request_ordinal=6,
                ts=start + timedelta(seconds=190),
                trigger="auto",
                pre_tokens=40_000,
            ),
        ),
    )


def _session(index: int, *, compactions: float, context: SessionContext | None) -> HistorySession:
    started = _T0 + timedelta(days=index)
    return HistorySession(
        session_ref=_ref(index),
        runtime="claude",
        project_key="cwd-a",
        project_label="SYN",
        started_at=started,
        ended_at=started + timedelta(hours=1),
        model=_MODEL,
        model_family="claude-opus",
        orchestrated=False,
        metrics={
            Metric.TOTAL_TOKENS: 10_000.0 * (index + 1),
            Metric.FRESH_INPUT_TOKENS: 1_000.0,
            Metric.OUTPUT_TOKENS: 500.0,
            Metric.REASONING_TOKENS: 50.0,
            Metric.CACHE_READ_TOKENS: 8_000.0,
            Metric.CACHE_WRITE_TOKENS: 500.0,
            Metric.MODEL_STEPS: 10.0,
            Metric.TOOL_CALLS: 5.0,
            Metric.COMPACTION_COUNT: compactions,
            Metric.CALENDAR_MS: 3_600_000.0,
        },
        cost_usd=Decimal(index + 1),
        pricing_quality=PricingQuality.VERIFIED,
        context=context,
    )


@dataclass(frozen=True)
class _Corpus:
    analysis: HistoryAnalysis
    sources: PageSources
    detailed: str


def _corpus(count: int, *, flagged: range = range(0)) -> _Corpus:
    """``count`` sessions, ``flagged`` ones compacting twice, session 0 read in full."""
    detail = _detail(0, _T0)
    context = session_context(detail)
    history = [
        _session(
            index,
            compactions=1.0 if index == 0 else 2.0 if index in flagged else 0.0,
            context=context if index == 0 else None,
        )
        for index in range(count)
    ]
    requests = [
        row
        for index in range(count)
        for row in (
            list(detail.steps) if index == 0 else _requests(index, 3, _T0 + timedelta(days=index))
        )
    ]
    analysis = analyze_sessions(history, requests=requests, now=_NOW)
    sources = PageSources(
        analysis=analysis,
        session_refs=frozenset(_ref(index) for index in range(count)),
        requests=tuple(requests),
        details={_ref(0): detail} if count else {},
        contexts={_ref(0): context} if count else {},
        history_files=count + 2,
        scope_label="SYN",
    )
    return _Corpus(analysis=analysis, sources=sources, detailed=_ref(0))


def _key(ref: str) -> str:
    return ref.removeprefix("vsid-")


@pytest.fixture
def written(tmp_path: Path) -> tuple[_Corpus, WrittenPages]:
    corpus = _corpus(45, flagged=range(1, 13))
    return corpus, write_pages(tmp_path / "pages", corpus.sources)


# ---- CR-745: the overview -------------------------------------------------------------


def test_overview(written: tuple[_Corpus, WrittenPages]) -> None:
    pages = written[1]
    tree = _parse(pages.overview)

    assert {"hero", "distribution", "must-see", "findings", "coverage", "rates"} <= set(
        _sections(tree)
    )
    hero = _section(tree, "hero")
    assert "Effective tokens" in hero.all_text()
    cells = [node.attrs["data-cls"] for node in hero.walk() if "data-cls" in node.attrs]
    assert cells == ["fresh-input", "output", "cache-writes", "cache-reads"]
    output = next(node for node in hero.walk() if node.attrs.get("data-cls") == "output")
    assert "reasoning" in output.all_text()

    distribution = _section(tree, "distribution")
    marks = {
        mark
        for node in distribution.find(cls="dist-mark")
        for mark in ("p50", "p95", "max")
        if mark in node.attrs["class"].split()
    }
    assert marks == {"p50", "p95", "max"}
    assert distribution.find(cls="dist-dot")

    ranked = _section(tree, "must-see").find(tag="a")
    # Flagged sessions 1..12 cost index + 1 dollars, so the ten dearest are 12 down to 3;
    # the unflagged sessions cost more and are never listed.
    assert [node.attrs["href"] for node in ranked] == [
        f"session-{_key(_ref(index))}.html" for index in range(12, 2, -1)
    ]

    findings = _section(tree, "findings").find(tag="article")
    assert any("compact" in node.all_text().lower() for node in findings)

    stages = _section(tree, "coverage").find(cls="funnel")[0].find(tag="div")
    assert [node.find(tag="dt")[0].all_text() for node in stages] == [
        "Sources seen",
        "Parsed",
        "In scope",
        "Eligible",
        "Priced",
    ]
    assert all(node.find(tag="small")[0].all_text().strip() for node in stages[1:])

    rates = _section(tree, "rates")
    assert _MODEL in rates.all_text()


def test_overview_with_no_sessions_says_nothing_was_priced(tmp_path: Path) -> None:
    corpus = _corpus(0)

    pages = write_pages(tmp_path / "pages", corpus.sources)

    tree = _parse(pages.overview)
    assert "not priced" in _section(tree, "hero").all_text()
    assert not _section(tree, "must-see").find(tag="a")
    assert pages.sessions == ()


def test_overview_with_one_session_draws_one_dot(tmp_path: Path) -> None:
    pages = write_pages(tmp_path / "pages", _corpus(1).sources)

    tree = _parse(pages.overview)
    assert len(_section(tree, "distribution").find(cls="dist-dot")) == 1
    assert len(pages.sessions) == 1


def test_write_pages_refuses_a_file_as_its_directory(tmp_path: Path) -> None:
    target = tmp_path / "pages"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError):
        write_pages(target, _corpus(1).sources)


# ---- CR-746: the finder and the session page ------------------------------------------


def _finder_data(tree: _Tree) -> dict[str, Any]:
    node = next(node for node in tree.root.walk() if node.attrs.get("id") == "finder-data")
    data: dict[str, Any] = json.loads(node.all_text())
    return data


def test_finder_and_session(written: tuple[_Corpus, WrittenPages]) -> None:
    corpus, pages = written
    finder = _parse(pages.finder)

    table = _section(finder, "finder")
    assert len(table.find(tag="tbody")[0].find(tag="tr")) == 40
    data = _finder_data(finder)
    assert data["page"] == 40
    assert len(data["rows"]) == 45
    overview = _parse(pages.overview)
    must_see = [node.attrs["href"] for node in _section(overview, "must-see").find(tag="a")]
    flagged = [f"session-{row['key']}.html" for row in data["rows"] if row["must"]]
    assert sorted(flagged) == sorted(must_see)
    toggle = next(node for node in table.walk() if node.attrs.get("data-filter") == "must")
    assert toggle.attrs["aria-pressed"] == "false"

    page = _parse(pages.overview.parent / f"session-{_key(corpus.detailed)}.html")
    assert {"timeline", "compaction", "generations", "requests"} <= set(_sections(page))
    lanes = _section(page, "timeline").find(cls="tl-row")
    assert len(lanes) == 3
    assert "main" in lanes[0].all_text().lower()
    assert _section(page, "timeline").find(cls="tl-mark")
    sankey = _section(page, "compaction")
    assert sankey.find(tag="svg")
    assert sankey.find(cls="flow")
    labels = sankey.all_text().lower()
    assert "carried" in labels
    assert "dropped" in labels
    strips = _section(page, "generations").find(cls="strip")
    assert len(strips) == 2
    assert all(len(strip.find(tag="button")) <= 60 for strip in strips)
    requests = _section(page, "requests").find(tag="tbody")
    assert requests
    assert _MODEL in requests[0].all_text()


@pytest.mark.parametrize(("count", "rows", "more"), [(40, 40, False), (41, 40, True)])
def test_finder_pages_at_forty_rows(tmp_path: Path, count: int, rows: int, more: bool) -> None:
    pages = write_pages(tmp_path / "pages", _corpus(count).sources)

    table = _section(_parse(pages.finder), "finder")
    assert len(table.find(tag="tbody")[0].find(tag="tr")) == rows
    following = next(node for node in table.walk() if node.attrs.get("data-page") == "next")
    assert ("disabled" not in following.attrs) is more


def test_finder_with_no_sessions_offers_a_reset(tmp_path: Path) -> None:
    pages = write_pages(tmp_path / "pages", _corpus(0).sources)

    table = _section(_parse(pages.finder), "finder")
    assert "No session" in table.find(tag="tbody")[0].all_text()
    assert any(node.attrs.get("data-filter") == "reset" for node in table.walk())


def test_session_without_detail_says_what_was_not_read(
    written: tuple[_Corpus, WrittenPages],
) -> None:
    pages = written[1]
    page = _parse(pages.overview.parent / f"session-{_key(_ref(5))}.html")

    assert {"timeline", "compaction", "generations", "requests"} <= set(_sections(page))
    assert "not read" in _section(page, "timeline").all_text()
    assert not _section(page, "generations").find(cls="strip")
    assert _section(page, "requests").find(tag="tbody")


# ---- CR-747: offline and colour scheme ------------------------------------------------


def test_offline_and_color_scheme(written: tuple[_Corpus, WrittenPages]) -> None:
    pages = written[1]
    written_pages = sorted(pages.overview.parent.glob("*.html"))
    assert len(written_pages) == 2 + 45

    for path in written_pages:
        tree = _parse(path)
        for node in tree.root.walk():
            for name in ("src", "href", "action", "poster"):
                assert not _URL.match(node.attrs.get(name, "")), f"{path.name}: {node.attrs}"
            assert not (node.tag == "script" and "src" in node.attrs), path.name
            assert node.tag != "link", path.name
        styles = [node.all_text() for node in tree.root.find(tag="style")]
        assert len(styles) == 1, path.name
        css = styles[0]
        assert "@import" not in css
        assert not re.search(r"url\(\s*['\"]?\s*https?:", css, re.IGNORECASE)
        assert re.search(r"@media\s*\(prefers-color-scheme:\s*light\)", css)
        assert css.count("--void:") == 2


def test_session_page_shows_unrecorded_effort_as_absent(
    written: tuple[_Corpus, WrittenPages],
) -> None:
    corpus, pages = written
    page = _parse(pages.overview.parent / f"session-{_key(corpus.detailed)}.html")

    assert "unknown" not in _section(page, "identity").all_text()
    ledger = _section(page, "requests").find(tag="tbody")[0].find(tag="tr")[0]
    effort = ledger.find(tag="td")[1]
    assert effort.all_text() == chr(0x2013)
    assert effort.find(cls="unk")
