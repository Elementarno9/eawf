"""``/mockup`` skill body.

``/mockup`` helps the model author UI mockups (ASCII layouts) and
surface them as side-by-side ``AskUserQuestion`` option previews. The
body holds the produced mockup variants plus an optional
:class:`UserQuestion` whose options carry the per-variant ``preview``
content so an operator can compare 2-4 concrete layouts and pick one.
The skill is advisory: it produces mockups and drives no state
mutation.

Pick-time golden capture
------------------------

When the operator picks one variant, :func:`resolve_mockup_pick`
normalises the chosen layout (reusing the single
:func:`~eawf.surfaces.render.snapshot_normalize.normalize_snapshot`
implementation the TUI snapshot harness uses) and writes it as the
approved ASCII golden under ``tests/snapshots/tui/golden/``, then stamps
the repo-relative path onto the wave-spec body. The golden derives from
the plan-time operator choice, not the implementing wave, so a later
close gate diffs the built screen against this pick-time oracle -- the
wave cannot author the golden it is graded against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.ids import RE_WAVE
from eawf.surfaces.render.snapshot_normalize import normalize_snapshot
from eawf.workflow.skills.bodies.user_question import UserQuestion
from eawf.workflow.skills.bodies.wave_spec import WaveSpecBody

if TYPE_CHECKING:
    from pathlib import Path

#: Repo-relative home for approved ASCII mockup goldens. The stamped
#: ``mockup_golden_path`` is always rooted here regardless of the
#: filesystem ``output_dir`` the capture writes to, so the stored path
#: points at the committed-tree location a later close gate reads.
GOLDEN_DIR_REPO_REL: str = "tests/snapshots/tui/golden"


class MockupVariant(BaseModel):
    """One candidate UI mockup the model authored.

    Attributes:
        name: Short label for the variant (e.g. ``compact`` or
            ``two-column``); reused as the matching option label when the
            variant is surfaced through a :class:`UserQuestion`.
        layout: The ASCII-art layout (multi-line). Box-drawing uses plain
            ASCII characters so the render stays lint-clean.
        notes: Optional one-line rationale for the variant.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    layout: str = Field(min_length=1)
    notes: str | None = None


class MockupBody(BaseModel):
    """Body for ``/mockup``.

    Attributes:
        target_scope: Optional Eä state-scope URN the mockups inform.
        variants: The 2-4 authored mockup variants.
        user_question: Optional :class:`UserQuestion` whose options carry
            each variant's ``preview`` so the operator compares layouts
            side by side. ``None`` when the skill only emits the variants
            without an operator decision.
    """

    model_config = ConfigDict(extra="forbid")

    target_scope: str | None = None
    variants: list[MockupVariant] = Field(default_factory=list)
    user_question: UserQuestion | None = None


def mockup_golden_filename(wave_id: str) -> str:
    """Return the canonical golden filename for a wave's mockup pick.

    Args:
        wave_id: The target wave id (e.g. ``P30-I04-W07``). Must match the
            canonical wave-id shape so the filename stays path-safe.

    Returns:
        The ``mockup_<wave-id>.txt`` filename (no directory component).

    Raises:
        ValueError: when ``wave_id`` does not match the canonical wave-id
            pattern (``P<NN>-I<NN>-W<NN>``).
    """
    if not RE_WAVE.fullmatch(wave_id):
        raise ValueError(f"invalid wave id: {wave_id!r}")
    return f"mockup_{wave_id}.txt"


def resolve_mockup_pick(
    chosen: MockupVariant,
    *,
    wave_id: str,
    body: WaveSpecBody,
    output_dir: Path,
) -> WaveSpecBody:
    """Capture the operator-picked variant as an approved golden + stamp it.

    Normalises the chosen variant's ``layout`` through the single
    :func:`~eawf.surfaces.render.snapshot_normalize.normalize_snapshot`
    implementation, writes it as ``mockup_<wave-id>.txt`` under
    *output_dir*, and returns a copy of *body* with ``mockup_golden_path``
    stamped to the repo-relative golden path. The stamped path is rooted
    at :data:`GOLDEN_DIR_REPO_REL` (the committed-tree home) regardless of
    *output_dir*, so the close gate that later diffs the built screen reads
    the path from its canonical location.

    Args:
        chosen: The :class:`MockupVariant` the operator picked.
        wave_id: The target wave id the golden is keyed on.
        body: The wave-spec body to stamp; returned unmutated, a stamped
            copy is produced via ``model_copy``.
        output_dir: Filesystem directory the golden is written under
            (created with parents if absent). In tests this is a
            ``tmp_path``; in the real flow it is the repo's golden dir.

    Returns:
        A copy of *body* with ``mockup_golden_path`` set to the
        repo-relative golden path.

    Raises:
        ValueError: when ``wave_id`` is not a canonical wave id, or the
            normalised layout is empty (an empty mockup carries no oracle).
    """
    filename = mockup_golden_filename(wave_id)
    content = normalize_snapshot(chosen.layout)
    if not content.strip():
        raise ValueError(f"empty mockup layout for wave: {wave_id!r}")
    output_dir.mkdir(parents=True, exist_ok=True)
    golden_file = output_dir / filename
    golden_file.write_text(content + "\n", encoding="utf-8")
    repo_relative_path = f"{GOLDEN_DIR_REPO_REL}/{filename}"
    return body.model_copy(update={"mockup_golden_path": repo_relative_path})


__all__ = [
    "GOLDEN_DIR_REPO_REL",
    "MockupBody",
    "MockupVariant",
    "mockup_golden_filename",
    "resolve_mockup_pick",
]
