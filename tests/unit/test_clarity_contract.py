"""P29-I07-W01: the doc-clarity glossary, internal-code blocklist, and the
``clarity-contract`` render block in the always-enabled ``core`` profile.

Covers the Layer-0 "define the standard" deliverable:

- :mod:`eawf.platform.profiles.clarity` — the queryable glossary
  (:data:`APPROVED_TERMS`), the internal-code blocklist
  (:data:`INTERNAL_CODE_BLOCKLIST` + :func:`internal_codes_in`), the
  commit-prefix exemption, and the six newcomer-test dimensions. The
  prose lints in later waves read this typed data, so the boundary and
  error paths are pinned here.
- The ``clarity-contract`` render block ships on the ``core`` profile and
  lands in the rendered AGENTS.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eawf.platform.profiles import compose, load_profile
from eawf.platform.profiles.clarity import (
    APPROVED_TERMS,
    COMMIT_SUBJECT_PREFIX_EXEMPT,
    INTERNAL_CODE_BLOCKLIST,
    NEWCOMER_TEST,
    NEWCOMER_TEST_DIMENSIONS,
    ClarityDimension,
    InternalCodePattern,
    internal_codes_in,
    is_approved_term,
)
from eawf.surfaces.render.agents_md import render_agents_md
from eawf.surfaces.render.manifest import Manifest

_CLARITY_BLOCK_ID = "clarity-contract"


# --- glossary ---------------------------------------------------------------


def test_approved_terms_are_sorted_and_unique() -> None:
    """The glossary stays sorted (diff hygiene) and carries no duplicates."""
    assert list(APPROVED_TERMS) == sorted(APPROVED_TERMS)
    assert len(set(APPROVED_TERMS)) == len(APPROVED_TERMS)


def test_is_approved_term_membership_case_insensitive() -> None:
    assert is_approved_term("wave") is True
    assert is_approved_term("Wave") is True
    assert is_approved_term("WAVE") is True


def test_is_approved_term_rejects_non_member() -> None:
    """A token that is not in the glossary is not approved."""
    assert is_approved_term("clusterfoo") is False
    assert is_approved_term("") is False


# --- newcomer-test dimensions -----------------------------------------------


def test_newcomer_test_is_a_question_mentioning_state_json() -> None:
    """The single gate names the state.json escape hatch a newcomer lacks."""
    assert NEWCOMER_TEST.endswith("?")
    assert "state.json" in NEWCOMER_TEST


def test_newcomer_dimensions_have_unique_keys() -> None:
    keys = [d.key for d in NEWCOMER_TEST_DIMENSIONS]
    assert len(set(keys)) == len(keys)
    assert all(isinstance(d, ClarityDimension) for d in NEWCOMER_TEST_DIMENSIONS)


def test_description_blocking_dimensions_are_why_and_not_duplicate() -> None:
    """``why_present`` and ``not_a_title_duplicate`` block the description surface."""
    blocking = {d.key for d in NEWCOMER_TEST_DIMENSIONS if d.blocking_for_description}
    assert blocking == {"why_present", "not_a_title_duplicate"}


def test_clarity_dimension_rejects_overlong_label() -> None:
    """The label bound (72) fires at the model boundary."""
    with pytest.raises(ValueError, match="label"):
        ClarityDimension(key="k", label="x" * 73)


# --- internal-code blocklist ------------------------------------------------


def test_blocklist_codes_are_unique() -> None:
    codes = [entry.code for entry in INTERNAL_CODE_BLOCKLIST]
    assert len(set(codes)) == len(codes)
    assert all(isinstance(e, InternalCodePattern) for e in INTERNAL_CODE_BLOCKLIST)


def test_blocklist_patterns_are_compiled() -> None:
    """Every blocklist row carries a compiled regex matcher."""
    for entry in INTERNAL_CODE_BLOCKLIST:
        assert isinstance(entry.pattern, re.Pattern)


@pytest.mark.parametrize(
    "token",
    ["P29", "I07", "W01", "C08", "D17", "D-SUP-01", "H03-12", "SWITCH_MANUAL", "EAWF_DAEMONLESS"],
)
def test_internal_codes_detected(token: str) -> None:
    """Each canonical internal-code family is recognised in prose."""
    found = internal_codes_in(f"see {token} for context")
    assert token in found


def test_internal_codes_in_order_of_appearance() -> None:
    """Multiple codes surface in order, no double-count at one position."""
    found = internal_codes_in("first W01 then D17 then SWITCH_MANUAL")
    assert found == ["W01", "D17", "SWITCH_MANUAL"]


def test_internal_codes_empty_when_clean() -> None:
    """Prose with no internal code yields an empty list (boundary case)."""
    assert internal_codes_in("a perfectly clear sentence for a newcomer") == []


def test_internal_codes_does_not_fire_midword() -> None:
    """A word-bounded pattern does not match inside a larger identifier."""
    # ``shipped`` / ``WIP01abc`` must not register as lifecycle ids.
    assert internal_codes_in("the shipped WIP01abc handler") == []


def test_commit_prefixes_are_exempt_and_not_internal_codes() -> None:
    """Conventional-commit type prefixes are exempt; they are not flagged."""
    assert "feat" in COMMIT_SUBJECT_PREFIX_EXEMPT
    assert "state" in COMMIT_SUBJECT_PREFIX_EXEMPT
    # A commit-subject prefix token is not an internal code.
    assert internal_codes_in("feat: add the thing") == []


def test_internal_code_pattern_rejects_uncompilable_source() -> None:
    """A malformed regex source fails at the model boundary."""
    with pytest.raises(ValueError, match="un-compilable"):
        InternalCodePattern(code="bad", label="bad pattern", pattern="(unterminated")


def test_internal_code_pattern_rejects_non_pattern_type() -> None:
    """A non-str / non-Pattern value is rejected."""
    with pytest.raises(ValueError, match="must be str or re"):
        InternalCodePattern(code="bad", label="bad pattern", pattern=123)


# --- clarity-contract render block ------------------------------------------


def test_core_profile_carries_clarity_contract_block() -> None:
    """The ``core`` profile body declares the clarity-contract render block."""
    core = load_profile("core")
    ids = {b.id for b in core.render_blocks}
    assert _CLARITY_BLOCK_ID in ids


def test_clarity_contract_block_targets_agents_md() -> None:
    core = load_profile("core")
    block = next(b for b in core.render_blocks if b.id == _CLARITY_BLOCK_ID)
    assert block.target == "AGENTS.md"
    assert "newcomer test" in block.body_template.casefold()


def test_clarity_contract_renders_into_agents_md(tmp_path: Path) -> None:
    """The clarity-contract block lands in the rendered AGENTS.md text."""
    composed = compose([load_profile("core")])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))
    text = target.read_text(encoding="utf-8")
    assert "Doc-clarity contract" in text
    assert "newcomer test" in text.casefold()
    # The block names the blocklist families a newcomer-facing artifact must gloss.
    assert "H<NN>-<NN>" in text
    assert "SWITCH_*" in text
    # And points at the typed source the lints read.
    assert "eawf.platform.profiles.clarity" in text
