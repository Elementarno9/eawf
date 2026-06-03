from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from eawf.platform.artifacts.references import (
    Citation,
    CitationValidationError,
    citation_numbers_in_text,
    validate_dense_citation_refs,
    validate_dense_citations,
)
from eawf.platform.artifacts.validation import (
    _reference_rows,
    validate_markdown_artifact,
)
from eawf.platform.scrub.scan import rewrite_text, scan_text
from eawf.surfaces.render.artifact_chassis import reference_anchor, render_references


def _artifact_body(
    *,
    summary: str = "Done.",
    references: str = "(none)",
    provenance: str = "- kind: plan",
    scrub: str = "- status: clean",
) -> str:
    return "\n".join(
        [
            "# Plan",
            "",
            "## Summary",
            "",
            summary,
            "",
            "## References",
            "",
            references,
            "",
            "## Provenance",
            "",
            provenance,
            "",
            "## Scrub",
            "",
            scrub,
            "",
        ]
    )


def test_validate_dense_citations_accepts_repo_url_and_urn_refs() -> None:
    citations = [
        Citation(n=1, ref="src/eawf/state/models.py:1", kind="repo"),
        Citation(n=2, ref="https://example.org/spec", kind="url"),
        Citation(n=3, ref="urn:eawf:v1:store:research/BR-001", kind="urn"),
    ]
    validate_dense_citations(citations)


def test_validate_dense_citations_rejects_gap() -> None:
    with pytest.raises(CitationValidationError, match="dense"):
        validate_dense_citations(
            [
                Citation(n=1, ref="docs/a.md"),
                Citation(n=3, ref="docs/b.md"),
            ]
        )


def test_citation_rejects_absolute_path() -> None:
    absolute_ref = PurePosixPath("/", "local", "project", "file.py").as_posix()
    with pytest.raises(ValidationError, match="repo-relative"):
        Citation(n=1, ref=absolute_ref)


def test_validate_dense_citation_refs_requires_used_rows() -> None:
    citations = [Citation(n=1, ref="docs/a.md"), Citation(n=2, ref="docs/b.md")]
    with pytest.raises(CitationValidationError, match="unused"):
        validate_dense_citation_refs("Finding [1].", citations)


def test_citation_numbers_in_text_ignores_image_alt_text() -> None:
    assert citation_numbers_in_text("See [2], not ![1](image.png)") == [2]


def test_scrub_scan_and_rewrite_local_tokens() -> None:
    local_path = PurePosixPath("/", "tmp", "repo").as_posix()
    local_url = "http://" + "local" + "host:8000"
    text = f"Opened {local_path} and {local_url} for review."
    findings = scan_text(text)
    assert {finding.kind for finding in findings} == {"absolute_posix_path", "local_url"}
    assert "<local-path>" in rewrite_text(text)
    assert "<local-url>" in rewrite_text(text)


def test_scrub_scan_blocks_hostnames_and_non_allowlisted_email() -> None:
    host = ".".join(["devbox", "local"])
    email = "person" + "@" + "example" + ".com"
    private_ip = ".".join(["192", "168", "1", "20"])
    text = f"Host {host} sent mail to {email} from {private_ip}."
    findings = scan_text(text)
    assert {finding.kind for finding in findings} == {
        "email",
        "local_hostname",
        "private_ip",
    }


def test_scrub_scan_allows_canonical_coauthor_noreply() -> None:
    text = "Co-Authored-By: Codex <noreply@openai.com>"
    assert scan_text(text) == []


def test_validate_markdown_artifact_accepts_sentinel_before_h1() -> None:
    body = "\n".join(
        [
            "<!-- eawf-template: plan -->",
            _artifact_body(),
        ]
    )
    report = validate_markdown_artifact(body, require_template_sentinel=True)
    assert report.ok


def test_validate_markdown_artifact_requires_provenance_content() -> None:
    report = validate_markdown_artifact(_artifact_body(provenance="(none)"))
    assert "provenance section is empty" in report.errors


def test_validate_markdown_artifact_requires_clean_scrub_status() -> None:
    report = validate_markdown_artifact(_artifact_body(scrub="- status: dirty"))
    assert "scrub status must be clean" in report.errors


def test_validate_markdown_artifact_accepts_dense_markdown_reference_rows() -> None:
    report = validate_markdown_artifact(
        _artifact_body(
            summary="Renderer uses typed rows [1].",
            references="[1] src/eawf/surfaces/render/research.py:1",
        )
    )
    assert report.ok


def test_validate_markdown_artifact_rejects_missing_markdown_reference_rows() -> None:
    report = validate_markdown_artifact(_artifact_body(summary="Renderer uses typed rows [1]."))
    assert "citation references missing rows: [1]" in report.errors


def test_validate_markdown_artifact_rejects_unused_markdown_reference_rows() -> None:
    report = validate_markdown_artifact(
        _artifact_body(references="[1] src/eawf/surfaces/render/research.py:1")
    )
    assert "citation rows unused by prose: [1]" in report.errors


def test_validate_markdown_artifact_rejects_non_dense_markdown_reference_rows() -> None:
    report = validate_markdown_artifact(
        _artifact_body(
            summary="Renderer uses typed rows [2].",
            references="[2] src/eawf/surfaces/render/research.py:1",
        )
    )
    assert "citation numbers must be dense 1..1; got [2]" in report.errors


def test_validate_markdown_artifact_rejects_unportable_markdown_reference_row() -> None:
    absolute_ref = PurePosixPath("/", "tmp", "repo", "artifact.md").as_posix()
    report = validate_markdown_artifact(
        _artifact_body(
            summary="Artifact uses an unportable row [1].", references=f"[1] {absolute_ref}"
        )
    )
    assert any("repo-relative" in error for error in report.errors)


# ---- numbered + anchored references render (W13) ----------------------------


def test_render_references_empty_renders_none() -> None:
    assert render_references([]) == ["## References", "", "(none)"]


def test_render_references_emits_ordered_list_with_anchors_and_self_links() -> None:
    rows = render_references(
        [
            Citation(n=1, ref="src/eawf/a.py:10", title="loader", note="why"),
            Citation(n=2, ref="https://example.org/spec", kind="url"),
        ]
    )
    assert rows[0] == "## References"
    body = rows[2:]
    # Ordered-list markers in number order.
    assert body[0].startswith("1. ")
    assert body[1].startswith("2. ")
    # Each row carries its stable anchor + a [N] self-link to that anchor.
    assert '<a id="ref-1"></a>' in body[0]
    assert r"[\[1\]](#ref-1)" in body[0]
    assert '<a id="ref-2"></a>' in body[1]
    assert r"[\[2\]](#ref-2)" in body[1]
    # The ref, title, and note still render after the marker.
    assert "src/eawf/a.py:10" in body[0]
    assert "— loader" in body[0]
    assert "(why)" in body[0]


def test_reference_anchor_is_stable_per_number() -> None:
    assert reference_anchor(3) == "ref-3"


def test_reference_rows_round_trips_new_anchored_shape() -> None:
    cits = [
        Citation(n=1, ref="src/eawf/a.py:10", title="loader"),
        Citation(n=2, ref="https://example.org/spec", kind="url"),
        Citation(n=3, ref="urn:eawf:v1:store:research/BR-001", kind="urn"),
    ]
    rendered = render_references(cits)
    section = "\n".join(rendered[2:])
    parsed = _reference_rows(section)
    assert [c.n for c in parsed] == [1, 2, 3]
    assert parsed[0].ref == "src/eawf/a.py:10"
    assert parsed[1].ref == "https://example.org/spec"
    assert parsed[2].ref == "urn:eawf:v1:store:research/BR-001"


def test_reference_rows_still_parses_legacy_bare_rows() -> None:
    section = "\n".join(
        [
            "[1] src/eawf/a.py:10 — loader (why)",
            "[2] docs/b.md",
        ]
    )
    parsed = _reference_rows(section)
    assert [c.n for c in parsed] == [1, 2]
    assert parsed[0].ref == "src/eawf/a.py:10"
    assert parsed[1].ref == "docs/b.md"


def test_validate_markdown_artifact_accepts_new_anchored_reference_rows() -> None:
    rendered = render_references([Citation(n=1, ref="src/eawf/surfaces/render/research.py:1")])
    report = validate_markdown_artifact(
        _artifact_body(
            summary="Renderer uses typed rows [1].",
            references="\n".join(rendered[2:]),
        )
    )
    assert report.ok, report.errors
