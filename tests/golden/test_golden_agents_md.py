"""Golden-output regression tests for ``eawf.surfaces.render.agents_md``.

For each fixture combo, render to a temp path and assert byte-equality
with the committed fixture under ``tests/golden/agents_md/``.

A failure here means the renderer's output drifted — either the template,
the composer, or one of the source profile bodies changed. Regenerate the
fixture deliberately (with the snippet at the top of each fixture-creation
PR) and commit the new bytes alongside the change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.profiles import compose, load_profile
from eawf.surfaces.render.agents_md import render_agents_md
from eawf.surfaces.render.manifest import Manifest

_FIXTURE_DIR: Path = Path(__file__).parent / "agents_md"


@pytest.mark.golden
@pytest.mark.parametrize(
    ("profile_combo", "fixture_name"),
    [
        (("core",), "core_only.md"),
        (("core", "python", "research"), "core_python_research.md"),
    ],
    ids=["core_only", "core_python_research"],
)
def test_render_agents_md_matches_golden(
    profile_combo: tuple[str, ...],
    fixture_name: str,
    tmp_path: Path,
) -> None:
    """Rendered AGENTS.md is byte-identical to the committed fixture."""
    composed = compose([load_profile(p) for p in profile_combo])
    target = tmp_path / "AGENTS.md"
    render_agents_md(composed, target, Manifest(version=1, generated={}))

    actual_bytes = target.read_bytes()
    expected_bytes = (_FIXTURE_DIR / fixture_name).read_bytes()
    assert actual_bytes == expected_bytes, (
        f"AGENTS.md output drifted from golden fixture {fixture_name!r}. "
        "If this is intentional, regenerate the fixture and commit the new bytes."
    )


@pytest.mark.golden
@pytest.mark.parametrize(
    "profile_combo",
    [
        ("core",),
        ("core", "python", "research"),
    ],
    ids=["core_only", "core_python_research"],
)
def test_render_agents_md_two_renders_byte_stable(
    profile_combo: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Two consecutive renders of the same composed profile produce identical bytes.

    This is stronger than the golden check: it asserts the renderer itself is
    deterministic across calls (no hash-of-now sneaking into the output).
    """
    composed = compose([load_profile(p) for p in profile_combo])
    target = tmp_path / "AGENTS.md"

    _, manifest = render_agents_md(composed, target, Manifest(version=1, generated={}))
    first = target.read_bytes()

    render_agents_md(composed, target, manifest)
    second = target.read_bytes()

    assert first == second
