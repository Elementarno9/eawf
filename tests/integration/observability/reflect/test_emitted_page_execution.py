"""The exported reflection page, executed against its real data chunk in a minimal DOM.

Requirement rows proved here, by id:

- ``LINT-036``: a change to the emitted page is gated by a harness that executes the
  page's own scripts against the data chunk the exporter wrote, in a minimal DOM with no
  browser engine and no runtime dependency, opens every disclosure, and fails on a thrown
  error, a rejected promise and anything a ``.catch`` logged instead of raising. A missing
  chunk is reported as missing, apart from the renderer error it causes. Absence of errors
  is not enough: the harness asserts that what the page draws is non-empty and equals the
  fixture it was fed, run against a named worst-case fixture rather than a small default.
- ``LINT-037`` (probe half): the rendered run is the instrument. The harness reads what the
  page drew -- its console errors, heading structure, labelled controls, and the rows it
  plotted against the sessions the data claims -- rather than scanning the page source.

The minimal DOM is ``minimal_dom.mjs`` beside this module, run by Node, which the
repository's CI images carry. The page markup is parsed here with the standard library and
handed to it as a tree.

The page this row was written for drew timelines, rulers and zoom levels; the exported
reflection page draws one sessions table and a head line, so the per-lane, per-zoom and
bar-packing assertions of the row have no panel to run against and are not asserted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.reflect.report import (
    EXPORT_DATA_FILENAME,
    EXPORT_PAGE_FILENAME,
    FillManifest,
    ReflectReport,
    SessionLine,
    export_report,
    store_report,
)
from eawf.observability.reflect.titles import TitleSource

HARNESS = Path(__file__).with_name("minimal_dom.mjs")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="the page harness runs on Node")

_VOID = frozenset({"meta", "link", "br", "hr", "img", "input", "col", "source"})


class _Tree(HTMLParser):
    """Parse a page into the element tree the harness builds its DOM from."""

    def __init__(self) -> None:
        super().__init__()
        self.root: dict[str, Any] = {"tag": "html", "attrs": {}, "children": [], "text": ""}
        self.stack: list[dict[str, Any]] = [self.root]
        self.scripts: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html":
            return
        values = {name: value or "" for name, value in attrs}
        if tag == "script":
            self.scripts.append({"src": values["src"]} if "src" in values else {"code": ""})
        node = {"tag": tag, "attrs": values, "children": [], "text": ""}
        self.stack[-1]["children"].append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag != "html" and tag not in _VOID:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        top = self.stack[-1]
        if top["tag"] == "script":
            self.scripts[-1]["code"] = self.scripts[-1].get("code", "") + data
        elif data.strip():
            top["text"] += data


@dataclass(frozen=True, slots=True)
class PageRun:
    """What one execution of a page produced.

    Attributes:
        errors: Thrown errors, rejections and console errors, in order.
        missing_chunks: Script chunks the page names that were not on disk.
        disclosures_opened: How many ``details`` elements were opened.
        headings: ``(tag, text)`` of every heading.
        unlabelled_controls: Controls with neither a label nor an ``aria-label``.
        texts: Text of every element carrying an id.
        tables: Body rows, as cell texts, of every table carrying an id.
    """

    errors: tuple[dict[str, str], ...]
    missing_chunks: tuple[str, ...]
    disclosures_opened: int
    headings: tuple[tuple[str, str], ...]
    unlabelled_controls: int
    texts: dict[str, str]
    tables: dict[str, list[list[str]]]


def run_page(page: Path) -> PageRun:
    """Execute ``page`` and the chunks beside it in the minimal DOM.

    Raises:
        subprocess.CalledProcessError: The harness itself failed, which is never the
            page's fault and is not reported as a page error.
    """
    parser = _Tree()
    parser.feed(page.read_text(encoding="utf-8"))
    files = {
        script["src"]: (page.parent / script["src"]).read_text(encoding="utf-8")
        for script in parser.scripts
        if "src" in script and (page.parent / script["src"]).is_file()
    }
    payload = json.dumps({"tree": parser.root, "scripts": parser.scripts, "files": files})
    done = subprocess.run(
        [str(NODE), str(HARNESS)], input=payload, capture_output=True, text=True, check=True
    )
    out = json.loads(done.stdout)
    return PageRun(
        errors=tuple(out["errors"]),
        missing_chunks=tuple(out["missing_chunks"]),
        disclosures_opened=out["disclosures_opened"],
        headings=tuple(tuple(h) for h in out["headings"]),
        unlabelled_controls=out["unlabelled_controls"],
        texts=out["texts"],
        tables=out["tables"],
    )


def _session(index: int) -> SessionLine:
    """Return the ``index``-th session of the worst case, cycling every edge the page reads."""
    sources = list(TitleSource)
    source = sources[index % len(sources)]
    return SessionLine(
        run_key=f"R-{index:04d}",
        status=("succeeded", "failed", "lost", "unended")[index % 4],
        runtime=("claude", "codex", "opencode")[index % 3],
        model_family=("opus", "sonnet", "gpt")[index % 3],
        wall_hours=None if index % 5 == 0 else Decimal(index) / 4,
        model_steps=index * 3,
        tool_work=index % 7,
        subagents=index % 3,
        title=None if source is TitleSource.UNAVAILABLE else f"Session {index}",
        title_source=source,
        quotable=source is not TitleSource.SCRUBBED_EXTRACT,
    )


#: The largest mixed session set of the render: every status, runtime and title source,
#: an unended wall time every fifth row and no title where none was available.
WORST_CASE = ReflectReport(
    projects=("abc", "eawf"),
    projection_revision=4096,
    title_fill="enabled",
    sessions=tuple(_session(index) for index in range(240)),
)

_MANIFEST = FillManifest(
    provider=None,
    local_only=True,
    sessions_listed=len(WORST_CASE.sessions),
    digests_sent=0,
    cache_hits=0,
    unknown_uncached=0,
    titles_by_source={},
)


def _export(tmp_path: Path, report: ReflectReport = WORST_CASE) -> Path:
    """Store ``report`` and export it through the production path; return the page."""
    stored = store_report(tmp_path, report, _MANIFEST, on=date(2026, 9, 29), out=None)
    return export_report(tmp_path, stored.document, dest=tmp_path / "export")


def _expected_row(line: SessionLine) -> list[str]:
    wall = "unended" if line.wall_hours is None else str(line.wall_hours)
    return [
        line.run_key,
        line.status,
        line.runtime,
        line.model_family,
        wall,
        str(line.model_steps),
        str(line.tool_work),
        str(line.subagents),
        line.title if line.title is not None else "no title",
        line.title_source.value,
        "yes" if line.quotable else "no",
    ]


def _mutated(page: Path, old: str, new: str) -> Path:
    """Rewrite the emitted page in place, the way a defective change would."""
    text = page.read_text(encoding="utf-8")
    assert old in text
    page.write_text(text.replace(old, new), encoding="utf-8")
    return page


# ---------- the page runs clean and draws exactly its fixture ----------


def test_lint_036_the_emitted_page_runs_without_error_on_the_worst_case(tmp_path: Path) -> None:
    """Nothing thrown, rejected or logged, and no chunk missing."""
    run = run_page(_export(tmp_path))
    assert run.errors == ()
    assert run.missing_chunks == ()


def test_lint_036_every_count_the_page_draws_is_non_zero(tmp_path: Path) -> None:
    """A page that draws nothing throws nothing, so each drawn count is asserted."""
    run = run_page(_export(tmp_path))
    rows = run.tables["sessions"]
    assert len(rows) > 0
    assert all(len(row) == 11 for row in rows)
    assert run.texts["head"]
    assert run.headings


def test_lint_036_every_drawn_row_equals_its_fixture_row(tmp_path: Path) -> None:
    """The table is the fixture, row for row: nothing merged, dropped or reordered."""
    run = run_page(_export(tmp_path))
    assert run.tables["sessions"] == [_expected_row(line) for line in WORST_CASE.sessions]


def test_lint_036_the_head_derives_from_the_same_fixture(tmp_path: Path) -> None:
    """Each quantity the head prints is the fixture's own value."""
    head = run_page(_export(tmp_path)).texts["head"]
    assert head == ("projects abc, eawf · projection revision 4096 · title fill enabled")


def test_lint_036_no_cell_prints_a_missing_value_as_text(tmp_path: Path) -> None:
    """The null edges -- no wall time, no title -- render as words, never as ``null``."""
    cells = [cell for row in run_page(_export(tmp_path)).tables["sessions"] for cell in row]
    assert not {"null", "undefined", "NaN", ""} & set(cells)


def test_lint_036_a_single_session_report_draws_one_row(tmp_path: Path) -> None:
    """The single boundary."""
    report = WORST_CASE.model_copy(update={"sessions": WORST_CASE.sessions[:1]})
    assert len(run_page(_export(tmp_path, report)).tables["sessions"]) == 1


def test_lint_036_an_empty_report_draws_no_row_and_no_error(tmp_path: Path) -> None:
    """The empty boundary: nothing to draw is drawn as nothing, not as a failure."""
    report = WORST_CASE.model_copy(update={"sessions": (), "projects": ()})
    run = run_page(_export(tmp_path, report))
    assert run.errors == ()
    assert run.tables["sessions"] == []
    assert run.texts["head"].startswith("projects none ·")


# ---------- the gate fires ----------


def test_lint_036_a_null_the_page_does_not_guard_is_a_thrown_error(tmp_path: Path) -> None:
    """The failure class the gate was built on: a null input the render does not guard."""
    page = _mutated(_export(tmp_path), 's.wall_hours ?? "unended"', "s.wall_hours.toFixed(1)")
    run = run_page(page)
    assert [error["kind"] for error in run.errors] == ["thrown"]
    assert "TypeError" in run.errors[0]["message"]


def test_lint_036_a_page_that_draws_nothing_fails_on_its_counts(tmp_path: Path) -> None:
    """A renderer reading the wrong field throws nothing and draws zero rows."""
    page = _mutated(_export(tmp_path), "of report.sessions", "of (report.session || [])")
    run = run_page(page)
    assert run.errors == ()
    assert run.tables["sessions"] == []


def test_lint_036_a_missing_chunk_is_reported_apart_from_the_error_it_causes(
    tmp_path: Path,
) -> None:
    """A missing chunk is named as missing; the renderer error it causes is not absent data."""
    page = _export(tmp_path)
    (page.parent / EXPORT_DATA_FILENAME).unlink()
    run = run_page(page)
    assert run.missing_chunks == (EXPORT_DATA_FILENAME,)
    assert [error["kind"] for error in run.errors] == ["thrown"]


def test_lint_036_a_logged_catch_fails(tmp_path: Path) -> None:
    """An error a ``.catch`` logged instead of raising is still an error."""
    page = _mutated(
        _export(tmp_path),
        "const report = window.EAWF_REFLECT_REPORT;",
        'Promise.reject(new Error("boot")).catch((e) => console.error(e));\n'
        "const report = window.EAWF_REFLECT_REPORT;",
    )
    assert [error["kind"] for error in run_page(page).errors] == ["console.error"]


def test_lint_036_a_rejected_boot_fails(tmp_path: Path) -> None:
    """A promise nobody handles is a rejected boot."""
    page = _mutated(
        _export(tmp_path),
        "const report = window.EAWF_REFLECT_REPORT;",
        'Promise.reject(new Error("boot"));\nconst report = window.EAWF_REFLECT_REPORT;',
    )
    assert [error["kind"] for error in run_page(page).errors] == ["rejected"]


def test_lint_036_every_disclosure_is_opened(tmp_path: Path) -> None:
    """Deferred content renders only when opened, so the harness opens each one."""
    page = _mutated(
        _export(tmp_path),
        "</body>",
        '<details id="more"><summary>More</summary></details>\n<script>\n'
        'document.getElementById("more").addEventListener("toggle", () => {'
        ' throw new Error("deferred"); });\n</script>\n</body>',
    )
    run = run_page(page)
    assert run.disclosures_opened == 1
    assert [error["kind"] for error in run.errors] == ["thrown"]


def test_lint_036_a_harness_that_cannot_parse_its_input_is_not_a_page_error() -> None:
    """The harness failing is its own error, never reported as the page's."""
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [str(NODE), str(HARNESS)], input="not json", text=True, check=True, capture_output=True
        )


# ---------- LINT-037: the rendered run is the instrument ----------


def test_lint_037_the_probe_reports_headings_controls_and_plotted_counts(tmp_path: Path) -> None:
    """Heading structure, labelled controls and plotted-versus-claimed rows off the run."""
    run = run_page(_export(tmp_path))
    assert run.headings == (("H1", "Reflect report"),)
    assert run.unlabelled_controls == 0
    assert len(run.tables["sessions"]) == len(WORST_CASE.sessions)


def test_lint_037_an_unlabelled_control_is_counted(tmp_path: Path) -> None:
    """A control nothing names is a finding of the probe, read off the rendered page."""
    page = _mutated(_export(tmp_path), "</body>", '<input id="filter">\n</body>')
    assert run_page(page).unlabelled_controls == 1


def test_lint_037_a_claim_the_source_scan_would_pass_is_caught_by_the_run(tmp_path: Path) -> None:
    """The page source still names every column, yet the rendered run draws none of them."""
    page = _mutated(_export(tmp_path), "row.insertCell().textContent = String(v);", "")
    assert "insertCell" not in page.read_text(encoding="utf-8")
    assert "<th>Run</th>" in page.read_text(encoding="utf-8")
    run = run_page(page)
    assert run.errors == ()
    assert all(row == [] for row in run.tables["sessions"])
    assert page.name == EXPORT_PAGE_FILENAME
