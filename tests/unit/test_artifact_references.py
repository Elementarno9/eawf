from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from pydantic import ValidationError

from eawf.artifacts.references import (
    Citation,
    CitationValidationError,
    citation_numbers_in_text,
    validate_dense_citation_refs,
    validate_dense_citations,
)
from eawf.artifacts.validation import validate_markdown_artifact
from eawf.scrub.scan import rewrite_text, scan_text


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


def test_validate_markdown_artifact_accepts_sentinel_before_h1() -> None:
    body = "\n".join(
        [
            "<!-- eawf-template: plan -->",
            "# Plan",
            "",
            "## Summary",
            "",
            "Done.",
            "",
            "## References",
            "",
            "(none)",
            "",
            "## Provenance",
            "",
            "- kind: plan",
            "",
            "## Scrub",
            "",
            "- status: clean",
            "",
        ]
    )
    report = validate_markdown_artifact(body, require_template_sentinel=True)
    assert report.ok
