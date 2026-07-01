"""Tests for the W57 iter-aware commit prefix in ``SubagentSpec`` step 0.

The pre-W57 ``_render_workflow`` hardcoded the ``[P<NN>-W<NN>]`` form,
producing invalid commit prefixes for iters >= I02 (where the
AGENTS ``commit-prefix`` rule requires the iter segment to remain
attributable). These tests pin the fix: a spec whose iter is I01
keeps the bare two-segment form; iter I02 and later carry the iter
segment too.
"""

from __future__ import annotations

import pytest

from eawf.workflow.agents.specs.models import SubagentSpec


def _spec(*, wave_id: str, iter_id: str) -> SubagentSpec:
    """Return a minimal :class:`SubagentSpec` with the given wave/iter pair."""
    return SubagentSpec.model_validate(
        {
            "wave_id": wave_id,
            "iter_id": iter_id,
            "title": "Solo wave",
            "scope_id": "QR",
        }
    )


def _workflow_block(spec: SubagentSpec, *, headless: bool = False) -> str:
    """Return the ``## Workflow`` block of *spec*'s rendered prompt."""
    rendered = spec.render(headless=headless)
    return rendered.split("## Workflow", 1)[1].split("## Out of scope", 1)[0]


def test_iter_i01_uses_bare_phase_wave_prefix() -> None:
    """I01 keeps the legacy ``[P<NN>-W<NN>]`` form (iter segment dropped)."""
    spec = _spec(wave_id="P01-I01-W01", iter_id="P01-I01")
    workflow = _workflow_block(spec)
    assert "[P01-W01]" in workflow
    assert "[P01-I01-W01]" not in workflow


def test_iter_i02_includes_iter_segment() -> None:
    """I02 carries the full ``[P<NN>-I<NN>-W<NN>]`` form."""
    spec = _spec(wave_id="P10-I02-W03", iter_id="P10-I02")
    workflow = _workflow_block(spec)
    assert "[P10-I02-W03]" in workflow
    assert "[P10-W03]" not in workflow


def test_iter_i03_includes_iter_segment_for_p28() -> None:
    """The bug repro: P28-I03-W57 must render as ``[P28-I03-W57]``."""
    spec = _spec(wave_id="P28-I03-W57", iter_id="P28-I03")
    workflow = _workflow_block(spec)
    assert "[P28-I03-W57]" in workflow
    # The legacy hardcoded shape would have produced the bare form below
    # — its absence is the test that the fix lands.
    assert "[P28-W57]" not in workflow


def test_iter_i10_two_digit_iter_segment_renders() -> None:
    """Two-digit iter segments survive the split (forward compat)."""
    spec = _spec(wave_id="P12-I10-W05", iter_id="P12-I10")
    workflow = _workflow_block(spec)
    assert "[P12-I10-W05]" in workflow


@pytest.mark.parametrize(
    ("wave_id", "iter_id", "expected"),
    [
        ("P01-I01-W01", "P01-I01", "[P01-W01]"),
        ("P28-I01-W01", "P28-I01", "[P28-W01]"),
        ("P28-I02-W14", "P28-I02", "[P28-I02-W14]"),
        ("P28-I03-W57", "P28-I03", "[P28-I03-W57]"),
        ("P28-I03-W57", "P28-I03", "[P28-I03-W57]"),
    ],
)
def test_commit_prefix_table(wave_id: str, iter_id: str, expected: str) -> None:
    """Table-driven coverage of the iter-aware prefix shape."""
    workflow = _workflow_block(_spec(wave_id=wave_id, iter_id=iter_id))
    assert expected in workflow


def test_close_command_still_uses_full_wave_id() -> None:
    """The interactive ``wave close`` command at step 5 uses the full ``Pxx-Iyy-Wzz``."""
    spec = _spec(wave_id="P28-I03-W57", iter_id="P28-I03")
    workflow = _workflow_block(spec)
    assert "uv run eawf wave close P28-I03-W57" in workflow


def test_headless_render_omits_self_close_step() -> None:
    """The daemon live-spawn (headless) render must NOT instruct a self-close.

    A sandboxed autopilot agent cannot self-close — the daemon closes the wave
    on its behalf once the report binds (DL-5). A literal ``uv run eawf wave
    close`` instruction is what drove a compliant agent (codex) to a spurious
    ``blocked`` verdict in the P30-I21 live e2e.
    """
    spec = _spec(wave_id="P28-I03-W57", iter_id="P28-I03")
    workflow = _workflow_block(spec, headless=True)
    assert "uv run eawf wave close" not in workflow
    assert "Do **not** run `eawf wave close`" in workflow
    assert "closes this wave on your behalf" in workflow


def test_interactive_and_headless_share_steps_1_through_4() -> None:
    """Only step 5 differs between the two renders; steps 1-4 stay identical."""
    spec = _spec(wave_id="P28-I03-W57", iter_id="P28-I03")
    interactive = _workflow_block(spec)
    headless = _workflow_block(spec, headless=True)
    # Steps 1-4 (everything up to the step-5 line) are byte-identical.
    prefix_interactive = interactive.split("5.", 1)[0]
    prefix_headless = headless.split("5.", 1)[0]
    assert prefix_interactive == prefix_headless
    assert "[P28-I03-W57]" in headless  # commit prefix unaffected by headless
