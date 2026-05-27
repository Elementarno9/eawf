"""Unit tests for the C06 palette verb registry (P26-W19).

Covers the pure registry surface — scope/profile/runtime visibility
filtering, the fuzzy ranker + scorer, and the longest-match verb/arg
splitter — without mounting Textual. The palette overlay's Pilot-driven
behaviour lives in ``test_palette_command.py``.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.palette.verbs import (
    SCOPES_ALL,
    VERBS,
    PaletteVerb,
    fuzzy_score,
    rank_verbs,
    split_verb_args,
    visible_verbs,
)

# --------------------------------------------------------------------------
# Registry integrity
# --------------------------------------------------------------------------


def test_registry_names_are_unique() -> None:
    names = [v.name for v in VERBS]
    assert len(names) == len(set(names))


def test_registry_every_verb_has_a_handler() -> None:
    assert all(callable(v.handler) for v in VERBS)


def test_registry_every_name_starts_with_slash() -> None:
    assert all(v.name.startswith("/") for v in VERBS)


# --------------------------------------------------------------------------
# visible_verbs — scope / profile / runtime gates
# --------------------------------------------------------------------------


def test_visible_verbs_repo_includes_cross_screen_verbs() -> None:
    names = {v.name for v in visible_verbs("repo")}
    assert "/find" in names  # SCOPES_ALL
    assert "/theme" in names  # SCOPES_ALL
    assert "/init" in names  # SCOPES_ALL


def test_visible_verbs_user_hides_removed_placeholder_verbs() -> None:
    names = {v.name for v in visible_verbs("user")}
    assert "/wave open" not in names
    assert "/switch" not in names
    assert "/find" in names  # cross-screen


def test_visible_verbs_placeholder_verbs_removed() -> None:
    names = {v.name for v in VERBS}
    assert {
        "/switch",
        "/wave",
        "/wave open",
        "/wave log",
        "/wave state",
        "/wave report",
        "/wave criteria",
        "/wave deps",
        "/wave events",
        "/wave dispatch",
        "/wt",
        "/prep",
        "/flow",
        "/research",
        "/spike",
        "/design",
        "/ship",
        "/review",
        "/polish",
    }.isdisjoint(names)


def test_visible_verbs_profile_gate_hides_research_verbs_by_default() -> None:
    names = {v.name for v in visible_verbs("repo")}
    assert "/spike" not in names
    assert "/design" not in names


def test_visible_verbs_profile_gate_keeps_removed_research_verbs_hidden() -> None:
    names = {v.name for v in visible_verbs("repo", profiles={"research"})}
    assert "/spike" not in names
    assert "/design" not in names


def test_visible_verbs_runtime_gate_empty_runtime_keeps_ungated() -> None:
    # No registry verb is runtime-gated yet; an empty runtime must not
    # accidentally drop ungated verbs.
    assert {v.name for v in visible_verbs("repo")} == {
        v.name for v in visible_verbs("repo", runtime="")
    }


def test_visible_verbs_preserves_registry_order() -> None:
    repo = visible_verbs("repo")
    order_in_registry = [v.name for v in VERBS if v in repo]
    assert [v.name for v in repo] == order_in_registry


# --------------------------------------------------------------------------
# fuzzy_score — subsequence matcher
# --------------------------------------------------------------------------


def test_fuzzy_score_empty_needle_is_zero() -> None:
    assert fuzzy_score("", "/find") == 0


def test_fuzzy_score_non_subsequence_is_none() -> None:
    assert fuzzy_score("zzz", "/find") is None


def test_fuzzy_score_contiguous_beats_scattered() -> None:
    contiguous = fuzzy_score("/th", "/theme")
    scattered = fuzzy_score("/te", "/theme")
    assert contiguous is not None and scattered is not None
    assert contiguous < scattered


def test_fuzzy_score_is_case_insensitive() -> None:
    assert fuzzy_score("/FIND", "/find") == fuzzy_score("/find", "/find")


# --------------------------------------------------------------------------
# rank_verbs — filter + best-first ordering
# --------------------------------------------------------------------------


def test_rank_verbs_empty_needle_returns_all_in_input_order() -> None:
    ranked = rank_verbs(VERBS, "")
    assert [v.name for v in ranked] == [v.name for v in VERBS]


def test_rank_verbs_filters_to_matches() -> None:
    ranked = rank_verbs(VERBS, "/theme")
    assert [v.name for v in ranked] == ["/theme"]


def test_rank_verbs_best_match_first() -> None:
    ranked = rank_verbs(visible_verbs("repo"), "/theme")
    assert ranked
    assert ranked[0].name == "/theme"


def test_rank_verbs_no_match_returns_empty() -> None:
    assert rank_verbs(VERBS, "/zzzzz") == []


# --------------------------------------------------------------------------
# split_verb_args — longest-match verb/arg split
# --------------------------------------------------------------------------


def test_split_verb_args_bare_verb_no_args() -> None:
    name, args = split_verb_args("/find")
    assert name == "/find"
    assert args == ""


def test_split_verb_args_single_token_verb_with_args() -> None:
    name, args = split_verb_args("/find phase 26")
    assert name == "/find"
    assert args == "phase 26"


def test_split_verb_args_unknown_verb_splits_first_token() -> None:
    name, args = split_verb_args("/nope here be args")
    assert name == "/nope"
    assert args == "here be args"


# --------------------------------------------------------------------------
# PaletteVerb — frozen dataclass contract
# --------------------------------------------------------------------------


def test_palette_verb_is_frozen() -> None:
    verb = VERBS[0]
    with pytest.raises((AttributeError, TypeError)):
        verb.name = "/mutated"  # type: ignore[misc]


def test_palette_verb_defaults() -> None:
    verb = PaletteVerb("/x", "hint", lambda app, args: None, SCOPES_ALL)
    assert verb.requires_profile == ()
    assert verb.requires_runtime == ()
    assert verb.args_grammar == ""
