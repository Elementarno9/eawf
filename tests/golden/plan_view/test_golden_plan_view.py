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

_FIXTURE_DIR: Path = Path(__file__).parent

_REGEN_HINT = """
To regenerate after a deliberate renderer change:

    cd <repo>
    uv run python -c "
    import orjson
    from pathlib import Path
    from eawf.surfaces.render.plan_view import build_view, render_markdown, render_json
    from eawf.kernel.state.models import State

    for combo in ('core_only', 'core_python', 'core_python_research'):
        d = Path('tests/golden/plan_view') / combo
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
def test_build_roadmap_rows_projects_every_phase(fixture_name: str) -> None:
    """``build_roadmap_rows`` projects every phase in ``state.phases``."""
    from eawf.surfaces.render.plan_view import build_roadmap_rows

    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    rows = build_roadmap_rows(state)
    assert {row.id for row in rows} == set(state.phases.keys())
    # Each row's wave_count tallies the phase's child waves.
    for row in rows:
        iter_id_set = set(row.iter_ids)
        expected = sum(1 for w in state.waves.values() if w.iter_id in iter_id_set)
        assert row.wave_count == expected, (
            f"phase {row.id!r} wave_count drift: row={row.wave_count} expected={expected}"
        )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "core_only",
        "core_python",
        "core_python_research",
    ],
)
def test_render_roadmap_markdown_matches_cli_show_md_shape(fixture_name: str) -> None:
    """``render_roadmap_markdown`` emits the ``roadmap show --md`` table.

    Pins the W18 unification: the CLI's ``--md`` branch now delegates to
    this helper so the renderer lives in ``plan_view`` alongside per-iter
    :func:`render_markdown`. The output is the same pipe-delimited table.
    """
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    fixture = _FIXTURE_DIR / fixture_name
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    md = render_roadmap_markdown(state)
    assert md.startswith("| Phase | Status | Waves | Depends on | Title |\n")
    # Every phase id appears in the rendered body.
    for phase_id in state.phases:
        assert f"`{phase_id}`" in md


def test_render_roadmap_markdown_phase_filter_restricts_rows() -> None:
    """``phase_id_filter`` restricts the rendered table to one phase row."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    fixture = _FIXTURE_DIR / "core_only"
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    target = next(iter(state.phases))
    md = render_roadmap_markdown(state, phase_id_filter=target)
    body_lines = [line for line in md.splitlines() if line.startswith("| `")]
    assert len(body_lines) == 1
    assert f"`{target}`" in body_lines[0]


def test_render_roadmap_markdown_no_matching_phase_literal() -> None:
    """Filtering for a phase id not in state yields the empty-state literal."""
    from eawf.surfaces.render.plan_view import render_roadmap_markdown

    fixture = _FIXTURE_DIR / "core_only"
    state = State.model_validate(orjson.loads((fixture / "state.json").read_bytes()))
    md = render_roadmap_markdown(state, phase_id_filter="P99")
    assert md == "_(no phases in state)_"


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
