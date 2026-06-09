"""Close-gate bar for the W32 offline daemon-down brand-frame reskin.

The headless brand frame (the full-screen daemon-down splash rendered by
:mod:`eawf.surfaces.tui.offline` when no daemon is reachable) used to head
with a colourless / old-teal ``Eä`` while the live interactive header
(:func:`eawf.surfaces.tui.widgets.header.render_header`) was reskinned to the
two-tone cosmic-terminal green. This suite proves the offline frame now paints
the SAME two-tone green ``Eä`` wordmark the header does:

1. **Token parity** -- the offline frame's brand head embeds the exact accent
   token (:func:`eawf.surfaces.render.brand.accent_sgr`) brand.py emits, and an
   old-teal frame (a different accent hex) fails the same assert.
2. **Two-tone** -- the ``ä`` (U+00E4) carries the accent SGR; the leading ``E``
   does NOT, exactly as the markup channel the header consumes.
3. **Header lockstep** -- the offline wordmark equals
   :func:`~eawf.surfaces.render.brand.render_wordmark_ansi` at the canonical
   :data:`~eawf.surfaces.render.brand.ACCENT_HEX`, the same accent the header
   threads through :func:`~eawf.surfaces.render.brand.render_wordmark_markup`.
4. **Snapshot golden** -- the deterministic missing-registry offline frame and
   the ``None``-state status frame are pinned to committed ``.txt`` goldens so
   a future brand-head regression (colour drop, teal regression, layout churn)
   trips a byte diff. Regenerate daemonless with
   ``EAWF_OFFLINE_BRAND_REGEN=1 uv run pytest tests/tui/test_offline_brand.py``.

The frames asserted here are pure functions of fixed input (a ``None`` state /
an absent registry path), so the goldens carry no volatile clock or daemon
banner -- the brand head and section chrome are fully deterministic.
"""

from __future__ import annotations

import os
from pathlib import Path

from eawf.surfaces.render.brand import (
    ACCENT_HEX,
    accent_sgr,
    render_wordmark_ansi,
)
from eawf.surfaces.tui.offline import build_status_text, offline_render

#: A representative pre-reskin teal accent. The offline frame must NOT carry
#: this hex's SGR -- a teal-headed frame fails the token-parity assert, which
#: is the regression W32 forecloses.
_OLD_TEAL_HEX = "#0d9488"

#: Regen env for the committed offline-brand text goldens. CI runs without it
#: so a drift fails the build; a developer regenerates with it set.
_REGEN_ENV = "EAWF_OFFLINE_BRAND_REGEN"

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "offline_brand"
_STATUS_GOLDEN = _GOLDEN_DIR / "status_frame_none.txt"
_DASHBOARD_GOLDEN = _GOLDEN_DIR / "dashboard_missing_registry.txt"


def _absent_registry_frame() -> str:
    """Render the fully deterministic missing-registry offline dashboard frame.

    An absent registry path carries no timestamps and no daemon banner, so
    the frame is a pure function with no volatile cell -- a stable golden.

    Returns:
        The rendered text frame (terminated by a trailing newline).
    """
    return offline_render(registry_path=Path("/nonexistent/offline-brand-absent.json"))


def _assert_text_golden(captured: str, golden_path: Path) -> None:
    """Assert *captured* equals *golden_path*, or regen under the regen env.

    Mirrors the :func:`eawf.surfaces.tui.snapshot.assert_screen_snapshot`
    contract for plain-text frames: byte equality against a committed golden,
    with an env-gated regeneration escape hatch. Both sides are normalized to
    exactly one trailing newline because the committed golden always ends with
    one (the pre-commit end-of-file fixer guarantees it) while a rendered
    frame may not.

    Args:
        captured: The rendered frame text under test.
        golden_path: Path to the committed ``.txt`` golden.

    Raises:
        AssertionError: When *captured* differs from the golden and
            regeneration is not requested.
    """
    captured = captured.rstrip("\n") + "\n"
    if os.environ.get(_REGEN_ENV) == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(captured, encoding="utf-8")
        return
    expected = golden_path.read_text(encoding="utf-8")
    assert captured == expected, (
        f"offline-brand golden drift for {golden_path.name!r}; "
        f"regenerate with {_REGEN_ENV}=1 uv run pytest tests/tui/test_offline_brand.py"
    )


# --------------------------------------------------------------------------
# Token parity -- the load-bearing close-gate claim
# --------------------------------------------------------------------------


def test_offline_status_frame_carries_brand_accent_token() -> None:
    """The status frame's brand head embeds the exact brand.py accent token.

    The accent token is the single canonical ANSI 24-bit SGR
    (:func:`~eawf.surfaces.render.brand.accent_sgr`) brand.py emits at the
    reskin green; the offline frame must carry it verbatim so the two surfaces
    cannot drift to different greens.
    """
    frame = build_status_text(None)
    assert accent_sgr(ACCENT_HEX) in frame


def test_offline_dashboard_frame_carries_brand_accent_token() -> None:
    """The dashboard frame's brand head embeds the exact brand.py accent token."""
    frame = _absent_registry_frame()
    assert accent_sgr(ACCENT_HEX) in frame


def test_offline_status_frame_rejects_old_teal_accent() -> None:
    """The old-teal frame fails the token-parity assert -- the regression bar.

    The pre-reskin teal accent's SGR must be ABSENT from the green-reskinned
    frame; were the offline frame still teal-headed this assert would trip,
    which is exactly the close-gate guarantee.
    """
    frame = build_status_text(None)
    assert accent_sgr(_OLD_TEAL_HEX) not in frame
    assert _OLD_TEAL_HEX not in frame


def test_offline_dashboard_frame_rejects_old_teal_accent() -> None:
    """The old-teal dashboard frame fails the token-parity assert."""
    frame = _absent_registry_frame()
    assert accent_sgr(_OLD_TEAL_HEX) not in frame
    assert _OLD_TEAL_HEX not in frame


# --------------------------------------------------------------------------
# Two-tone proof + header lockstep
# --------------------------------------------------------------------------


def test_offline_brand_head_is_two_tone_umlaut_accented_e_plain() -> None:
    """The ``ä`` carries the accent SGR; the ``E`` is plain (two-tone).

    The accent SGR opens immediately before the umlaut and the foreground is
    reset immediately after, so the leading ``E`` is never inside the accent
    span -- the same two-tone shape the markup channel gives the header.
    """
    frame = build_status_text(None)
    sgr = accent_sgr(ACCENT_HEX)
    # The wordmark leads the frame: E, then the accent-open, then the umlaut.
    assert frame.startswith("E")
    # The accent span opens right after the bare E and wraps only the umlaut.
    assert frame.startswith(f"E{sgr}ä")
    # The E is NOT preceded by the accent SGR -- it stays in the base fg.
    assert not frame.startswith(sgr)


def test_offline_brand_head_equals_header_wordmark_channel() -> None:
    """The offline brand head wordmark is brand.py's canonical green wordmark.

    The interactive header threads ``$accent`` (resolved to
    :data:`~eawf.surfaces.render.brand.ACCENT_HEX` by the dark theme) through
    :func:`~eawf.surfaces.render.brand.render_wordmark_markup`; the offline
    frame threads the SAME ``ACCENT_HEX`` through the ANSI sibling
    :func:`~eawf.surfaces.render.brand.render_wordmark_ansi`. Both channels
    therefore carry one accent token, so the splash and the live header are
    the same green -- proven by the offline head leading with the canonical
    ANSI wordmark verbatim.
    """
    wordmark = render_wordmark_ansi(ACCENT_HEX)
    assert build_status_text(None).startswith(wordmark)
    assert _absent_registry_frame().startswith(wordmark)


def test_offline_status_and_dashboard_share_one_brand_head() -> None:
    """Both offline renderers head with the identical two-tone wordmark.

    Neither path re-derives the brand head independently, so a future reskin
    cannot leave one frame green and the other teal.
    """
    wordmark = render_wordmark_ansi(ACCENT_HEX)
    status_head = build_status_text(None).split("\n", 1)[0]
    dashboard_head = _absent_registry_frame().split("\n", 1)[0]
    assert status_head.startswith(wordmark)
    assert dashboard_head.startswith(wordmark)
    # Both heads carry byte-identical wordmark + gap before the breadcrumb.
    assert status_head[: len(wordmark)] == dashboard_head[: len(wordmark)]


# --------------------------------------------------------------------------
# Snapshot goldens -- deterministic text frames
# --------------------------------------------------------------------------


def test_offline_status_frame_matches_golden() -> None:
    """The ``None``-state status frame is byte-equal to its committed golden."""
    _assert_text_golden(build_status_text(None), _STATUS_GOLDEN)


def test_offline_dashboard_frame_matches_golden() -> None:
    """The missing-registry dashboard frame is byte-equal to its committed golden."""
    _assert_text_golden(_absent_registry_frame(), _DASHBOARD_GOLDEN)
