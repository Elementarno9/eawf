"""Tests for the EAWF024 test-tier contract lint.

Covers the banned-import rule (a ``tests/unit/`` file must not import
``subprocess`` / ``textual`` / ``CliRunner``) across boundary and error
paths, the ``# noqa: EAWF024`` waiver, and the ``is_unit_tier_path``
dispatcher predicate. The check is content-only, so these tests feed it
source snippets as strings -- the module itself imports nothing banned
and is clean under its own rule.
"""

from __future__ import annotations

import pytest

from eawf.platform.lint.eawf024_test_tier_contract import (
    RULE_CODE,
    TierViolation,
    check_source,
    is_unit_tier_path,
)


def test_check_source_flags_plain_subprocess_import() -> None:
    findings = check_source("import subprocess\n")
    assert [f.imported for f in findings] == ["subprocess"]
    assert findings[0].lineno == 1
    assert findings[0].code == RULE_CODE


def test_check_source_flags_from_subprocess_import() -> None:
    findings = check_source("from subprocess import run\n")
    assert [f.imported for f in findings] == ["subprocess"]


def test_check_source_flags_plain_textual_import() -> None:
    findings = check_source("import textual\n")
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_flags_dotted_textual_import() -> None:
    findings = check_source("from textual.widgets import Button\n")
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_flags_clirunner_name_import() -> None:
    findings = check_source("from typer.testing import CliRunner\n")
    assert [f.imported for f in findings] == ["CliRunner"]


def test_check_source_flags_multiple_sorted_by_position() -> None:
    source = "import subprocess\nimport textual\nfrom typer.testing import CliRunner\n"
    findings = check_source(source)
    assert [(f.lineno, f.imported) for f in findings] == [
        (1, "subprocess"),
        (2, "textual"),
        (3, "CliRunner"),
    ]


def test_check_source_clean_import_yields_no_finding() -> None:
    # A single allowed import (boundary: exactly one non-banned import).
    assert check_source("import json\n") == []


def test_check_source_empty_source_yields_no_finding() -> None:
    # Boundary: empty file.
    assert check_source("") == []


def test_check_source_string_literal_mentioning_subprocess_not_flagged() -> None:
    # The AST scan inspects import statements only; a string that merely
    # names subprocess must never false-fire.
    assert check_source('X = "we call subprocess here"\n') == []


def test_check_source_noqa_waiver_clears_violation() -> None:
    findings = check_source("import subprocess  # noqa: EAWF024 deliberate fixture\n")
    assert findings == []


def test_check_source_noqa_waiver_only_clears_marked_line() -> None:
    source = "import subprocess  # noqa: EAWF024\nimport textual\n"
    findings = check_source(source)
    assert [f.imported for f in findings] == ["textual"]


def test_check_source_raises_on_unparseable() -> None:
    with pytest.raises(SyntaxError):
        check_source("def (:\n")


def test_render_contains_code_and_token() -> None:
    violation = TierViolation(lineno=3, col_offset=0, imported="textual")
    rendered = violation.render()
    assert RULE_CODE in rendered
    assert "textual" in rendered
    assert rendered.startswith("3:0:")


def test_is_unit_tier_path_accepts_unit_python_file() -> None:
    assert is_unit_tier_path("tests/unit/test_x.py")
    assert is_unit_tier_path("tests/unit/sub/test_y.py")


def test_is_unit_tier_path_rejects_other_tiers_and_non_python() -> None:
    assert not is_unit_tier_path("tests/integration/test_x.py")
    assert not is_unit_tier_path("tests/tui/test_x.py")
    assert not is_unit_tier_path("tests/unit/data.json")
    assert not is_unit_tier_path("src/eawf/foo.py")


def test_is_unit_tier_path_folds_backslashes() -> None:
    assert is_unit_tier_path("tests\\unit\\test_x.py")


def test_is_unit_tier_path_accepts_absolute_path() -> None:
    assert is_unit_tier_path("/tmp/repo/tests/unit/test_x.py")
    assert not is_unit_tier_path("/tmp/repo/tests/integration/test_x.py")
