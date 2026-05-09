"""Unit tests for :mod:`eawf.profiles.compose`.

Coverage targets the W02 acceptance criteria:

- Empty / single-input cases behave sanely.
- Render-block order follows caller order; first profile to declare an id
  locks that id's slot.
- Strictest-wins on ``instrument_requirements[].kind``: ``hard`` beats ``soft``
  regardless of caller order.
- Plain string lists (``skills_referenced``, ``hooks_referenced``) are union+
  sorted.
- Provenance records every input profile that contributed to a populated key.
- ``state_extensions.fields_required`` is sorted-union.
"""

from __future__ import annotations

from eawf.profiles import (
    InstrumentReq,
    ProfileBody,
    RenderBlock,
    StateExtensions,
    compose,
    load_profile,
)


def _profile(
    name: str,
    *,
    state_keys: list[str] | None = None,
    instruments: list[InstrumentReq] | None = None,
    blocks: list[RenderBlock] | None = None,
    skills: list[str] | None = None,
    hooks: list[str] | None = None,
) -> ProfileBody:
    return ProfileBody(
        name=name,
        state_extensions=StateExtensions(fields_required=state_keys or []),
        instrument_requirements=instruments or [],
        render_blocks=blocks or [],
        skills_referenced=skills or [],
        hooks_referenced=hooks or [],
    )


def test_compose_empty_returns_empty() -> None:
    composed = compose([])
    assert composed.name == "composed:empty"
    assert composed.render_blocks == []
    assert composed.instrument_requirements == []
    assert composed.state_extensions.fields_required == []
    assert composed.skills_referenced == []
    assert composed.hooks_referenced == []
    # Provenance for every populated-or-default top-level field is present
    # even when empty so callers don't get KeyErrors when introspecting.
    assert set(composed.provenance.keys()) == {
        "state_extensions",
        "instrument_requirements",
        "render_blocks",
        "skills_referenced",
        "hooks_referenced",
    }
    assert all(v == [] for v in composed.provenance.values())


def test_compose_single_passthrough() -> None:
    body = _profile(
        "alpha",
        state_keys=["x", "y"],
        skills=["a", "b"],
    )
    composed = compose([body])
    assert composed.name == "alpha"
    assert composed.state_extensions.fields_required == ["x", "y"]
    assert composed.skills_referenced == ["a", "b"]
    assert composed.provenance["state_extensions"] == ["alpha"]
    assert composed.provenance["skills_referenced"] == ["alpha"]


def test_compose_deep_merge_simple() -> None:
    """Composing core + python yields a block list with both ids in order."""
    composed = compose([load_profile("core"), load_profile("python")])
    block_ids = [b.id for b in composed.render_blocks]
    assert block_ids == ["non-negotiable-rules", "python-style"]
    assert composed.name == "core+python"


def test_compose_preserves_block_order() -> None:
    """The first profile to mention a block id locks its position."""
    a = _profile(
        "a",
        blocks=[
            RenderBlock(id="x", target="AGENTS.md", body_template="from-a-x"),
            RenderBlock(id="y", target="AGENTS.md", body_template="from-a-y"),
        ],
    )
    b = _profile(
        "b",
        blocks=[
            # ``b`` re-declares ``y`` but in a swapped relative order; the
            # composed order must still be ``[x, y, z]`` because ``a`` saw
            # ``y`` first.
            RenderBlock(id="z", target="AGENTS.md", body_template="from-b-z"),
            RenderBlock(id="y", target="AGENTS.md", body_template="from-b-y"),
        ],
    )
    composed = compose([a, b])
    assert [block.id for block in composed.render_blocks] == ["x", "y", "z"]
    # Later override wins per id: y comes from b.
    by_id = {block.id: block for block in composed.render_blocks}
    assert by_id["y"].body_template == "from-b-y"
    assert by_id["x"].body_template == "from-a-x"
    assert by_id["z"].body_template == "from-b-z"


def test_compose_strictest_wins_for_instrument_kind_left_first() -> None:
    """``hard`` beats ``soft`` when the soft declaration comes first."""
    a = _profile(
        "a",
        instruments=[InstrumentReq(name="git", kind="soft")],
    )
    b = _profile(
        "b",
        instruments=[InstrumentReq(name="git", kind="hard")],
    )
    composed = compose([a, b])
    assert composed.instrument_requirements[0].name == "git"
    assert composed.instrument_requirements[0].kind == "hard"


def test_compose_strictest_wins_for_instrument_kind_right_first() -> None:
    """``hard`` beats ``soft`` when the hard declaration comes first."""
    a = _profile(
        "a",
        instruments=[InstrumentReq(name="git", kind="hard")],
    )
    b = _profile(
        "b",
        instruments=[InstrumentReq(name="git", kind="soft")],
    )
    composed = compose([a, b])
    assert composed.instrument_requirements[0].kind == "hard"


def test_compose_state_extensions_sorted_union() -> None:
    a = _profile("a", state_keys=["foo", "bar"])
    b = _profile("b", state_keys=["baz", "bar"])
    composed = compose([a, b])
    assert composed.state_extensions.fields_required == ["bar", "baz", "foo"]
    assert composed.provenance["state_extensions"] == ["a", "b"]


def test_compose_string_lists_sorted_union() -> None:
    a = _profile("a", skills=["alpha", "delta"], hooks=["pre"])
    b = _profile("b", skills=["beta", "alpha"], hooks=["post"])
    composed = compose([a, b])
    assert composed.skills_referenced == ["alpha", "beta", "delta"]
    assert composed.hooks_referenced == ["post", "pre"]


def test_compose_provenance_records_contributors() -> None:
    a = _profile("a", state_keys=["x"])
    b = _profile("b", state_keys=[])  # contributes nothing to state_keys
    c = _profile("c", state_keys=["y"])
    composed = compose([a, b, c])
    assert composed.provenance["state_extensions"] == ["a", "c"]
    # Empty contributor lists for fields no one populated.
    assert composed.provenance["render_blocks"] == []
    assert composed.provenance["skills_referenced"] == []


def test_compose_no_inputs_makes_empty_label() -> None:
    composed = compose([])
    assert composed.name == "composed:empty"


def test_compose_real_three_profile_combo() -> None:
    composed = compose([load_profile("core"), load_profile("python"), load_profile("research")])
    assert "hypotheses" in composed.state_extensions.fields_required
    assert "audits" in composed.state_extensions.fields_required
    assert composed.provenance["state_extensions"] == ["research"]
    # Render-block ids cover all three profile contributions.
    block_ids = [b.id for b in composed.render_blocks]
    assert "non-negotiable-rules" in block_ids
    assert "python-style" in block_ids
    assert "research-workflow" in block_ids


def test_compose_deterministic_for_identical_input() -> None:
    a = load_profile("core")
    b = load_profile("python")
    one = compose([a, b]).model_dump(mode="json")
    two = compose([a, b]).model_dump(mode="json")
    assert one == two


def test_compose_order_insensitive_for_non_render_block_fields() -> None:
    """Sorted-union and strictest-wins are symmetric in caller order."""
    a = load_profile("core")
    b = load_profile("python")
    c = load_profile("research")
    forward = compose([a, b, c])
    swapped = compose([a, c, b])
    # Non-render-block fields must match exactly.
    assert forward.state_extensions == swapped.state_extensions
    assert sorted(forward.skills_referenced) == sorted(swapped.skills_referenced)
    assert sorted(forward.hooks_referenced) == sorted(swapped.hooks_referenced)
    # Instrument set (by name) is invariant; kind reflects strictest-wins.
    fwd_by_name = {r.name: r for r in forward.instrument_requirements}
    swp_by_name = {r.name: r for r in swapped.instrument_requirements}
    assert fwd_by_name.keys() == swp_by_name.keys()
    for name in fwd_by_name:
        assert fwd_by_name[name].kind == swp_by_name[name].kind


def test_compose_later_overrides_earlier_block_body() -> None:
    a = _profile(
        "a",
        blocks=[RenderBlock(id="dup", target="AGENTS.md", body_template="A")],
    )
    b = _profile(
        "b",
        blocks=[RenderBlock(id="dup", target="AGENTS.md", body_template="B")],
    )
    composed = compose([a, b])
    assert len(composed.render_blocks) == 1
    assert composed.render_blocks[0].body_template == "B"


def test_compose_preserves_instrument_first_seen_order() -> None:
    a = _profile(
        "a",
        instruments=[
            InstrumentReq(name="git", kind="hard"),
            InstrumentReq(name="uv", kind="hard"),
        ],
    )
    b = _profile(
        "b",
        instruments=[
            InstrumentReq(name="ruff", kind="soft"),
            InstrumentReq(name="git", kind="soft"),
        ],
    )
    composed = compose([a, b])
    names = [r.name for r in composed.instrument_requirements]
    assert names == ["git", "uv", "ruff"]


def test_compose_description_takes_last_non_empty() -> None:
    a = _profile("a")
    a = a.model_copy(update={"description": "first"})
    b = _profile("b")
    b = b.model_copy(update={"description": "second"})
    c = _profile("c")  # empty description shouldn't reset
    composed = compose([a, b, c])
    assert composed.description == "second"


def test_compose_strictest_keys_constant_documented() -> None:
    """The STRICTEST_KEYS constant lists the v0.1 strictest-wins paths."""
    from eawf.profiles import STRICTEST_KEYS

    assert "instrument_requirements[].kind" in STRICTEST_KEYS
