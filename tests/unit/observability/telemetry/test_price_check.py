"""The online price check compares the price files with fixture vendor pages, never the network.

The fixtures are trimmed captures of the vendor pages and the OpenAI price
script. The check calls the file current only when it compared every priced
class of every row; anything it could not compare ends it with status 2.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
import pytest
import typer
import yaml
from typer.testing import CliRunner  # noqa: EAWF024 the exit status is the contract

from eawf.observability.telemetry import price_check
from eawf.observability.telemetry.price_check import (
    PageParseError,
    PageRow,
    PriceCheckReport,
    PriceDifference,
    UnreadablePage,
    check_prices_online,
    fetch_page,
    read_anthropic_page,
    read_openai_long_context,
    read_openai_page,
    render_price_check,
)
from eawf.observability.telemetry.pricing import PRICE_FILE_DIR
from eawf.surfaces.cli.app import app

_REPO = Path(__file__).resolve().parents[4]
_PAGES = _REPO / "tests" / "fixtures" / "pricing_pages"
_WORKFLOWS = _REPO / ".github" / "workflows"
_WORKFLOW = _WORKFLOWS / "price-check.yml"
_ISSUE_TITLE = "Price file differs from vendor pages"
_ANTHROPIC_URL = "https://platform.claude.com/docs/en/about-claude/pricing"


def _page(name: str) -> str:
    return (_PAGES / name).read_text(encoding="utf-8")


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    anthropic: str | None = None,
    openai: str | None = None,
    script: str | None = None,
) -> list[str]:
    """Answer each vendor URL with a fixture; ``None`` keeps the shipped fixture."""
    pages = {
        "/_astro/pricing": _page("openai_pricing.js") if script is None else script,
        "claude.com": _page("anthropic.html") if anthropic is None else anthropic,
        "openai.com": _page("openai.html") if openai is None else openai,
    }
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return next(page for key, page in pages.items() if key in url)

    monkeypatch.setattr(price_check, "fetch_page", fetch)
    return fetched


def _price_file_digest() -> str:
    hasher = hashlib.sha256()
    for path in sorted(PRICE_FILE_DIR.glob("*")):
        hasher.update(path.name.encode())
        hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _run_check(*extra: str) -> click.testing.Result:
    return CliRunner().invoke(app, ["reflect", "prices", "check", *extra])


def test_exit_codes_against_fixture_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _price_file_digest()

    _serve(monkeypatch)
    equal = _run_check("--online")
    assert equal.exit_code == 0, equal.output
    assert "matches the vendor pages (27 rows, " in equal.output

    # One changed rate: gpt-6-astra's standard output rate.
    astra = "[0,&quot;gpt-6-astra&quot;],[0,10],[0,1],[0,12.5],[0,50]"
    assert _page("openai.html").count(astra) == 1
    changed = _page("openai.html").replace(astra, astra.replace("[0,50]", "[0,55]"))
    _serve(monkeypatch, openai=changed)
    differs = _run_check("--online")
    assert differs.exit_code == 1, differs.output
    assert "gpt-6-astra output: file 50, page 55" in differs.output

    def refuse(url: str) -> str:
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(price_check, "fetch_page", refuse)
    unreadable = _run_check("--online")
    assert unreadable.exit_code == 2, unreadable.output
    assert "fetch failed" in unreadable.output

    _serve(monkeypatch, anthropic="<html><body>moved</body></html>")
    no_table = _run_check("--online")
    assert no_table.exit_code == 2, no_table.output
    assert "no price table names a row of the file" in no_table.output

    assert _price_file_digest() == before


def test_shipped_files_match_the_shipped_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch)
    report = check_prices_online()

    assert report.differences == ()
    assert report.unchecked == () and report.unreadable == ()
    assert report.checked_rows == 27
    assert report.exit_status == 0


def test_every_priced_class_is_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch)
    report = check_prices_online()

    assert report.exit_status == 0
    # gpt-5.5's tier and the OpenAI 12h writes are among the compared classes.
    _serve(
        monkeypatch,
        script=_page("openai_pricing.js").replace("input:10,output:45", "input:11,output:45"),
    )
    fields = {(d.model_id, d.field) for d in check_prices_online().differences}
    assert fields == {("gpt-5.5", "tiers[0].input")}
    assert report.checked_classes > report.checked_rows * 4


# ---- repro: rows, tiers and columns the check could not read ---------------------------

_OLD_TABLE_PAGE = (
    "<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached input</th>"
    "<th>30m write</th><th>12h write</th></tr>"
    "<tr><td>gpt-6-astra</td><td>$10.00</td><td>$50.00</td><td>$1.00</td>"
    "<td>$12.50</td><td>$25.00</td></tr>"
    "<tr><td>gpt-6.1-sol</td><td>$2.00</td><td>$10.00</td><td>$0.10</td>"
    "<td>$2.50</td><td>$5.00</td></tr>"
    "<tr><td>gpt-6-luna</td><td>$0.10</td><td>$0.50</td><td>$0.01</td>"
    "<td>$0.125</td><td>$0.25</td></tr></table>"
)


def test_price_check_repro_rows_not_on_the_page_are_not_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collapsed table lists three rows of six and no tier: that is not a match."""
    _serve(monkeypatch, openai=_OLD_TABLE_PAGE)
    report = check_prices_online()

    assert report.exit_status == 2
    assert "matches" not in render_price_check(report)


def test_price_check_repro_renamed_column_is_not_current(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page("anthropic.html")
    assert page.count("<button>5m writes</button>") == 1
    _serve(monkeypatch, anthropic=page.replace("5m writes", "5m cache writes"))
    report = check_prices_online()

    assert report.unreadable == ()
    assert report.exit_status == 2
    assert any(
        line.startswith("claude-opus-5-5: the page states no cache_write.5m")
        for line in report.unchecked
    )


def test_a_row_missing_from_the_page_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    gpt_5 = "[1,[[0,&quot;gpt-5&quot;],[0,1.25],[0,0.125],[0,10]]],"
    assert _page("openai.html").count(gpt_5) == 1
    _serve(monkeypatch, openai=_page("openai.html").replace(gpt_5, ""))
    report = check_prices_online()

    assert report.exit_status == 2
    assert "gpt-5: not on the page" in report.unchecked


def test_a_dropped_tier_is_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    luna = '"gpt-6-luna":{input:.2,output:.75,cachedInput:.02,cacheWrite:.25},'
    assert _page("openai_pricing.js").count(luna) == 1
    _serve(monkeypatch, script=_page("openai_pricing.js").replace(luna, ""))
    report = check_prices_online()

    assert report.exit_status == 2
    assert any(
        line.startswith("gpt-6-luna: the page states no larger-prompt tier: tiers[0].")
        for line in report.unchecked
    )


def test_an_unreadable_script_leaves_every_tier_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, script="export{}")
    report = check_prices_online()

    assert report.exit_status == 2
    tiered = [line for line in report.unchecked if "could not read the larger-prompt" in line]
    assert {line.split(":")[0] for line in tiered} == {
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-5.5",
    }


def test_a_script_on_another_site_is_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page("openai.html").replace(
        'component-url="/_astro/pricing', 'component-url="https://elsewhere.invalid/_astro/pricing'
    )
    fetched = _serve(monkeypatch, openai=page)
    report = check_prices_online()

    assert report.exit_status == 2
    assert not any("elsewhere" in url for url in fetched)
    assert any("another site" in line for line in report.unchecked)


def test_price_check_repro_base_rates_compared_beside_an_unlisted_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page tier the file lacks is a difference, and the base rates are still compared."""
    page = _page("anthropic.html")
    opus = '<a href="/docs/en/models/opus-5-5/overview">Claude Opus 5.5</a>'
    assert page.count(opus) == 1
    row_end = page.index("</tr>", page.index(opus)) + len("</tr>")
    head, tail = page[:row_end], page[row_end:]
    head = head.replace("<td><div>$4<!-- -->", "<td><div>$4.50<!-- -->", 1)
    tier = "<tr>" + "<td><div>$9</div></td>" * 5 + "</tr>"
    _serve(monkeypatch, anthropic=head + tier + tail)
    fields = {(d.model_id, d.field) for d in check_prices_online().differences}

    assert ("claude-opus-5-5", "tiers[0].input") in fields
    assert ("claude-opus-5-5", "input") in fields


def test_a_tier_the_page_states_is_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    # Haiku 5.5's larger-prompt input rate sits on the row after its own.
    tier_input = "<tr><td><div>$0.50<!-- -->"
    page = _page("anthropic.html")
    assert page.count(tier_input) == 1
    _serve(monkeypatch, anthropic=page.replace(tier_input, "<tr><td><div>$0.55<!-- -->"))
    fields = {(d.model_id, d.field) for d in check_prices_online().differences}
    assert ("claude-haiku-5-5", "tiers[0].input") in fields


def test_fast_mode_and_region_are_compared(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _page("anthropic.html").replace("$8 USD / MTok", "$9 USD / MTok")
    page = page.replace("applies a 1.1x pricing", "applies a 1.2x pricing")
    _serve(monkeypatch, anthropic=page)
    report = check_prices_online()

    assert ("claude-opus-5-5", "modifiers.fast.input") in {
        (d.model_id, d.field) for d in report.differences
    }
    # Two sentences now disagree, so the multiplier is unread, never guessed.
    assert any("could not read the US-region multiplier" in line for line in report.unchecked)
    assert report.exit_status == 2


# ---- the report ------------------------------------------------------------------------


def _report(**fields: Any) -> PriceCheckReport:
    base: dict[str, Any] = {
        "checked_rows": 1,
        "checked_classes": 5,
        "differences": (),
        "unchecked": (),
        "unreadable": (),
    }
    return PriceCheckReport(**(base | fields))


def test_unreadable_page_or_unchecked_class_outranks_a_difference() -> None:
    difference = PriceDifference(
        model_id="m", field="input", file=Decimal(1), page=Decimal(2), source_url="https://a"
    )
    page = UnreadablePage(source_url="https://b", reason="fetch failed: 403")

    assert _report().exit_status == 0
    assert _report(differences=(difference,)).exit_status == 1
    assert _report(unreadable=(page,)).exit_status == 2
    assert _report(unchecked=("m: not on the page",)).exit_status == 2
    assert _report(differences=(difference,), unreadable=(page,)).exit_status == 2
    assert render_price_check(_report(differences=(difference,))).splitlines()[1] == (
        "- m input: file 1, page 2 (https://a)"
    )
    assert "could not check" in render_price_check(_report(unreadable=(page,)))
    assert "could not check" in render_price_check(_report(unchecked=("m: x",)))
    assert render_price_check(_report()).startswith(
        "Prices: the file matches the vendor pages (1 rows, 5 classes compared)."
    )


def test_render_names_a_class_the_page_does_not_state() -> None:
    difference = PriceDifference(
        model_id="m", field="cache_write.1h", file=Decimal(2), page=None, source_url="https://a"
    )
    report = _report(differences=(difference,), unchecked=("x: not on the page",))
    lines = render_price_check(report).splitlines()
    assert lines[1] == "- m cache_write.1h: file 2, page none (https://a)"
    assert lines[2] == "- unchecked: x: not on the page"


def test_report_refuses_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="extra"):
        PriceCheckReport.model_validate(
            {
                "checked_rows": 0,
                "checked_classes": 0,
                "differences": [],
                "unchecked": [],
                "unreadable": [],
                "x": 1,
            }
        )


# ---- the Anthropic page ----------------------------------------------------------------


def read_price_page(html: str) -> dict[str, PageRow]:
    """Return the price-table rows :func:`read_anthropic_page` reads from *html*."""
    return dict(read_anthropic_page(html).rows)


def test_read_price_page_on_an_empty_page_finds_nothing() -> None:
    assert read_price_page("") == {}


def test_read_price_page_skips_a_table_with_no_write_column() -> None:
    # The fast-mode table lists Fable 5.2 at $16; the base table's $8 must win.
    found = read_price_page(_page("anthropic.html"))
    assert found["claude-fable-5-2"].rates["input"] == Decimal(8)
    only_fast = (
        "<table><tr><th>Model</th><th>Input</th></tr>"
        "<tr><td>Claude Opus 5.5</td><td>$8</td></tr></table>"
    )
    assert read_price_page(only_fast) == {}


def test_read_price_page_single_row_and_dash() -> None:
    html = (
        "<table><tr><th>Model</th><th>Input</th><th>Output</th><th>5m writes</th></tr>"
        "<tr><td>Claude Opus 9</td><td>$1</td><td>-</td><td>$1,000.5</td></tr></table>"
    )
    found = read_price_page(html)
    assert list(found) == ["claude-opus-9"]
    assert found["claude-opus-9"].rates == {
        "input": Decimal(1),
        "output": None,
        "cache_write.5m": Decimal("1000.5"),
    }


def test_read_price_page_off_by_one_rows() -> None:
    header = "<tr><th>Name</th><th>Input</th><th>5m writes</th></tr>"
    model = "<tr><td>Claude Opus 9</td><td>$1</td><td>$1.25</td></tr>"
    tier = "<tr><td>$2</td><td>$2.50</td></tr>"
    short = "<tr><td>$3</td></tr>"
    found = read_price_page(f"<table>{header}{model}{tier}</table>")
    assert found["claude-opus-9"].tier == {"input": Decimal(2), "cache_write.5m": Decimal("2.50")}
    # Two cells short is not a continuation row.
    found = read_price_page(f"<table>{header}{model}{short}</table>")
    assert found["claude-opus-9"].tier is None
    # A continuation row with no model row above it is ignored.
    assert read_price_page(f"<table>{header}{tier}</table>") == {}


def test_read_anthropic_page_reads_fast_mode_for_each_named_model() -> None:
    page = read_anthropic_page(_page("anthropic.html"))

    assert page.misses == {}
    assert page.geo_us == Decimal("1.1")
    for model in ("claude-opus-5", "claude-opus-4-8"):
        assert page.rows[model].fast == {"input": Decimal(10), "output": Decimal(50)}
    assert page.rows["claude-sonnet-5-5"].fast is None


def test_read_anthropic_page_names_what_it_could_not_read() -> None:
    page = read_anthropic_page(_page("anthropic.html").replace("Fast mode pricing", "Speed"))
    assert "fast" in page.misses
    assert all(row.fast is None for row in page.rows.values())

    bare = read_anthropic_page("<p>no prices</p>")
    assert bare.rows == {} and bare.geo_us is None
    assert set(bare.misses) == {"fast", "geo_us"}


# ---- the OpenAI page and script --------------------------------------------------------


def test_read_openai_page_reads_the_standard_tier_only() -> None:
    rows, script = read_openai_page(_page("openai.html"))

    assert script == "/_astro/pricing.DepN27NN.js"
    assert rows["gpt-6-astra"].rates == {
        "input": Decimal(10),
        "cache_read": Decimal(1),
        "cache_write.30m": Decimal("12.5"),
        "output": Decimal(50),
        "cache_write.12h": Decimal("25.0"),
    }
    # A row with no write column offers no write; "-" offers none either.
    assert rows["gpt-5"].rates["cache_write.30m"] is None
    assert rows["gpt-5"].rates["cache_write.12h"] is None
    assert rows["gpt-5.5"].rates["cache_write.30m"] is None
    assert rows["gpt-5-pro"].rates["cache_read"] is None


def _island(tier: str, rows: str, script: str = "/_astro/pricing.x.js") -> str:
    props = f'{{"tier":[0,"{tier}"],"rows":[1,[{rows}]]}}'.replace('"', "&quot;")
    return f'<astro-island component-url="{script}" props="{props}"></astro-island>'


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("", "found 0 standard-tier"),
        (_island("standard", "") + _island("standard", ""), "found 2 standard-tier"),
        (_island("standard", '[1,[[0,"gpt-x"],[0,1]]]'), "unknown shape"),
        (_island("standard", '[1,[[0,"gpt-x"],[0,"$1"],[0,1],[0,1]]]'), "unknown rate"),
        (_island("standard", '[1,[[0,"GPT X"],[0,1],[0,1],[0,1]]]'), "names no model"),
        (_island("standard", "[2,[]]"), "unknown prop encoding"),
        (_island("standard", "", script=""), "names no script"),
    ],
)
def test_read_openai_page_refuses_what_it_cannot_read(html: str, message: str) -> None:
    with pytest.raises(PageParseError, match=message):
        read_openai_page(html)


def test_read_openai_page_single_row_and_other_tiers() -> None:
    html = _island("batch", '[1,[[0,"gpt-x"],[0,9],[0,9],[0,9]]]') + _island(
        "standard", '[1,[[0,"gpt-x (<272K context length)"],[0,1],[0,null],[0,"-"],[0,2]]]'
    )
    rows, _ = read_openai_page(html)
    assert list(rows) == ["gpt-x"]
    assert rows["gpt-x"].rates["input"] == Decimal(1)
    assert rows["gpt-x"].rates["cache_read"] is None


def test_read_openai_long_context_reads_the_standard_map() -> None:
    tiers = read_openai_long_context(_page("openai_pricing.js"))

    assert set(tiers) == {"gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.5"}
    assert tiers["gpt-6-luna"] == {
        "input": Decimal("0.2"),
        "output": Decimal("0.75"),
        "cache_read": Decimal("0.02"),
        "cache_write.30m": Decimal("0.25"),
        "cache_write.12h": Decimal("0.50"),
    }
    assert tiers["gpt-5.5"]["cache_write.30m"] is None
    assert tiers["gpt-5.5"]["cache_write.12h"] is None


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("", "found 0 standard"),
        ('standard:{"a":{}},standard:{"b":{}}', "found 2 standard"),
        ('standard:{"a":{input:1,output:1,cachedInput:1,cacheWrite:1}', "does not close"),
        ('standard:{"a":{input:1,output:1,cachedInput:1}}', "states"),
        ('standard:{"a":{input:x,output:1,cachedInput:1,cacheWrite:1}}', "unknown rate literal"),
        ('standard:{"a":{input:1,output:1,cachedInput:1,cacheWrite:1},junk}', "unknown shape"),
    ],
)
def test_read_openai_long_context_refuses_what_it_cannot_read(script: str, message: str) -> None:
    with pytest.raises(PageParseError, match=message):
        read_openai_long_context(script)


def test_read_openai_long_context_single_entry_and_literals() -> None:
    tiers = read_openai_long_context(
        'x={standard:{"a":{input:.5,output:2,cachedInput:null,cacheWrite:"-"}}}'
    )
    assert tiers == {
        "a": {
            "input": Decimal("0.5"),
            "output": Decimal(2),
            "cache_read": None,
            "cache_write.30m": None,
            "cache_write.12h": None,
        }
    }


# ---- fetching --------------------------------------------------------------------------


class _Answer(io.BytesIO):
    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _urlopen(body: bytes) -> Callable[..., _Answer]:
    def opener(request: urllib.request.Request, timeout: float) -> _Answer:
        assert request.get_header("User-agent") == "eawf-price-check"
        return _Answer(body)

    return opener


def test_fetch_page_decodes_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(b"<p>$1</p>"))
    assert fetch_page("https://example.invalid/pricing") == "<p>$1</p>"


def test_fetch_page_refuses_a_page_that_is_not_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(b"\xff\xfe"))
    with pytest.raises(ValueError):
        fetch_page("https://example.invalid/pricing")


def test_fetch_page_lets_a_network_error_through(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: urllib.request.Request, timeout: float) -> _Answer:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(OSError):
        fetch_page("https://example.invalid/pricing")


def test_offline_check_without_online_flag_needs_no_page(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(url: str) -> str:
        raise AssertionError(f"offline check fetched {url}")

    monkeypatch.setattr(price_check, "fetch_page", forbid)
    result = _run_check()
    assert result.exit_code == 0, result.output
    assert "offline" in result.output


# ---- the workflow ----------------------------------------------------------------------


def _steps() -> list[dict[str, Any]]:
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = document["jobs"]["price-check"]["steps"]
    return steps


def test_workflow_shape() -> None:
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key ``on`` as the boolean True.
    triggers = document[True]
    assert triggers == {"schedule": [{"cron": "0 6 * * 1"}], "workflow_dispatch": None}
    assert document["permissions"] == {"contents": "read", "issues": "write"}
    assert list(document["jobs"]) == ["price-check"]

    steps = _steps()
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False
    check = next(step for step in steps if step.get("id") == "check")
    assert "uv run eawf reflect prices check --online" in check["run"]
    assert "set +e" in check["run"]
    assert '>> "${GITHUB_STEP_SUMMARY}"' in check["run"]
    assert "if" not in check

    issue = next(step for step in steps if "gh issue" in step.get("run", ""))
    assert issue["if"] == "steps.check.outputs.outcome != 'current'"
    assert issue["env"]["TITLE"] == _ISSUE_TITLE
    assert "gh issue comment" in issue["run"]
    assert "gh issue create" in issue["run"]
    assert steps[-1]["if"] == "steps.check.outputs.outcome != 'current'"
    assert steps[-1]["run"] == "exit 1"

    scripts = " ".join(step.get("run", "") for step in steps)
    assert "git commit" not in scripts
    assert "git push" not in scripts

    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    reflect = root.get_command(click.Context(root), "reflect")
    assert isinstance(reflect, click.Group)
    prices = reflect.get_command(click.Context(reflect), "prices")
    assert isinstance(prices, click.Group)
    assert prices.get_command(click.Context(prices), "check") is not None

    others = [
        path.name
        for path in sorted(_WORKFLOWS.iterdir())
        if path != _WORKFLOW and "price-check" in path.read_text(encoding="utf-8")
    ]
    assert others == []
