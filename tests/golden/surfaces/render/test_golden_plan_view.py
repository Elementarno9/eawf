"""Golden-output regression tests for ``eawf.surfaces.render.plan_view``.

For each fixture combo we render both branches (markdown + JSON) from a
pinned ``state.json`` and assert byte-equality against the committed
``expected.md`` / ``expected.json`` files.

A failure means the renderer drifted — either an algorithm change or a
template bug. Regenerate the fixture deliberately (use the snippet at the
top of ``_REGEN_HINT`` in this file) and commit the new bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.render.plan_view import build_view, render_json, render_markdown

#: The committed fixture combos keep their own sub-directory: the test module
#: mirrors the package it renders, the bytes stay grouped under one name the
#: snapshot inventory can address.
_FIXTURE_DIR: Path = Path(__file__).parent / "plan_view"

_REGEN_HINT = """
To regenerate after a deliberate renderer change:

    cd <repo>
    uv run python -c "
    import orjson
    from pathlib import Path
    from eawf.surfaces.render.plan_view import build_view, render_markdown, render_json
    from eawf.kernel.state.models import State

    for combo in ('core_only', 'core_python', 'core_python_research'):
        d = Path('tests/golden/surfaces/render/plan_view') / combo
        state = State.model_validate(orjson.loads((d / 'state.json').read_bytes()))
        # iter id pinned to P05-I01 across all fixtures
        view = build_view(state, 'P05-I01')
        (d / 'expected.md').write_text(render_markdown(view))
        env = render_json(view)
        (d / 'expected.json').write_bytes(
            orjson.dumps(env, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        )
    "
"""


def _normalise_trailing_newline(b: bytes) -> bytes:
    """Strip a single trailing newline if present.

    Pre-commit's ``end-of-file-fixer`` unconditionally appends a trailing
    newline to text fixtures. ``orjson.dumps`` and our markdown renderer
    do not emit one. Comparing against a normalised form lets the
    pre-commit hook stay enabled without forcing the renderer to mimic
    an editor convention.
    """
    if b.endswith(b"\n"):
        return b[:-1]
    return b


@pytest.mark.golden
@pytest.mark.parametrize(
    "fixture_name",
    [
        "core_only",
        "core_python",
        "core_python_research",
    ],
)
def test_render_plan_view_matches_golden_markdown(fixture_name: str) -> None:
    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    view = build_view(state, "P05-I01")
    actual = _normalise_trailing_newline(render_markdown(view).encode("utf-8"))
    expected = _normalise_trailing_newline((fixture / "expected.md").read_bytes())
    assert actual == expected, (
        f"plan_view markdown drift in fixture {fixture_name!r}. {_REGEN_HINT}"
    )


@pytest.mark.golden
@pytest.mark.parametrize(
    "fixture_name",
    [
        "core_only",
        "core_python",
        "core_python_research",
    ],
)
def test_render_plan_view_matches_golden_json(fixture_name: str) -> None:
    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    view = build_view(state, "P05-I01")
    actual = orjson.dumps(
        render_json(view),
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    expected = _normalise_trailing_newline((fixture / "expected.json").read_bytes())
    assert actual == expected, f"plan_view JSON drift in fixture {fixture_name!r}. {_REGEN_HINT}"


@pytest.mark.golden
@pytest.mark.parametrize(
    "fixture_name",
    [
        "core_only",
        "core_python",
        "core_python_research",
    ],
)
def test_render_plan_view_two_renders_byte_stable(fixture_name: str) -> None:
    """Two consecutive renders of the same state produce identical bytes."""
    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    view = build_view(state, "P05-I01")
    md1 = render_markdown(view)
    md2 = render_markdown(view)
    assert md1 == md2
    j1 = json.dumps(render_json(view), sort_keys=True)
    j2 = json.dumps(render_json(view), sort_keys=True)
    assert j1 == j2


# ---- P28-W18: unified roadmap renderer + per-phase markdown ----------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "core_only",
        "core_python",
        "core_python_research",
    ],
)
def test_render_phase_markdown_wraps_per_iter_plan_view(fixture_name: str) -> None:
    """``render_phase_markdown`` renders the phase header + each iter's plan view.

    Pins the W18 unification: the ``/prep`` skill body's ``plan_text``
    draws from this helper so the operator's plan-mode body and the per-
    iter ``eawf plan show`` markdown are the same projection.
    """
    from eawf.surfaces.render.plan_view import render_phase_markdown

    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    # All fixtures pin a P05 phase via the build_view tests above.
    md = render_phase_markdown(state, "P05")
    assert md.startswith("# Roadmap: P05")
    # Each iter under the phase yields its own plan-view body.
    for iter_id in state.phases["P05"].iter_ids:
        assert f"# Plan: {iter_id}" in md


def test_render_phase_markdown_unknown_phase_literal() -> None:
    """A phase missing from state renders an empty-state line, not a crash."""
    from eawf.surfaces.render.plan_view import render_phase_markdown

    fixture = _FIXTURE_DIR / "core_only"
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    md = render_phase_markdown(state, "P99")
    assert "P99" in md
    assert "not in state" in md
