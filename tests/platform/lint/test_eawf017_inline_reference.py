"""Tests for the EAWF017 inline-reference-tabulation lint.

Covers the two rules (inline bare URL, more than two inline ``path:line``
references per block) with a flagging and a clean case each, the block
boundary (two refs per paragraph clean, three in one paragraph flagged), the
exemptions (fenced code, the ``## References`` block, reference rows,
inline-code spans), and the ``assert_inline_references`` text-surface helper.
"""

from __future__ import annotations

import pytest

from eawf.platform.lint.eawf017_inline_reference import (
    MAX_INLINE_PATH_REFS,
    RULE_CODE,
    assert_inline_references,
    check_source,
)


def _reasons(source: str) -> list[str]:
    return [v.reason for v in check_source(source)]


# ---- inline bare URL rule ---------------------------------------------------


def test_check_source_flags_inline_bare_url() -> None:
    reasons = _reasons("Fixed the jitter, see https://example.org/jitter for details.\n")
    assert any("inline bare URL" in r for r in reasons)


def test_check_source_strips_trailing_punctuation_from_url_snippet() -> None:
    violations = check_source("See https://example.org/x.\n")
    assert violations[0].snippet == "https://example.org/x"


def test_check_source_clean_when_no_inline_url() -> None:
    assert check_source("Raised the runner budget after CI jitter flagged false failures.\n") == []


def test_check_source_url_in_inline_code_is_exempt() -> None:
    # A URL inside backticks is a code span, not running prose.
    assert check_source("Set the endpoint to `https://example.org/api` in config.\n") == []


def test_check_source_code_property_is_eawf017() -> None:
    violations = check_source("See https://example.org/x for more.\n")
    assert violations[0].code == RULE_CODE == "EAWF017"


# ---- reference-soup rule ----------------------------------------------------


def test_check_source_flags_three_inline_path_refs_in_one_block() -> None:
    source = (
        "Edited src/eawf/a.py:10 and the table in src/eawf/b.py:20 plus "
        "src/eawf/c.py:30 to fix it.\n"
    )
    reasons = _reasons(source)
    assert any("inline path:line refs" in r for r in reasons)


def test_check_source_two_inline_path_refs_is_clean() -> None:
    # Exactly two reads fine; the third is the first violation.
    source = "Edited src/eawf/a.py:10 and src/eawf/b.py:20 to fix it.\n"
    assert check_source(source) == []


def test_check_source_two_refs_per_block_across_blocks_is_clean() -> None:
    # Two refs in one paragraph and two in the next are both under the cap.
    source = (
        "Edited src/eawf/a.py:10 and src/eawf/b.py:20 here.\n"
        "\n"
        "Then src/eawf/c.py:30 and src/eawf/d.py:40 there.\n"
    )
    assert check_source(source) == []


def test_check_source_soup_finding_anchored_to_over_limit_line() -> None:
    source = (
        "First line src/eawf/a.py:10.\n"
        "Second line src/eawf/b.py:20.\n"
        "Third line src/eawf/c.py:30.\n"
    )
    violations = [v for v in check_source(source) if "inline path:line refs" in v.reason]
    assert len(violations) == 1
    # The third (over-limit) reference is on line 3.
    assert violations[0].lineno == 3


def test_check_source_path_refs_in_inline_code_are_exempt() -> None:
    # Three path:line refs but all inside code spans -> not prose -> clean.
    source = "See `src/eawf/a.py:10`, `src/eawf/b.py:20`, and `src/eawf/c.py:30`.\n"
    assert check_source(source) == []


def test_max_inline_path_refs_is_two() -> None:
    assert MAX_INLINE_PATH_REFS == 2


# ---- exemptions: fenced code, references block, reference rows --------------


def test_check_source_ignores_fenced_code() -> None:
    source = (
        "```\n"
        "see https://example.org/x and src/a.py:1, src/b.py:2, src/c.py:3\n"
        "```\n"
    )
    assert check_source(source) == []


def test_check_source_ignores_references_section() -> None:
    # The whole ## References block is path/URL dense by design and exempt.
    source = (
        "A clean claim [1][2][3].\n"
        "\n"
        "## References\n"
        "\n"
        "[1] `src/eawf/a.py:10`\n"
        "[2] `src/eawf/b.py:20`\n"
        "[3] https://example.org/x\n"
    )
    assert check_source(source) == []


def test_check_source_ignores_table_reference_rows() -> None:
    # The brief's worked-example table form: ``| [a] | label | target |``.
    source = (
        "## References\n"
        "\n"
        "| # | Label | Target |\n"
        "| --- | --- | --- |\n"
        "| [a] | perf | `src/eawf/a.py:10` |\n"
        "| [b] | jitter | https://example.org/x |\n"
    )
    assert check_source(source) == []


def test_check_source_references_section_ends_at_next_heading() -> None:
    # After ## References ends (a new ## heading), prose linting resumes.
    source = (
        "## References\n"
        "\n"
        "[1] `src/eawf/a.py:10`\n"
        "\n"
        "## Appendix\n"
        "\n"
        "A stray inline URL https://example.org/leak here.\n"
    )
    reasons = _reasons(source)
    assert any("inline bare URL" in r for r in reasons)


# ---- assert_inline_references ----------------------------------------------


def test_assert_inline_references_is_a_noop_for_clean_text() -> None:
    clean = "A clean sentence with no refs.\n"
    assert assert_inline_references(clean, surface="commit body") is None


def test_assert_inline_references_raises_with_surface_context() -> None:
    with pytest.raises(ValueError, match="commit body fails inline-reference"):
        assert_inline_references("See https://example.org/x for details.\n", surface="commit body")


def test_assert_inline_references_message_carries_finding() -> None:
    with pytest.raises(ValueError) as excinfo:
        assert_inline_references("See https://example.org/x.\n", surface="PR body")
    message = str(excinfo.value)
    assert "EAWF017" in message
    assert "inline bare URL" in message


# ---- render -----------------------------------------------------------------


def test_violation_render_shape() -> None:
    violations = check_source("See https://example.org/x for more.\n")
    rendered = violations[0].render()
    assert rendered.startswith("1:")
    assert "EAWF017" in rendered
    assert "inline bare URL" in rendered
