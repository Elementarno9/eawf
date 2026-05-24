"""Hypothesis property tests for :mod:`eawf.platform.profiles.compose`.

Three invariants drive the suite:

1. **Idempotence** — feeding a previously composed profile back into
   :func:`compose` is a no-op for every field except the synthetic ``name``
   label (because re-composing changes ``a+b`` to ``(a+b)``-style suffix).
   We assert payload equality on the merge-bearing fields.

2. **Associativity for non-render-block fields** — for profiles that contain
   no render blocks, the order of inputs does not matter for composed output.
   This nails down the sorted-union / strictest-wins symmetry.

3. **Provenance completeness** — every populated top-level field on the
   composed profile traces back to at least one input profile name.
"""

from __future__ import annotations

from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.platform.profiles import (
    InstrumentReq,
    ProfileBody,
    StateExtensions,
    compose,
)

# Hypothesis strategies ------------------------------------------------------


_profile_name_strategy = st.from_regex(r"^[a-z][a-z0-9_]{0,7}$", fullmatch=True)
_state_key_strategy = st.from_regex(r"^[a-z][a-z0-9_]{0,5}$", fullmatch=True)
_instrument_name_strategy = st.from_regex(r"^[a-z][a-z0-9_-]{0,7}$", fullmatch=True)
_skill_strategy = st.from_regex(r"^[a-z][a-z0-9_-]{0,7}$", fullmatch=True)


_instrument_strategy = st.builds(
    InstrumentReq,
    name=_instrument_name_strategy,
    kind=st.sampled_from(["hard", "soft"]),
    probe=st.just("which"),
    version_args=st.just([]),
    version_regex=st.none(),
)


def _profile_strategy() -> st.SearchStrategy[ProfileBody]:
    """Build a render-block-free :class:`ProfileBody`.

    The associativity test requires render-block-free profiles (caller order
    locks block positions, breaking commutativity); the idempotence test
    reuses this same strategy so it exercises the merge logic for every
    field that *can* be associatively merged.
    """
    blocks_strategy = cast("st.SearchStrategy[list[object]]", st.just([]))
    return st.builds(
        ProfileBody,
        name=_profile_name_strategy,
        version=st.just("1.0"),
        description=st.just(""),
        state_extensions=st.builds(
            StateExtensions,
            fields_required=st.lists(_state_key_strategy, max_size=4, unique=True),
        ),
        instrument_requirements=st.lists(
            _instrument_strategy,
            max_size=3,
            unique_by=lambda req: req.name,
        ),
        render_blocks=blocks_strategy,
        skills_referenced=st.lists(_skill_strategy, max_size=4, unique=True),
        hooks_referenced=st.lists(_skill_strategy, max_size=4, unique=True),
    )


def _profile_list_strategy() -> st.SearchStrategy[list[ProfileBody]]:
    """1-3 unique-by-name profile bodies for the associativity tests."""
    return st.lists(
        _profile_strategy(),
        min_size=1,
        max_size=3,
        unique_by=lambda body: body.name,
    )


# Properties -----------------------------------------------------------------


@given(profiles=_profile_list_strategy())
@settings(max_examples=80, deadline=None)
def test_compose_idempotent(profiles: list[ProfileBody]) -> None:
    """``compose`` is idempotent on its merge-bearing fields.

    Re-feeding a composed profile (cast back to a ProfileBody-shaped input)
    into :func:`compose` produces identical merge output — only the label
    differs (``a+b`` vs ``(a+b)``).
    """
    once = compose(profiles)
    # Re-wrap the composed view as a single ProfileBody so it can be fed
    # back into compose. ProfileBody and ComposedProfile share every
    # contribution-bearing field; the audit-only maps on ComposedProfile
    # (``provenance``, ``override_audit``, ``conflict_warnings``) are
    # dropped here because ProfileBody is the upstream shape.
    fed_back = ProfileBody.model_validate(
        once.model_dump(
            mode="python",
            exclude={"provenance", "override_audit", "conflict_warnings"},
        ),
    )
    twice = compose([fed_back])

    # Compare the merge-bearing fields only — the synthetic ``name`` differs
    # because the second pass labels the merge as the wrapped profile's name.
    assert once.state_extensions == twice.state_extensions
    assert sorted(once.skills_referenced) == sorted(twice.skills_referenced)
    assert sorted(once.hooks_referenced) == sorted(twice.hooks_referenced)
    assert {(r.name, r.kind) for r in once.instrument_requirements} == {
        (r.name, r.kind) for r in twice.instrument_requirements
    }


@given(profiles=_profile_list_strategy())
@settings(max_examples=80, deadline=None)
def test_compose_associative_for_pure_dict_profiles(
    profiles: list[ProfileBody],
) -> None:
    """For render-block-free profiles, caller order does not affect output.

    Compares ``compose([a, b, c])`` to ``compose([a, c, b])`` after sorting
    every list field — the output sets must match.
    """
    if len(profiles) < 2:
        return
    forward = compose(profiles)
    # Swap the last two: this is enough to tickle the strictest-wins symmetry.
    swapped = compose(profiles[:-2] + profiles[-2:][::-1])

    # State extensions are sorted-union → identical regardless of order.
    assert forward.state_extensions == swapped.state_extensions
    # String lists are sorted-union.
    assert forward.skills_referenced == swapped.skills_referenced
    assert forward.hooks_referenced == swapped.hooks_referenced
    # Instrument *set* (by name) and final ``kind`` (strictest-wins) match.
    fwd_kinds = {r.name: r.kind for r in forward.instrument_requirements}
    swp_kinds = {r.name: r.kind for r in swapped.instrument_requirements}
    assert fwd_kinds == swp_kinds


@given(profiles=_profile_list_strategy())
@settings(max_examples=80, deadline=None)
def test_compose_provenance_complete(profiles: list[ProfileBody]) -> None:
    """Every populated composed field traces back to ≥1 input profile.

    For each top-level field the composed profile populates with non-default
    content, the ``provenance`` map must list at least one input profile
    name. Conversely, fields no one populated may have an empty contributor
    list (default fall-through).
    """
    composed = compose(profiles)
    prov = composed.provenance

    if composed.state_extensions.fields_required:
        assert prov["state_extensions"], "state_extensions populated but provenance is empty"
        assert all(name in {b.name for b in profiles} for name in prov["state_extensions"])

    if composed.instrument_requirements:
        assert prov["instrument_requirements"]
        assert all(name in {b.name for b in profiles} for name in prov["instrument_requirements"])

    if composed.skills_referenced:
        assert prov["skills_referenced"]
        assert all(name in {b.name for b in profiles} for name in prov["skills_referenced"])

    if composed.hooks_referenced:
        assert prov["hooks_referenced"]
        assert all(name in {b.name for b in profiles} for name in prov["hooks_referenced"])
