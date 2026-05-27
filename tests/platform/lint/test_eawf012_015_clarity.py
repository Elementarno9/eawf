"""Tests for EAWF012-EAWF015 clarity lint rules."""

from __future__ import annotations

import textwrap

from eawf.platform.lint.eawf012_design_provenance import check_source as check_012
from eawf.platform.lint.eawf013_bracket_position import check_source as check_013
from eawf.platform.lint.eawf014_no_manual_wrap import check_source as check_014
from eawf.platform.lint.eawf015_ears_advisory import check_source as check_015


def test_eawf012_flags_design_provenance_comments() -> None:
    source = textwrap.dedent(
        """
        def run() -> None:
            # per Q12 this branch is operator-approved
            pass
        """
    )
    violations = check_012(source)
    assert len(violations) == 1
    assert violations[0].code == "EAWF012"
    assert violations[0].lineno == 3


def test_eawf012_flags_design_provenance_docstrings() -> None:
    source = textwrap.dedent(
        '''
        """per Q12 this branch is operator-approved."""

        def run() -> None:
            # Explain why the branch exists.
            pass
        '''
    )
    violations = check_012(source)
    assert len(violations) == 1
    assert violations[0].code == "EAWF012"
    assert violations[0].lineno == 2


def test_eawf012_ignores_ordinary_docstrings_and_comments() -> None:
    source = textwrap.dedent(
        '''
        """Explain module behavior without implementation provenance."""

        def run() -> None:
            # Explain why the branch exists.
            pass
        '''
    )
    assert check_012(source) == []


def test_eawf013_flags_post_punctuation_citation() -> None:
    markdown = "The claim is already over. [1]\n\n## References\n\n[1] `src/example.py`\n"
    violations = check_013(markdown)
    assert len(violations) == 1
    assert violations[0].code == "EAWF013"
    assert "precede sentence punctuation" in violations[0].reason


def test_eawf013_accepts_dense_citation_and_reference_rows() -> None:
    markdown = "The claim is supported [1].\n\n## References\n\n[1] `src/example.py`\n"
    assert check_013(markdown) == []


def test_eawf014_flags_plain_paragraph_hard_wrap() -> None:
    markdown = textwrap.dedent(
        """
        This rendered paragraph was manually wrapped
        across two physical lines.
        """
    )
    violations = check_014(markdown)
    assert len(violations) == 1
    assert violations[0].code == "EAWF014"


def test_eawf014_ignores_lists_and_fences() -> None:
    markdown = textwrap.dedent(
        """
        - This list item may wrap in the editor
          and remain a nested continuation.

        ```
        This fenced block may wrap
        over two lines.
        ```
        """
    )
    assert check_014(markdown) == []


def test_eawf014_ignores_yaml_frontmatter() -> None:
    markdown = textwrap.dedent(
        """
        ---
        name: prep
        description: Activate the next PLANNED phase
        ---

        Body is a single rendered paragraph.
        """
    ).lstrip()
    assert check_014(markdown) == []


def test_eawf015_warns_on_non_ears_requirement_language() -> None:
    markdown = "The operator should review the output before merge.\n"
    advisories = check_015(markdown)
    assert len(advisories) == 1
    assert advisories[0].code == "EAWF015"


def test_eawf015_accepts_basic_ears_shape() -> None:
    markdown = "When a wave closes, the daemon shall persist the audit link.\n"
    assert check_015(markdown) == []
