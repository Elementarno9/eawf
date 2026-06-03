"""Tests for the EAWF016 entity-title-clarity lint.

Covers each title rule (a flagging case and a clean case), the
false-positive guards for the deliberately-dropped leading-``[A-Z]\\d`` rule
(legitimate ``B0NN`` / ``V1`` / ``C++`` shapes), the ``assert_title_clarity``
mutation-boundary helper, and the ``state.json`` delta scan.
"""

from __future__ import annotations

import pytest

from eawf.platform.lint.eawf016_title_clarity import (
    RULE_CODE,
    assert_title_clarity,
    check_state_title_lines,
    check_title,
)


def _reasons(title: str) -> list[str]:
    return [v.reason for v in check_title(title)]


# ---- clean titles -----------------------------------------------------------


def test_check_title_accepts_imperative_noun_phrase() -> None:
    assert check_title("Add title-clarity lint") == []


def test_check_title_accepts_two_plus_joined_tokens() -> None:
    # A single conjunction reads fine; cluster-soup starts at three +-joins.
    assert check_title("Render changelog + tag release") == []
    assert check_title("A+B") == []


def test_check_title_code_property_is_eawf016() -> None:
    violations = check_title("feat: x")
    assert violations[0].code == RULE_CODE == "EAWF016"


# ---- conventional-commit prefix rule ---------------------------------------


def test_check_title_flags_conventional_commit_prefix() -> None:
    reasons = _reasons("feat: add the title lint")
    assert any("conventional-commit type prefix" in r for r in reasons)


def test_check_title_flags_state_prefix() -> None:
    reasons = _reasons("state: close iter")
    assert any("conventional-commit type prefix" in r for r in reasons)


def test_check_title_accepts_word_that_merely_starts_like_a_type() -> None:
    # "features" is not the "feat:" token; no colon-delimited prefix.
    assert check_title("Features behind a flag") == []


# ---- cluster-soup rule ------------------------------------------------------


def test_check_title_flags_three_plus_joined_tokens() -> None:
    reasons = _reasons("spawn+orchestrator+jury wiring")
    assert any("cluster-code" in r for r in reasons)


def test_check_title_carves_out_cpp() -> None:
    # The C++ carve-out keeps a legitimate language name from tripping soup.
    assert check_title("Add C++ parser support") == []


# ---- bare-id-only rule ------------------------------------------------------


def test_check_title_flags_bare_wave_id() -> None:
    reasons = _reasons("W02")
    assert any("bare id" in r for r in reasons)


def test_check_title_flags_bracketed_decision_id() -> None:
    reasons = _reasons("[D17]")
    assert any("bare id" in r for r in reasons)


def test_check_title_flags_bare_id_with_lowercase_suffix() -> None:
    reasons = _reasons("P29a")
    assert any("bare id" in r for r in reasons)


# ---- dropped leading-code rule: false-positive guards ----------------------


def test_check_title_accepts_id_followed_by_words() -> None:
    # The dropped rule would have flagged a leading code; a real label that
    # merely starts with one is clean (it is not a bare id).
    assert check_title("C06 surfaces render the roadmap") == []
    assert check_title("Iter1 bootstrap helper") == []


def test_check_title_accepts_legitimate_backlog_and_decision_shapes_in_prose() -> None:
    # B0NN backlog refs and V1 decision codes inside a descriptive title are
    # not bare-id-only, so they pass (this is the F5 trap the design drops).
    assert check_title("Backfill B027 titles in the migration") == []
    assert check_title("Adopt V1 of the audit DSL") == []


# ---- reused over-cap / trailing-period checks ------------------------------


def test_check_title_flags_over_cap() -> None:
    reasons = _reasons("x" * 73)
    assert any("72-char cap" in r for r in reasons)


def test_check_title_flags_trailing_period() -> None:
    reasons = _reasons("Add a thing.")
    assert any("trailing period" in r for r in reasons)


def test_check_title_collects_multiple_findings() -> None:
    # A title can trip several rules at once; all are surfaced.
    reasons = _reasons("feat: a.")
    assert any("conventional-commit type prefix" in r for r in reasons)
    assert any("trailing period" in r for r in reasons)


# ---- assert_title_clarity ---------------------------------------------------


def test_assert_title_clarity_is_a_noop_for_clean_titles() -> None:
    assert assert_title_clarity("Add the gate", entity_kind="wave", entity_id="W01") is None


def test_assert_title_clarity_raises_with_entity_context() -> None:
    with pytest.raises(ValueError, match="wave 'P01-I01-W01' title"):
        assert_title_clarity("feat: x", entity_kind="wave", entity_id="P01-I01-W01")


def test_assert_title_clarity_message_names_every_failed_rule() -> None:
    with pytest.raises(ValueError) as excinfo:
        assert_title_clarity("feat: x.", entity_kind="decision", entity_id="D17")
    message = str(excinfo.value)
    assert "conventional-commit type prefix" in message
    assert "trailing period" in message


# ---- state.json delta scan --------------------------------------------------


def test_check_state_title_lines_flags_only_bad_added_titles() -> None:
    added = [
        (10, '        "title": "feat: bad label",'),
        (20, '        "title": "Clean descriptive label",'),
        (30, '        "status": "pending",'),
    ]
    findings = check_state_title_lines(added)
    assert len(findings) == 1
    assert findings[0].lineno == 10
    assert findings[0].code == "EAWF016"
    assert "conventional-commit type prefix" in findings[0].reason


def test_check_state_title_lines_ignores_non_title_lines() -> None:
    added = [
        (5, '        "id": "P29-I07-W02",'),
        (6, '        "scope_id": "EAWF",'),
    ]
    assert check_state_title_lines(added) == []


def test_check_state_title_lines_handles_escaped_quotes_in_title() -> None:
    # A title value carrying an escaped quote decodes before the rules run.
    added = [(7, '        "title": "Use \\"vector\\" index W02",')]
    # Not a bare id (has words), no cc-prefix, no soup -> clean.
    assert check_state_title_lines(added) == []


def test_check_state_title_lines_renders_line_and_code() -> None:
    added = [(42, '        "title": "W02",')]
    findings = check_state_title_lines(added)
    assert len(findings) == 1
    rendered = findings[0].render()
    assert rendered.startswith("42:0: EAWF016 ")
    assert "bare id" in rendered
