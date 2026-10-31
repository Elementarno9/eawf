"""The curated docs and the published surfaces describe only the native model.

The lifecycle is Track, Milestone, Batch, Task and Run, planned through a
PlanRevision. A docs page that still describes phases, iters and waves as
current, a published plan-view schema or renderer, or a bench operation
that walks waves sends a reader after an entity that no longer exists. The
epoch-2 cutover page is the one page that names the retired model on
purpose: it explains how a tree crosses from it.

The generated pages under ``docs/reference/autogen`` are rendered from the
live source tree and pinned to it by the autogen drift gate, so they follow
the source vocabulary and are not censused here.
"""

from __future__ import annotations

from pathlib import Path

from eawf.observability.bench.harness import HARNESS_CATALOG
from eawf.workflow.skills.census import retired_nouns

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "docs"
_CUTOVER_PAGE = _DOCS / "reference" / "epoch2-cutover.md"
_AUTOGEN = _DOCS / "reference" / "autogen"
_POLICY_PAGES = (
    _DOCS / "policy" / "agents-claude-md.md",
    _DOCS / "policy" / "no-plan-md.md",
    _DOCS / "policy" / "fixed-decisions.md",
    _DOCS / "tutorial" / "first-workflow.md",
)


def _curated_pages() -> list[Path]:
    """Return the concept, architecture and curated reference pages."""
    pages = [_DOCS / "concepts.md", *sorted((_DOCS / "architecture").rglob("*.md"))]
    pages.extend(
        page
        for page in sorted((_DOCS / "reference").rglob("*.md"))
        if page != _CUTOVER_PAGE and not page.is_relative_to(_AUTOGEN)
    )
    return pages


def _noun_findings(pages: list[Path], *, root: Path = _REPO_ROOT) -> list[str]:
    """Return one ``<page>: <nouns>`` line per page under *root* naming an epoch-1 noun."""
    findings: list[str] = []
    for page in pages:
        nouns = retired_nouns(page.read_text(encoding="utf-8"))
        if nouns:
            findings.append(f"{page.relative_to(root).as_posix()}: {', '.join(nouns)}")
    return findings


def test_docs_describe_only_the_native_model() -> None:
    findings = _noun_findings(_curated_pages())
    plan_view_schema = _REPO_ROOT / "src" / "eawf" / "schemas" / "plan-view.schema.json"
    if plan_view_schema.exists():
        findings.append("src/eawf/schemas/plan-view.schema.json still exists")
    assert findings == []


def test_plan_view_is_gone_and_policy_pages_are_native() -> None:
    findings = _noun_findings(list(_POLICY_PAGES))
    if (_REPO_ROOT / "src" / "eawf" / "surfaces" / "render" / "plan_view.py").exists():
        findings.append("src/eawf/surfaces/render/plan_view.py still exists")
    if "plan_view_render" in HARNESS_CATALOG:
        findings.append("the bench harness catalogue still holds plan_view_render")
    golden = _REPO_ROOT / "tests" / "golden"
    if (golden / "surfaces" / "render" / "plan_view").exists():
        findings.append("tests/golden/surfaces/render/plan_view still exists")
    findings.extend(
        f"{path.relative_to(_REPO_ROOT).as_posix()} still exists"
        for path in sorted((golden / "narrative").glob("wave_*.md"))
    )
    assert findings == []


def test_the_census_reads_every_curated_page() -> None:
    pages = _curated_pages()
    assert _DOCS / "concepts.md" in pages
    assert _DOCS / "architecture" / "state-model.md" in pages
    assert _CUTOVER_PAGE not in pages
    assert not any(page.is_relative_to(_AUTOGEN) for page in pages)


def test_the_census_reports_a_page_naming_a_retired_noun(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("A Milestone ships through one Batch.\n", encoding="utf-8")
    stale = tmp_path / "stale.md"
    stale.write_text("Close the wave, then the phase P01-I01.\n", encoding="utf-8")
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    findings = _noun_findings([clean, stale, empty], root=tmp_path)
    assert findings == ["stale.md: wave, phase, P01-I01"]
