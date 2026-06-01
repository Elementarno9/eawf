"""Promotion-call-site tests: EviBound rung-1 wired into validate_markdown_artifact.

These cover success criterion 2 of P29-I01-W08 — the ABSENT promotion
call-site that un-idles :attr:`IntentBrief.evidence_refs`. Before this
wave nothing ran the EviBound gate; now
:func:`validate_markdown_artifact` runs rung-1 over a supplied brief's
``evidence_refs`` so a brief is promotable iff every claim's
evidence_refs resolve.

The chassis-only path (``intent=None``) is asserted unchanged so the
PR / release-notes / coauthor callers keep their existing behaviour.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.spec.intent import IntentBrief
from eawf.platform.artifacts.validation import validate_markdown_artifact


def _artifact_body() -> str:
    """A minimal valid-chassis artifact body (no scrub/citation failures)."""
    return "\n".join(
        [
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


def test_promotion_allowed_when_all_evidence_refs_resolve(tmp_path: Path) -> None:
    """A chassis-clean artifact with a fully-resolving brief validates ok."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("hi", encoding="utf-8")
    brief = IntentBrief(
        problem="p",
        desired_outcome="o",
        evidence_refs=["docs/x.md", "urn:eawf:v1:decision:owner/D17"],
    )
    report = validate_markdown_artifact(
        _artifact_body(),
        intent=brief,
        project_root=tmp_path,
    )
    assert report.ok
    assert report.errors == []


def test_promotion_blocked_when_an_evidence_ref_fails_rung1(tmp_path: Path) -> None:
    """A chassis-clean artifact is blocked when a brief ref fails rung-1.

    The chassis itself is valid; the only failure is the unresolved
    evidence ref, proving the EviBound gate (not the chassis checks) is
    what blocks promotion.
    """
    brief = IntentBrief(
        problem="p",
        desired_outcome="o",
        evidence_refs=["docs/missing.md"],
    )
    report = validate_markdown_artifact(
        _artifact_body(),
        intent=brief,
        project_root=tmp_path,
    )
    assert not report.ok
    assert any("docs/missing.md" in err and "rung-1" in err for err in report.errors)


def test_promotion_without_intent_skips_evibound(tmp_path: Path) -> None:
    """The chassis-only path (intent=None) does not run the EviBound gate.

    Existing PR / release-notes callers pass no brief; their behaviour
    is unchanged — a chassis-clean body validates ok with no EviBound
    error even though no evidence ref was provided.
    """
    report = validate_markdown_artifact(_artifact_body())
    assert report.ok
    assert report.errors == []


def test_promotion_folds_evibound_errors_alongside_chassis_errors(tmp_path: Path) -> None:
    """EviBound rejections accumulate alongside chassis errors, not instead of them."""
    dirty_body = _artifact_body().replace("- status: clean", "- status: dirty")
    brief = IntentBrief(
        problem="p",
        desired_outcome="o",
        evidence_refs=["docs/missing.md"],
    )
    report = validate_markdown_artifact(
        dirty_body,
        intent=brief,
        project_root=tmp_path,
    )
    assert not report.ok
    assert "scrub status must be clean" in report.errors
    assert any("docs/missing.md" in err for err in report.errors)
