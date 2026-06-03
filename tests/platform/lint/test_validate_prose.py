"""Tests for the ``validate_prose`` Layer-2 prose chokepoint.

Pins the chokepoint contract (see ``.ea/local/research/2026-05-29-doc-clarity.md``
"Layer 2" + the "Prose coverage DAGs" stability model):

- it AGGREGATES findings from the composed deterministic lints (EAWF013
  bracket-position, EAWF014 no-manual-wrap, EAWF017 inline-reference) plus any
  injected Vale rows;
- **fail-open** (``strict=False``) returns ``ok`` / exit 0 even on a finding;
- **fail-closed** (``strict=True``) returns not-``ok`` / exit non-zero on a
  finding;
- a known-bad Markdown is rejected in strict mode and a clean one passes both
  modes;
- the Vale leg is injected (the chokepoint never shells out), so an absent /
  empty Vale leg still leaves the deterministic legs blocking in strict mode.
"""

from __future__ import annotations

from eawf.platform.lint.validate_prose import (
    COMPOSED_RULES,
    ProseFinding,
    ProseReport,
    validate_prose,
)

# --- corpus: each shape trips exactly one composed deterministic lint --------

# EAWF017 — a bare inline URL in prose.
_BAD_EAWF017_URL = "Fixed the jitter, see https://example.org/jitter for details.\n"

# EAWF017 — three inline path:line refs in one block.
_BAD_EAWF017_SOUP = (
    "Edited src/eawf/a.py:10 and src/eawf/b.py:20 plus src/eawf/c.py:30 to fix it.\n"
)

# EAWF013 — a citation marker detached after sentence punctuation.
_BAD_EAWF013 = "The claim holds. [1]\n"

# EAWF014 — two plain-prose lines where the first does not end a sentence, so
# the pair looks manually wrapped.
_BAD_EAWF014 = (
    "This paragraph is split across two lines and the first line\n"
    "does not end a sentence.\n"
)

# A clean artifact: refs live in a ## References table, no bare URL, no wrap.
_CLEAN = (
    "Raised the runner budget after continuous-integration jitter flagged false failures [a].\n"
    "\n"
    "## References\n"
    "\n"
    "[a] `src/eawf/observability/perf.py:142`\n"
)


# --- aggregation -------------------------------------------------------------


def test_validate_prose_aggregates_eawf017_url() -> None:
    report = validate_prose(_BAD_EAWF017_URL)
    assert report.has_findings
    assert "EAWF017" in report.codes()


def test_validate_prose_aggregates_eawf017_soup() -> None:
    report = validate_prose(_BAD_EAWF017_SOUP)
    assert "EAWF017" in report.codes()


def test_validate_prose_aggregates_eawf013() -> None:
    report = validate_prose(_BAD_EAWF013)
    assert "EAWF013" in report.codes()


def test_validate_prose_aggregates_eawf014() -> None:
    report = validate_prose(_BAD_EAWF014)
    assert "EAWF014" in report.codes()


def test_validate_prose_aggregates_multiple_lints_in_one_pass() -> None:
    # One source that trips both the bracket lint and the inline-URL lint: the
    # chokepoint reports both codes from the single composed pass.
    source = "The claim holds. [1]\nA second line, see https://example.org/x here.\n"
    report = validate_prose(source)
    assert {"EAWF013", "EAWF017"} <= report.codes()


def test_validate_prose_composed_rules_constant_lists_the_deterministic_legs() -> None:
    assert COMPOSED_RULES == ("EAWF013", "EAWF014", "EAWF017")


def test_validate_prose_findings_sorted_by_position() -> None:
    source = "A second line, see https://example.org/x here.\nThe claim holds. [1]\n"
    report = validate_prose(source)
    linenos = [f.lineno for f in report.findings if f.code != "VALE"]
    assert linenos == sorted(linenos)


# --- fail-open (local / in-skill) --------------------------------------------


def test_validate_prose_fail_open_ok_on_finding() -> None:
    report = validate_prose(_BAD_EAWF017_URL, strict=False)
    assert report.has_findings
    assert report.ok is True
    assert report.exit_code == 0


def test_validate_prose_default_mode_is_fail_open() -> None:
    # The default (no strict kwarg) must be the fail-open local contract.
    report = validate_prose(_BAD_EAWF013)
    assert report.strict is False
    assert report.exit_code == 0


# --- fail-closed (strict / CI) -----------------------------------------------


def test_validate_prose_fail_closed_not_ok_on_finding() -> None:
    report = validate_prose(_BAD_EAWF017_URL, strict=True)
    assert report.has_findings
    assert report.ok is False
    assert report.exit_code == 1


def test_validate_prose_known_bad_rejected_in_strict_mode() -> None:
    # The acceptance criterion: a known-bad markdown is rejected by the
    # chokepoint in strict mode.
    for bad in (_BAD_EAWF017_URL, _BAD_EAWF017_SOUP, _BAD_EAWF013, _BAD_EAWF014):
        report = validate_prose(bad, strict=True)
        assert report.exit_code == 1, bad


def test_validate_prose_clean_passes_both_modes() -> None:
    # A clean artifact passes fail-open AND fail-closed.
    for strict in (False, True):
        report = validate_prose(_CLEAN, strict=strict)
        assert not report.has_findings
        assert report.ok is True
        assert report.exit_code == 0


def test_validate_prose_empty_source_is_clean() -> None:
    # Boundary: empty input has nothing to flag in either mode.
    assert validate_prose("", strict=True).exit_code == 0
    assert validate_prose("\n\n", strict=True).exit_code == 0


# --- Vale leg (injected; chokepoint never shells out) ------------------------


def test_validate_prose_folds_injected_vale_rows() -> None:
    row = "  note.md:3:5: warning Google.Weasel Weasel word"
    report = validate_prose(_CLEAN, strict=False, vale_rows=(row,))
    assert report.has_findings
    assert "VALE" in report.codes()


def test_validate_prose_vale_rows_block_in_strict_mode() -> None:
    row = "  note.md:3:5: warning Google.Weasel Weasel word"
    report = validate_prose(_CLEAN, strict=True, vale_rows=(row,))
    assert report.exit_code == 1


def test_validate_prose_empty_vale_leg_still_blocks_deterministic_in_strict() -> None:
    # The fail-open-vale stability property: even with NO vale rows (binary
    # absent / unsynced styles), a deterministic finding still blocks strict.
    report = validate_prose(_BAD_EAWF013, strict=True, vale_rows=())
    assert report.exit_code == 1
    assert "EAWF013" in report.codes()


def test_validate_prose_clean_with_empty_vale_leg_passes_strict() -> None:
    report = validate_prose(_CLEAN, strict=True, vale_rows=())
    assert report.exit_code == 0


# --- ProseReport / ProseFinding rendering ------------------------------------


def test_prose_report_render_clean_notes_mode() -> None:
    assert validate_prose(_CLEAN, strict=True).render() == "validate_prose: clean (strict)"
    assert validate_prose(_CLEAN, strict=False).render() == "validate_prose: clean (advisory)"


def test_prose_report_render_lists_findings() -> None:
    rendered = validate_prose(_BAD_EAWF017_URL, strict=True).render()
    assert "finding(s) (strict)" in rendered
    assert "EAWF017" in rendered


def test_prose_finding_render_with_snippet() -> None:
    finding = ProseFinding(code="EAWF013", lineno=4, col_offset=2, reason="bad", snippet="x")
    assert finding.render() == "EAWF013 4:2 bad: 'x'"


def test_prose_finding_render_without_snippet() -> None:
    finding = ProseFinding(code="VALE", lineno=0, col_offset=0, reason="weasel word")
    assert finding.render() == "VALE 0:0 weasel word"


def test_prose_report_codes_distinct() -> None:
    report = ProseReport(
        findings=(
            ProseFinding(code="EAWF013", lineno=1, col_offset=0, reason="a"),
            ProseFinding(code="EAWF013", lineno=2, col_offset=0, reason="b"),
            ProseFinding(code="EAWF017", lineno=3, col_offset=0, reason="c"),
        ),
        strict=True,
    )
    assert report.codes() == {"EAWF013", "EAWF017"}
