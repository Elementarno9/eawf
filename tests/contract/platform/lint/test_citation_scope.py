"""EAWF027, the citation-scope lint, over the four fixtures its row names and the real render.

Requirement row proved here, by id:

- ``LINT-041``: the lint reads every committed artifact, rendered document, rule projection
  and pull-request body, and the proposal packet where it exists, and fails on a reflection
  row whose quotability mark is non-quotable or absent and on any ``scrubbed_extract``
  session title, naming the file, the line and the mark. The fixtures are a positive
  this-project quote, a negative cross-project quote, an unmarked row and a
  ``scrubbed_extract`` title.

The fire-proof case renders a real report through the reflection renderer: a
``scrubbed_extract`` title the renderer marks non-quotable, quoted into a committed
document, is refused in both the text and the machine form.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from eawf.observability.reflect.report import ReflectReport, SessionLine, render_text
from eawf.observability.reflect.titles import TitleSource
from eawf.platform.lint import eawf027_citation_scope as eawf027
from eawf.platform.lint.eawf027_citation_scope import (
    PROPOSAL_PACKET_DIR,
    CitationFinding,
    committed_scope,
    packet_scope,
    scan_paths,
    scan_text,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()

_HEAD = "session R-0042 succeeded claude opus wall 1.5h steps 40 tools 12 subagents 2"

#: A this-project row, quotable.
POSITIVE = f'{_HEAD} · title "Gate rendered artifacts" [model_written] · quotable'
#: A cross-project row, which the report marks non-quotable.
CROSS_PROJECT = f'{_HEAD} · title "Tune the ladder" [structural] · non-quotable'
#: A row whose mark was dropped when it was quoted.
UNMARKED = f'{_HEAD} · title "Gate rendered artifacts" [model_written]'
#: A row whose title is an excerpt, marked quotable by hand.
EXTRACT = f'{_HEAD} · title "fix the thing I asked" [scrubbed_extract] · quotable'


def _line(title_source: TitleSource, *, quotable: bool) -> SessionLine:
    return SessionLine(
        run_key="R-0042",
        status="succeeded",
        runtime="claude",
        model_family="opus",
        wall_hours=Decimal("1.5"),
        model_steps=40,
        tool_work=12,
        subagents=2,
        title="Gate rendered artifacts",
        title_source=title_source,
        quotable=quotable,
    )


def _report(*lines: SessionLine) -> ReflectReport:
    return ReflectReport(
        projects=("eawf",), projection_revision=7, title_fill="enabled", sessions=lines
    )


# ---------- the four fixtures ----------


def test_lint_041_a_this_project_quote_passes() -> None:
    """The positive fixture: a quotable row quoted into a committed document."""
    assert eawf027.scan_text("docs/a.md", f"Median wall time:\n{POSITIVE}\n") == []


def test_lint_041_a_cross_project_quote_fails_with_its_file_line_and_mark() -> None:
    """The negative fixture: a non-quotable row is refused, and the finding says where."""
    findings = scan_text("docs/a.md", f"intro\n\n{CROSS_PROJECT}\n")
    assert findings == [CitationFinding("docs/a.md", 3, "non-quotable")]
    assert findings[0].render() == "docs/a.md:3: EAWF027 reflection row marked non-quotable"


def test_lint_041_an_unmarked_row_fails() -> None:
    """A row whose mark was dropped has an unknown scope, so it is refused."""
    assert scan_text("pr", UNMARKED) == [CitationFinding("pr", 1, "unmarked")]


def test_lint_041_a_scrubbed_extract_title_fails_whatever_its_mark() -> None:
    """An excerpt title is local-only at any scope, even marked quotable."""
    assert scan_text("x.md", EXTRACT) == [CitationFinding("x.md", 1, "scrubbed_extract")]


def test_lint_041_a_scrubbed_extract_title_fails_outside_a_report_row() -> None:
    """Wherever it appears: the source mark alone, lifted out of its row, is refused."""
    assert scan_text("x.md", "the title [scrubbed_extract] came from") == [
        CitationFinding("x.md", 1, "scrubbed_extract")
    ]


def test_lint_041_prose_naming_the_title_source_is_not_a_quotation() -> None:
    """Explaining the fallback chain names ``scrubbed_extract`` without quoting a title."""
    assert scan_text("x.md", "then `scrubbed_extract`, then `structural`") == []


@pytest.mark.parametrize("text", ["", "\n", "session notes: none quoted"])
def test_lint_041_empty_and_unrelated_text_passes(text: str) -> None:
    """The empty boundary and a line that merely mentions a session."""
    assert scan_text("x.md", text) == []


# ---------- the real render ----------


def test_lint_041_a_real_rendered_extract_row_is_refused_in_text_form() -> None:
    """The renderer's own output for an excerpt title fails when committed."""
    text = render_text(_report(_line(TitleSource.SCRUBBED_EXTRACT, quotable=False)))
    marks = [finding.mark for finding in scan_text("docs/audit.md", text)]
    assert marks == ["scrubbed_extract"]


def test_lint_041_a_real_rendered_quotable_row_passes_in_text_form() -> None:
    """The renderer's quotable row is exactly the positive fixture's shape."""
    text = render_text(_report(_line(TitleSource.MODEL_WRITTEN, quotable=True)))
    assert scan_text("docs/audit.md", text) == []


def test_lint_041_a_real_rendered_non_quotable_row_is_refused_in_text_form() -> None:
    """A row the renderer marks non-quotable fails with that mark."""
    text = render_text(_report(_line(TitleSource.STRUCTURAL, quotable=False)))
    assert [finding.mark for finding in scan_text("d.md", text)] == ["non-quotable"]


def test_lint_041_the_machine_form_is_refused_on_both_fields() -> None:
    """The report document carries the same two facts, and both are read."""
    document = _report(_line(TitleSource.SCRUBBED_EXTRACT, quotable=False)).model_dump_json(
        indent=2
    )
    marks = sorted(finding.mark for finding in scan_text("r.json", document))
    assert marks == ["non-quotable", "scrubbed_extract"]


# ---------- the scope it reads ----------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a committed tree with one clean document, the lint's working directory."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "clean.md").write_text(f"{POSITIVE}\n", encoding="utf-8")
    (tmp_path / "src.py").write_text(f'ROW = "{CROSS_PROJECT}"\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_lint_041_the_committed_scope_excludes_source(tree: Path) -> None:
    """Source files are not rendered documents; a string constant there is not a quote."""
    assert committed_scope(tree) == [Path("docs/clean.md")]


def _hook(*args: str) -> Result:
    """Run the lint's production call-site in the working directory."""
    return runner.invoke(app, ["hook", "eawf027-citation-scope", *args])


def test_lint_041_a_clean_tree_passes(tree: Path) -> None:
    """The committed scope quotes only a quotable row, so the hook exits zero."""
    result = _hook()
    assert result.exit_code == 0, result.stdout
    assert result.stdout == "eawf027-citation-scope: clean (1 file(s) scanned)\n"


def test_lint_041_a_committed_artifact_quoting_a_refused_row_fails(tree: Path) -> None:
    """A tracked artifact is in scope, and the finding carries its path, line and mark."""
    audit = tree / ".ea" / "artifacts" / "audits"
    audit.mkdir(parents=True)
    (audit / "2026-09-29-review.md").write_text(f"# Audit\n{EXTRACT}\n", encoding="utf-8")
    _git(tree, "add", "-A")
    result = _hook()
    assert result.exit_code == 1
    assert result.stdout.splitlines()[1:] == [
        "  .ea/artifacts/audits/2026-09-29-review.md:2: EAWF027 reflection row marked "
        "scrubbed_extract"
    ]


def test_lint_041_the_packet_is_scanned_where_it_exists(tree: Path) -> None:
    """A specification file quoting a cross-project row fails, though it is never committed."""
    packet = tree / PROPOSAL_PACKET_DIR
    packet.mkdir(parents=True)
    (packet / "67-measurement.md").write_text(f"{CROSS_PROJECT}\n", encoding="utf-8")
    assert packet_scope(tree) == [PROPOSAL_PACKET_DIR / "67-measurement.md"]
    result = _hook()
    assert result.exit_code == 1
    assert "67-measurement.md:1: EAWF027 reflection row marked non-quotable" in result.stdout


def test_lint_041_the_packet_scope_is_empty_where_the_packet_is_absent(tree: Path) -> None:
    """The packet is local; where it is absent, as in CI, it contributes nothing."""
    assert packet_scope(tree) == []


def test_lint_041_a_pull_request_body_is_scanned(tree: Path) -> None:
    """The body named on the command line is read beside the tree."""
    body = tree / "body.md"
    body.write_text(f"## Summary\n{UNMARKED}\n", encoding="utf-8")
    result = _hook("--pr-body", str(body))
    assert result.exit_code == 1
    assert result.stdout.splitlines()[1:] == [
        "  pull-request body:2: EAWF027 reflection row marked unmarked"
    ]


def test_lint_041_a_missing_pull_request_body_is_a_usage_error(tree: Path) -> None:
    """A named body that does not exist fails loudly rather than scanning nothing."""
    result = _hook("--pr-body", str(tree / "absent.md"))
    assert result.exit_code == 2


def test_lint_041_outside_a_work_tree_the_lint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed scope needs git; outside a work tree that is an error, not a pass."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        committed_scope(tmp_path)


def test_lint_041_this_repository_is_clean() -> None:
    """The shipped tree quotes no refused row."""
    root = Path(__file__).resolve().parents[4]
    assert [f.render() for f in scan_paths(root, committed_scope(root))] == []
