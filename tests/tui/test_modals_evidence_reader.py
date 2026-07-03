"""Pure-render tests for the ``EvidenceReaderModal`` claim reader helpers.

The modal is a thin scrollable view over four pure module functions -- the
numbered supporting-source list, the conflicts list, the provenance line, and
the full-body composer -- so the content is asserted here without mounting
Textual. The Pilot integration (Enter opens the reader, Esc dismisses it) lives
in ``test_modes_research_board.py`` where the board drives the push.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import ClaimStatus, OpenQuestionStatus
from eawf.kernel.state.models import Claim, OpenQuestion
from eawf.surfaces.tui.modals.evidence_reader import (
    NO_CONFLICTS_NOTICE,
    NO_QUESTION_NOTICE,
    NO_SOURCES_NOTICE,
    conflict_lines,
    evidence_source_lines,
    provenance_line,
    render_evidence_reader,
)

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Row builders
# --------------------------------------------------------------------------


def _claim(
    claim_id: str = "CL-0001",
    *,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    description: str | None = None,
    evidence_refs: list[str] | None = None,
    answers_question_id: str | None = None,
    source_artifact_id: str | None = None,
) -> Claim:
    """Build a claim row for the reader helpers."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title="Short tenor is best fit by SABR",
        description=description,
        status=status,
        evidence_refs=evidence_refs if evidence_refs is not None else [],
        answers_question_id=answers_question_id,
        source_artifact_id=source_artifact_id,
        created_at=_T0,
    )


def _question(question_id: str = "OQ-0001") -> OpenQuestion:
    """Build an open-question row the provenance line can name by title."""
    return OpenQuestion(
        id=question_id,
        scope_id="QR",
        title="Which curve model fits the short tenor",
        status=OpenQuestionStatus.OPEN,
        created_at=_T0,
    )


# --------------------------------------------------------------------------
# evidence_source_lines -- the numbered [N] supporting-source list
# --------------------------------------------------------------------------


def test_evidence_source_lines_empty_returns_honest_notice() -> None:
    """A claim citing no evidence renders the honest no-sources notice."""
    assert evidence_source_lines(_claim()) == (NO_SOURCES_NOTICE,)


def test_evidence_source_lines_single_ref_is_numbered_one() -> None:
    """A single source renders as a 1-indexed ``[1]`` line (off-by-one boundary)."""
    lines = evidence_source_lines(_claim(evidence_refs=["docs/sabr.md"]))
    assert lines == ("[1] docs/sabr.md",)


def test_evidence_source_lines_numbers_each_ref_in_order() -> None:
    """Multiple sources render as an ascending ``[1]`` / ``[2]`` / ``[3]`` list."""
    lines = evidence_source_lines(
        _claim(evidence_refs=["docs/sabr.md", "docs/fit.md", "https://x.example/y"])
    )
    assert lines == (
        "[1] docs/sabr.md",
        "[2] docs/fit.md",
        "[3] https://x.example/y",
    )


# --------------------------------------------------------------------------
# conflict_lines -- the contradicting-sibling list (honest "none" empty)
# --------------------------------------------------------------------------


def test_conflict_lines_empty_returns_none_notice() -> None:
    """No resolvable conflict renders the honest single ``none`` line."""
    assert conflict_lines(()) == (NO_CONFLICTS_NOTICE,)


def test_conflict_lines_renders_status_and_title_per_conflict() -> None:
    """Each contradicting sibling renders its status word then its title."""
    conflicts = (
        _claim("CL-0002", status=ClaimStatus.REFUTED),
        _claim("CL-0003", status=ClaimStatus.REFUTED),
    )
    lines = conflict_lines(conflicts)
    assert lines == (
        "refuted -- Short tenor is best fit by SABR",
        "refuted -- Short tenor is best fit by SABR",
    )


# --------------------------------------------------------------------------
# provenance_line -- what produced the claim
# --------------------------------------------------------------------------


def test_provenance_line_names_question_status_and_timestamp() -> None:
    """Provenance names the answered question (by title), status, and log time."""
    claim = _claim(answers_question_id="OQ-0001")
    line = provenance_line(claim, (_question(),))
    assert "answers OQ-0001 (Which curve model fits the short tenor)" in line
    assert "status supported" in line
    assert "logged 2026-05-27T12:00:00+00:00" in line


def test_provenance_line_includes_source_artifact_when_present() -> None:
    """A claim distilled from an artifact names it in the provenance tail."""
    claim = _claim(answers_question_id="OQ-0001", source_artifact_id="ART-42")
    line = provenance_line(claim, (_question(),))
    assert "from ART-42" in line


def test_provenance_line_drops_source_artifact_when_absent() -> None:
    """No source artifact leaves the provenance line without a trailing ``from``."""
    line = provenance_line(_claim(answers_question_id="OQ-0001"), (_question(),))
    assert "from" not in line


def test_provenance_line_free_standing_claim_names_no_question() -> None:
    """A claim answering no tracked question reads the honest no-question notice."""
    line = provenance_line(_claim(answers_question_id=None), ())
    assert NO_QUESTION_NOTICE in line


def test_provenance_line_unresolved_question_falls_back_to_id() -> None:
    """A claim whose question is not on hand names it by id alone (missing-key edge)."""
    claim = _claim(answers_question_id="OQ-9999")
    line = provenance_line(claim, (_question("OQ-0001"),))
    assert "answers OQ-9999" in line
    assert "(" not in line.split("status")[0]  # no title parenthetical


# --------------------------------------------------------------------------
# render_evidence_reader -- the full-body composer
# --------------------------------------------------------------------------


def test_render_evidence_reader_lays_out_all_sections() -> None:
    """The composed body carries the title, sources, conflicts, and provenance."""
    claim = _claim(
        description="SABR beats SVI on the front of the curve",
        evidence_refs=["docs/sabr.md", "docs/fit.md"],
        answers_question_id="OQ-0001",
        source_artifact_id="ART-42",
    )
    conflicts = (_claim("CL-0002", status=ClaimStatus.REFUTED),)
    body = render_evidence_reader(claim, (_question(),), conflicts)
    assert "Short tenor is best fit by SABR" in body  # the title
    assert "SABR beats SVI on the front of the curve" in body  # the description
    assert "supporting sources:" in body
    assert "[1] docs/sabr.md" in body
    assert "[2] docs/fit.md" in body
    assert "conflicts:" in body
    assert "refuted -- Short tenor is best fit by SABR" in body
    assert "provenance:" in body
    assert "answers OQ-0001" in body
    assert "from ART-42" in body


def test_render_evidence_reader_honest_empty_sources_and_conflicts() -> None:
    """A bare claim renders the honest no-sources + ``none`` conflict lines."""
    body = render_evidence_reader(_claim(), (), ())
    assert NO_SOURCES_NOTICE in body
    assert f"  {NO_CONFLICTS_NOTICE}" in body


def test_render_evidence_reader_omits_description_when_absent() -> None:
    """A claim with no long-form body renders only its title above the sections."""
    body = render_evidence_reader(_claim(description=None), (), ())
    lines = body.splitlines()
    # The first line is the title; the next line is already the sources header,
    # so no blank description line is interposed.
    assert lines[0] == "Short tenor is best fit by SABR"
    assert lines[1] == "supporting sources:"
