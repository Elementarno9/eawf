# noqa: EAWF010 cohesive workspace-grid surface; the pure width-helpers +
# cell-map slicing split into a sibling module once the responsive-degrade
# seam settles (extract-module follow-up), not mid-wave.
"""``WorkspaceTable`` — per-repo portfolio grid with zoom-on-Enter.

A :class:`~textual.widgets.DataTable` of the workspace's linked repos —
one row per repo, **always at least one** (the workspace dashboard shows
a real table even when a single repo is registered, never a fallback
panel). Each row carries the repo code, a phase-completion bar, an
EU-burn bar (both status-tinted Braille / ASCII via the App
:attr:`~eawf.surfaces.tui.app.EaApp.render_mode`), a live git status cell, and the
repo's last-touch age.

Two render concerns are split:

* The static columns (repo / phase / eu / age) derive from the bound
  :class:`~eawf.kernel.state.models.WorkspaceIndex` and each repo's own
  ``state.json``, computed in pure helpers
  (:func:`completion_pair`, :func:`eu_pair`) so the bar inputs are
  unit-testable without mounting the widget.
* The git column is **live**: a short ``git`` probe per repo, run off the
  event loop (Textual worker) and cached ~1 s, refreshed on the host's
  refresh tick. A probe failure dims the cell to ``git?`` (the
  ``GIT_UNAVAILABLE`` path) while every other column keeps rendering.

``Enter`` / ``z`` on the focused row posts :class:`WorkspaceTable.RowZoomed`
carrying the repo code; the host :class:`~eawf.surfaces.tui.scopes.workspace.WorkspaceScreen`
zooms that repo into a 2x2 quadrant. The downstream user-portfolio table
(W07) reuses this widget family.

The grid paints in the Eae cosmic-terminal language. Every per-repo row AND
the totals row LEAD with a lifecycle sigil (:func:`repo_row_sigil` /
:func:`totals_row_sigil`): the RUNNING diamond for an active phase, the
ABANDONED circled-slash for a stale repo, the CLOSED circle otherwise. The
per-repo phase bar is tinted the green status hue (:func:`_green_hex`); the
blocker / stale attention chip renders as the warn marker (the
:func:`~eawf.surfaces.tui.widgets.sigils.chrome` ``attention`` triangle, warn
band) rather than a bare word. The roll-up totals row reads in the BRAND voice
(:func:`_brand_hex`) so the summary lifts off the per-repo rows as the
portfolio's own line; its EU bar keeps the consumed-fraction burn band.

Every sigil + bar + chip span is baked to a concrete ``#rrggbb`` hex at
row-build time: a :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed
and cannot resolve the Textual ``$accent`` / ``$warn`` palette vars (see
:func:`_band_palette`). The plain :func:`format_totals_line` stays untinted so
the headless offline render keeps its colourless frame.

The row layout is width-responsive (:func:`visible_columns` /
:func:`phase_bar_cells`): at or below 80 cols the grid drops the low-priority
``git`` / ``pr`` / ``age`` columns and shrinks the phase bar, keeping the
load-bearing sigil + status-tinted bars un-clipped rather than overflowing the
pane edge.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.events import Resize
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable

from eawf.platform.registry.staleness import read_repo_state
from eawf.surfaces.render.brand import ACCENT_HEX as BRAND_ACCENT_HEX
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_BAND_PALETTE,
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_bar_rich,
    render_completion_bar,
)
from eawf.surfaces.tui.widgets.git_pane import gather_git_fields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph, tint
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State, WorkspaceRepoRef

logger = logging.getLogger(__name__)

#: Column ids in display order. ``repo`` and ``age`` are fixed-shape; the
#: two bar columns + git absorb the middle of the row; ``pr`` carries the
#: open-PR total per repo between the live git cell and the age cell.
_COLUMNS: tuple[str, ...] = ("repo", "phase", "eu", "git", "pr", "age")

#: The load-bearing columns that survive every width: the repo cell (the
#: leading lifecycle sigil + warn-marker status tint) and the two status-tinted
#: bars. A narrow terminal degrades to exactly these three so the sigil + tint +
#: bar are never dropped or clipped -- the reskin's irreducible signal.
_NARROW_COLUMNS: tuple[str, ...] = ("repo", "phase", "eu")

#: Terminal width (cells) at or below which the grid degrades to
#: :data:`_NARROW_COLUMNS`. 80 cols is the canonical narrow terminal (a tmux
#: split / an 80x24 default), the regime where the wide six-column row would
#: overflow the ``overflow-x: hidden`` pane edge and clip the trailing columns.
_NARROW_WIDTH_THRESHOLD: int = 80

#: Phase-completion bar cell count at the wide width.
_WIDE_BAR_CELLS: int = 6

#: Phase-completion bar cell count at the narrow width -- shrunk from
#: :data:`_WIDE_BAR_CELLS` so the degraded ``repo / phase / eu`` row fits 80
#: cells. The bar keeps its status tint; only its glyph run is shorter.
_NARROW_BAR_CELLS: int = 4


def visible_columns(width: int) -> tuple[str, ...]:
    """Return the column subset a grid of *width* cells renders.

    The responsive-degrade lever: at or below :data:`_NARROW_WIDTH_THRESHOLD`
    the grid drops the low-priority ``git`` / ``pr`` / ``age`` columns and keeps
    only the load-bearing :data:`_NARROW_COLUMNS`, so a narrow terminal degrades
    the row layout rather than overflowing the pane edge and clipping the sigil.
    Above the threshold (or at a pre-layout width of ``0``, where no clip can yet
    occur) the full :data:`_COLUMNS` row renders.

    Args:
        width: The grid's measured content width in cells.

    Returns:
        The ordered column-id tuple to render at *width*.
    """
    if 0 < width <= _NARROW_WIDTH_THRESHOLD:
        return _NARROW_COLUMNS
    return _COLUMNS


def phase_bar_cells(width: int) -> int:
    """Return the phase-completion bar cell count for a grid of *width* cells.

    The bar shrinks from :data:`_WIDE_BAR_CELLS` to :data:`_NARROW_BAR_CELLS`
    once the grid degrades (at or below :data:`_NARROW_WIDTH_THRESHOLD`) so the
    narrowed row fits an 80-cell terminal -- the bar keeps its tint, only its
    glyph run is shorter. A pre-layout width of ``0`` keeps the wide count.

    Args:
        width: The grid's measured content width in cells.

    Returns:
        The phase-bar cell count to render at *width*.
    """
    if 0 < width <= _NARROW_WIDTH_THRESHOLD:
        return _NARROW_BAR_CELLS
    return _WIDE_BAR_CELLS


#: Cell text rendered for a repo whose git probe could not resolve
#: (timeout / missing binary / non-git path). The substring ``git?`` is
#: part of the ``GIT_UNAVAILABLE`` contract the host + tests assert on.
GIT_UNAVAILABLE_CELL: str = "git?"

#: Cell text rendered before a repo's first git probe returns (the
#: worker is in flight). Distinct from :data:`GIT_UNAVAILABLE_CELL` so a
#: pending row is never mistaken for a failed probe.
GIT_PENDING_CELL: str = "…"

#: Per-repo git-probe cache TTL (seconds). A refresh tick inside this
#: window reuses the cached fields rather than re-paying the subprocess
#: cost, matching the brief's 1 s git cadence for the workspace table.
GIT_CACHE_TTL_S: float = 1.0


@dataclass(frozen=True)
class RepoRow:
    """One rendered workspace-table row's static (non-git) data.

    The git cell is rendered separately off the live probe; this carries
    the columns derived from the bound workspace index + per-repo state.

    Attributes:
        code: The repo's project code (the row key).
        path: Absolute on-disk path to the repo working tree.
        phase_id: The repo's active phase id, or ``None`` when no phase
            is active.
        phase_done: Closed wave count for the active phase's completion
            bar.
        phase_total: Total wave count for the active phase's completion
            bar.
        eu_consumed: Effort units consumed (actuals) for the EU-burn bar.
        eu_total: Estimated effort units for the EU-burn bar.
        age: Human-readable last-touch age cell (or a dash).
        blocker: ``True`` when the repo has a wave needing the operator
            now (a failed wave or a blocked open question); drives the
            attention chip.
        stale: ``True`` when the repo's ``state.json`` has not been
            touched within :data:`~eawf.platform.registry.staleness.STALE_AFTER`
            (or could not be read); drives the attention chip.
        open_prs: The repo's open-PR total for the PR-count column. ``0``
            when no live source resolves a count (the honest-empty case).
    """

    code: str
    path: str
    phase_done: int
    phase_total: int
    eu_consumed: float
    eu_total: float
    age: str
    # Defaulted (and therefore last in field order) so existing positional
    # ``RepoRow(...)`` constructions stay valid; ``_repo_row`` always sets them.
    phase_id: str | None = None
    blocker: bool = False
    stale: bool = False
    open_prs: int = 0


def completion_pair(repo_state: dict[str, Any] | None) -> tuple[int, int]:
    """Return the ``(closed, total)`` wave counts for a per-repo state dict.

    The phase-completion bar's input: how many of the repo's waves are
    closed over how many exist. A ``None`` / empty / malformed state
    yields ``(0, 0)`` so the bar surfaces the empty-state sentinel rather
    than a fabricated ratio.

    Args:
        repo_state: A decoded per-repo ``state.json`` dict, or ``None``.

    Returns:
        The ``(closed_waves, total_waves)`` pair.
    """
    if not repo_state:
        return (0, 0)
    waves = repo_state.get("waves")
    if not isinstance(waves, dict):
        return (0, 0)
    total = len(waves)
    closed = sum(1 for w in waves.values() if isinstance(w, dict) and w.get("status") == "closed")
    return (closed, total)


def active_phase_completion(repo_state: dict[str, Any] | None) -> tuple[str | None, int, int]:
    """Return ``(phase_id, closed, total)`` for a repo's active phase.

    Scopes the phase-completion bar to the repo's *active* phase rather
    than the whole repo: which phase is live, and how many of that
    phase's waves are closed over how many it owns. A ``None`` / empty /
    malformed state, or a state with no active phase, yields
    ``(None, 0, 0)`` so the bar surfaces the empty-state sentinel rather
    than a fabricated ratio.

    The active phase id resolves from the decoded per-repo state dict
    (not a typed :class:`~eawf.kernel.state.models.State`, since
    :func:`~eawf.platform.registry.staleness.read_repo_state` returns a raw
    ``dict``): the ``current.phase_id`` pointer wins when it names an
    existing phase whose ``status`` is ``"active"``; otherwise the single
    phase whose ``status`` is ``"active"`` is used; otherwise ``None``.

    Args:
        repo_state: A decoded per-repo ``state.json`` dict, or ``None``.

    Returns:
        The ``(phase_id, closed_waves, total_waves)`` triple, scoped to
        the active phase (or ``(None, 0, 0)`` when no phase is active).
    """
    if not repo_state:
        return (None, 0, 0)
    phases = repo_state.get("phases")
    if not isinstance(phases, dict):
        return (None, 0, 0)
    phase_id = _active_phase_id(repo_state, phases)
    if phase_id is None:
        return (None, 0, 0)
    iters = repo_state.get("iters")
    if not isinstance(iters, dict):
        return (phase_id, 0, 0)
    phase_iter_ids = {
        iter_id
        for iter_id, it in iters.items()
        if isinstance(it, dict) and it.get("phase_id") == phase_id
    }
    waves = repo_state.get("waves")
    if not isinstance(waves, dict):
        return (phase_id, 0, 0)
    phase_waves = [
        w for w in waves.values() if isinstance(w, dict) and w.get("iter_id") in phase_iter_ids
    ]
    total = len(phase_waves)
    closed = sum(1 for w in phase_waves if w.get("status") == "closed")
    return (phase_id, closed, total)


def _active_phase_id(repo_state: dict[str, Any], phases: dict[str, Any]) -> str | None:
    """Resolve the active phase id from a decoded per-repo state dict.

    The ``current.phase_id`` pointer wins when it names an existing phase
    whose ``status`` is ``"active"``; otherwise the single phase whose
    ``status`` is ``"active"`` is returned; otherwise ``None``. Every
    dict access is guarded so a partial / malformed state yields ``None``
    rather than raising out of the render path.

    Args:
        repo_state: The decoded per-repo ``state.json`` dict.
        phases: The already-validated ``repo_state["phases"]`` dict.

    Returns:
        The active phase id, or ``None`` when none is active.
    """
    current = repo_state.get("current")
    if isinstance(current, dict):
        pointer = current.get("phase_id")
        if isinstance(pointer, str):
            phase = phases.get(pointer)
            if isinstance(phase, dict) and phase.get("status") == "active":
                return pointer
    for phase_id, phase in phases.items():
        if isinstance(phase, dict) and phase.get("status") == "active":
            return phase_id
    return None


def eu_pair(repo_state: dict[str, Any] | None) -> tuple[float, float]:
    """Return the ``(consumed, total)`` EU pair for a per-repo state dict.

    The EU-burn bar's input: summed actual ``elapsed_eu`` over summed
    estimate ``expected_eu`` across the repo's recorded summaries. A
    ``None`` / empty / malformed state yields ``(0.0, 0.0)`` so the bar
    surfaces the empty-state sentinel.

    Args:
        repo_state: A decoded per-repo ``state.json`` dict, or ``None``.

    Returns:
        The ``(consumed_eu, total_eu)`` pair.
    """
    if not repo_state:
        return (0.0, 0.0)
    consumed = _sum_field(repo_state.get("actuals"), "elapsed_eu")
    total = _sum_field(repo_state.get("estimates"), "expected_eu")
    return (consumed, total)


def _sum_field(summaries: object, field: str) -> float:
    """Sum a numeric *field* across a mapping of summary dicts.

    Non-mapping inputs and rows missing / carrying a non-numeric *field*
    contribute zero so a malformed state never raises out of the render
    path.

    Args:
        summaries: The candidate mapping of id → summary dict.
        field: The numeric field to sum.

    Returns:
        The summed value (``0.0`` when nothing matches).
    """
    if not isinstance(summaries, dict):
        return 0.0
    total = 0.0
    for row in summaries.values():
        if isinstance(row, dict) and isinstance(row.get(field), int | float):
            total += float(row[field])
    return total


#: Chip text rendered in the repo cell when a repo has a wave needing the
#: operator now. The substring ``blocked`` is part of the attention-chip
#: contract the host + snapshot tests assert on. Parenthesised (not
#: bracketed) so the Rich-parsed DataTable cell never mistakes the chip
#: for a ``[style]`` markup tag and swallows it -- matching the registry
#: strip's ``(active)`` / ``(stale)`` chip convention.
BLOCKER_CHIP: str = "(blocked)"

#: Chip text rendered in the repo cell when a repo trips the stale-band
#: threshold. The substring ``stale`` is part of the attention-chip
#: contract the host + snapshot tests assert on. Parenthesised for the
#: same Rich-markup-safety reason as :data:`BLOCKER_CHIP`.
STALE_CHIP: str = "(stale)"


def repo_has_blocker(repo_state: dict[str, Any] | None) -> bool:
    """Return ``True`` when a repo has a wave / question needing the operator.

    The blocker signal behind the row's attention chip: a repo trips it
    when its decoded state carries either a ``failed`` wave or a
    ``blocked`` open question -- the two point-in-time "needs you now"
    signals the cross-repo band surfaces. A ``None`` / empty / malformed
    state yields ``False`` so a broken state file never fabricates an
    alarm.

    Args:
        repo_state: A decoded per-repo ``state.json`` dict, or ``None``.

    Returns:
        ``True`` when a failed wave or a blocked open question exists.
    """
    if not repo_state:
        return False
    waves = repo_state.get("waves")
    if isinstance(waves, dict) and any(
        isinstance(w, dict) and w.get("status") == "failed" for w in waves.values()
    ):
        return True
    questions = repo_state.get("open_questions")
    return isinstance(questions, dict) and any(
        isinstance(q, dict) and q.get("status") == "blocked" for q in questions.values()
    )


def repo_is_stale(repo_path: Path, *, now: datetime | None = None) -> bool:
    """Return ``True`` when *repo_path*'s state trips the stale-band threshold.

    The stale signal behind the row's attention chip: a repo trips it when
    its ``<repo_path>/.ea/state.json`` has not been touched within
    :data:`~eawf.platform.registry.staleness.STALE_AFTER`, or could not be
    read at all (a missing / unreadable state file is treated as stale,
    matching the registry strip's OR-chain).

    Args:
        repo_path: The repo working-tree root.
        now: Override for the current timestamp so freshness comparisons
            stay deterministic in tests; defaults to :func:`datetime.now`.

    Returns:
        ``True`` when the state mtime is older than
        :data:`~eawf.platform.registry.staleness.STALE_AFTER`, or the
        state file is missing / unreadable.
    """
    from eawf.platform.registry.staleness import STALE_AFTER, repo_state_mtime

    mtime = repo_state_mtime(repo_path)
    if mtime is None:
        return True
    current = now if now is not None else datetime.now(UTC)
    return (current - mtime) > STALE_AFTER


def attention_chip(row: RepoRow) -> str | None:
    """Return the repo row's attention-chip text, or ``None`` when calm.

    A repo renders an attention chip when its blocker or stale-band
    threshold trips. A row that trips both carries both chips
    (``(blocked) (stale)``, blocker first since it is the more acute "needs
    you now" signal); a row that trips neither returns ``None`` so the host
    renders the plain repo code.

    Args:
        row: The repo row to inspect.

    Returns:
        The chip text (one or both chips, space-joined), or ``None`` when
        the row trips neither threshold.
    """
    chips: list[str] = []
    if row.blocker:
        chips.append(BLOCKER_CHIP)
    if row.stale:
        chips.append(STALE_CHIP)
    if not chips:
        return None
    return " ".join(chips)


def _repo_cell(row: RepoRow) -> str:
    """Render *row*'s repo-column cell: the code plus any attention chip.

    A calm repo renders just its code; a repo tripping the blocker or
    stale-band threshold renders ``<code> <chip>`` so the operator scans
    which repo needs them without leaving the table-browse mode.

    Args:
        row: The repo row to render.

    Returns:
        The repo cell text.
    """
    chip = attention_chip(row)
    return f"{row.code} {chip}" if chip is not None else row.code


def repo_row_sigil(row: RepoRow) -> Sigil:
    """Map a repo row's lifecycle onto its leading :class:`Sigil`.

    Each per-repo row leads with a lifecycle sigil drawn from the I02 sigils
    source so the grid reads in the Eae cosmic-terminal language rather than
    as a flat code/bar table. A repo's lifecycle maps onto the shared
    lifecycle alphabet from its phase + staleness state, mirroring the
    registry strip's active / stale / closed mapping:

    * a repo with an active phase wears the RUNNING diamond (it is in flight);
    * a stale repo with no active phase wears the ABANDONED circled-slash (it
      has receded), shape-distinct from a not-yet-run ring;
    * every other repo wears the CLOSED filled circle (registered + calm).

    The active-phase signal wins over staleness when both hold, since a repo
    with a live phase reads as in-flight even when its on-disk state mtime has
    drifted stale. The attention chip carries the blocker / stale alarm
    separately (see :func:`warn_chip_markup`), so the leading sigil need not
    double as the alarm shape.

    Args:
        row: The repo row to inspect.

    Returns:
        The lifecycle :class:`Sigil` the row leads with.
    """
    if row.phase_id is not None:
        return Sigil.RUNNING
    if row.stale:
        return Sigil.ABANDONED
    return Sigil.CLOSED


def totals_row_sigil(totals: PortfolioTotals) -> Sigil:
    """Map the portfolio totals onto the summary row's leading :class:`Sigil`.

    The totals row leads with a lifecycle sigil too, so the summary lines up
    under the per-repo sigil column rather than reading as a bare ``Sigma``
    label. The portfolio reads as RUNNING (in flight) while any repo still has
    open work (``wave_done < wave_total``) and as CLOSED once every tracked
    wave has landed -- the aggregate twin of the per-repo lifecycle.

    Args:
        totals: The folded :class:`PortfolioTotals`.

    Returns:
        The lifecycle :class:`Sigil` the totals row leads with.
    """
    if totals.wave_total > 0 and totals.wave_done < totals.wave_total:
        return Sigil.RUNNING
    return Sigil.CLOSED


def _sigil_hex(sigil: Sigil, *, mode: RenderMode, palette: Mapping[str, str]) -> str:
    """Render *sigil*'s glyph tinted by its lifecycle status, as Rich markup.

    Composes the SHAPE (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) and the
    COLOUR (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) from the shared
    sigils helper so a row leads with a tinted lifecycle mark. The colour is
    baked to a concrete ``#rrggbb`` -- a :class:`textual.widgets.DataTable`
    ``str`` cell is Rich-parsed and cannot resolve the Textual ``$`` palette
    vars -- and the CLOSED hue is theme-resolved via *palette* so the calm
    green tracks the active theme. A sigil whose mapped status carries no tint
    falls back to the muted band so the mark still renders.

    Args:
        sigil: The lifecycle mark to render.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.
        palette: The status-tint band map (see :func:`_band_palette`); the
            CLOSED green is read from it so the tint tracks the theme.

    Returns:
        A Rich-markup span: the tinted (or muted) lifecycle glyph.
    """
    mark = glyph(sigil, mode=mode)
    if sigil is Sigil.CLOSED:
        return f"[{_green_hex(palette)}]{mark}[/]"
    hue = tint(sigil)
    if hue is None:
        return f"[{BAND_HEX['warn']}]{mark}[/]"
    return f"[{hue}]{mark}[/]"


def _green_hex(palette: Mapping[str, str]) -> str:
    """Return the concrete green status hex for the calm sigil + bar tint.

    The reskin's calm-green hue, theme-resolved off the EU-burn band map's
    ``ok`` key (the Wong green ``#009e73`` on the dark baseline) so the green
    tint tracks the active theme rather than hard-coding a hex. A
    :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed, so a
    concrete hex -- not the ``$ok`` / ``$accent`` palette var -- is required.

    Args:
        palette: The status-tint band map (see :func:`_band_palette`).

    Returns:
        The concrete green ``#rrggbb`` hex.
    """
    return palette.get("ok", BAND_HEX["ok"])


def _brand_hex(palette: Mapping[str, str]) -> str:
    """Return the concrete brand-accent hex for the roll-up totals tint.

    The reskin's brand green -- the green-rotated ``$accent`` / ``$primary``
    palette var (``#16b384`` on the dark baseline) -- theme-resolved off the
    palette's ``accent`` key so the brand tint tracks the active theme rather
    than hard-coding a hex, falling back to
    :data:`~eawf.surfaces.render.brand.ACCENT_HEX` when the active theme is
    unavailable. The totals row carries this brand hue rather than the
    per-repo band ``ok`` green (:func:`_green_hex`) so the roll-up reads in
    the portfolio's own brand voice. A
    :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed, so a
    concrete hex -- not the ``$accent`` palette var -- is required.

    Args:
        palette: The status-tint band map (see :func:`_band_palette`); the
            ``accent`` key carries the brand green when the theme resolves it.

    Returns:
        The concrete brand-accent ``#rrggbb`` hex.
    """
    return palette.get("accent", BRAND_ACCENT_HEX)


#: The built-in band palette extended with the brand ``accent`` key, used as
#: the default for the totals-row brand tint when no theme-resolved palette is
#: passed (e.g. an unmounted test harness). The live render passes the
#: theme-resolved :func:`_band_palette` map, which already carries ``accent``.
_DEFAULT_BRAND_PALETTE: dict[str, str] = {**DEFAULT_BAND_PALETTE, "accent": BRAND_ACCENT_HEX}


#: The chip text the warn marker trails when a repo trips the blocker
#: threshold. Carried after the triangle so a screen reader / plain scrape
#: still resolves which alarm fired; the substring ``blocked`` keeps the
#: attention-chip contract the host + tests assert on.
WARN_BLOCKER_TEXT: str = "blocked"

#: The chip text the warn marker trails when a repo trips the stale-band
#: threshold. Mirrors :data:`WARN_BLOCKER_TEXT`; the substring ``stale``
#: keeps the attention-chip contract.
WARN_STALE_TEXT: str = "stale"


def warn_chip_markup(row: RepoRow, *, mode: RenderMode) -> str | None:
    """Render *row*'s attention chip as the warn marker, or ``None`` when calm.

    The reskin twin of :func:`attention_chip`: a repo tripping the blocker or
    stale-band threshold renders the warn marker -- the
    :func:`~eawf.surfaces.tui.widgets.sigils.chrome` ``attention`` triangle,
    tinted the warn band -- followed by the alarm word, rather than a bare
    ``(blocked)`` / ``(stale)`` parenthesised word. A repo tripping both
    trails both words after one shared triangle (``<triangle> blocked stale``,
    blocker first since it is the more acute signal). A calm repo returns
    ``None`` so the cell renders just the code. The triangle + word are baked
    to a concrete warn ``#rrggbb`` hex because the cell is Rich-parsed.

    Args:
        row: The repo row to inspect.
        mode: The App's resolved render-mode label -- selects the triangle's
            ASCII / unicode column.

    Returns:
        The warn-marker chip markup, or ``None`` when the row is calm.
    """
    words: list[str] = []
    if row.blocker:
        words.append(WARN_BLOCKER_TEXT)
    if row.stale:
        words.append(WARN_STALE_TEXT)
    if not words:
        return None
    triangle = chrome("attention", mode=mode)
    return f"[{BAND_HEX['warn']}]{triangle} {' '.join(words)}[/]"


def _repo_cell_markup(row: RepoRow, *, mode: RenderMode, palette: Mapping[str, str]) -> str:
    """Render *row*'s repo-column cell with a leading sigil + warn-marker chip.

    The reskin twin of :func:`_repo_cell`: the cell leads with the row's tinted
    lifecycle sigil (:func:`repo_row_sigil` + :func:`_sigil_hex`), then the
    repo code, then the warn-marker attention chip (:func:`warn_chip_markup`)
    when the row trips a threshold. A calm repo renders ``<sigil> <code>``; an
    attention repo renders ``<sigil> <code> <warn-triangle> <words>``.

    Args:
        row: The repo row to render.
        mode: The App's resolved render-mode label.
        palette: The status-tint band map (see :func:`_band_palette`).

    Returns:
        The repo cell's Rich-markup string.
    """
    sigil = _sigil_hex(repo_row_sigil(row), mode=mode, palette=palette)
    chip = warn_chip_markup(row, mode=mode)
    body = f"{row.code} {chip}" if chip is not None else row.code
    return f"{sigil} {body}"


#: Row key for the portfolio totals summary row appended under the repo
#: rows. The leading sentinel avoids colliding with any repo code (a repo
#: code matches the project-code symbol pattern and never starts with this
#: marker), so a totals row is never mistaken for a zoomable repo.
TOTALS_ROW_KEY: str = "Σ-totals"

#: Repo-column cell text for the totals summary row. The capital sigma
#: reads as "sum across the portfolio" and keeps the row visually distinct
#: from the per-repo rows above it.
TOTALS_ROW_LABEL: str = "Σ"


@dataclass(frozen=True)
class PortfolioTotals:
    """Workspace-wide roll-up of every repo row's wave + EU counts.

    The summary-row inputs: the portfolio reducer folds the per-repo
    :class:`RepoRow` wave counts and EU pairs into one totals row rendered
    under the workspace table, so the operator reads the whole portfolio's
    progress without summing the rows by eye.

    Attributes:
        repo_count: Number of repo rows folded into the totals.
        wave_done: Summed closed-wave count across every repo's active
            phase.
        wave_total: Summed total-wave count across every repo's active
            phase.
        eu_consumed: Summed EU actuals across every repo.
        eu_total: Summed EU estimate across every repo.
        open_prs: Summed open-PR count across every repo.
    """

    repo_count: int
    wave_done: int
    wave_total: int
    eu_consumed: float
    eu_total: float
    open_prs: int


def portfolio_totals(rows: list[RepoRow]) -> PortfolioTotals:
    """Fold *rows* into the workspace-wide :class:`PortfolioTotals` summary.

    Pure reducer: sums each repo row's active-phase wave counts
    (``phase_done`` / ``phase_total``) and EU pair (``eu_consumed`` /
    ``eu_total``) into one totals value. An empty row list yields a
    zero-valued totals (``repo_count == 0``) so the host renders an honest
    empty summary rather than a fabricated ratio.

    Args:
        rows: The workspace table's repo rows (see :func:`build_repo_rows`).

    Returns:
        The folded :class:`PortfolioTotals`.
    """
    wave_done = sum(row.phase_done for row in rows)
    wave_total = sum(row.phase_total for row in rows)
    eu_consumed = sum(row.eu_consumed for row in rows)
    eu_total = sum(row.eu_total for row in rows)
    open_prs = sum(row.open_prs for row in rows)
    return PortfolioTotals(
        repo_count=len(rows),
        wave_done=wave_done,
        wave_total=wave_total,
        eu_consumed=eu_consumed,
        eu_total=eu_total,
        open_prs=open_prs,
    )


def format_totals_line(totals: PortfolioTotals) -> str:
    """Format a plain-text one-line portfolio-totals summary.

    The shared totals layout both the live workspace table's summary row
    and the headless offline render emit, so the two surfaces stay
    byte-aligned on the totals fields. Carries the same numbers as the
    live row -- the :data:`TOTALS_ROW_LABEL` sigma, the repo count, the
    summed wave ``done/total``, the summed EU ``consumed/total``, and the
    summed open-PR count -- without the live row's Rich-tinted bars (a
    plain-text frame cannot carry the colour spans).

    The EU + PR fields degrade to a dash when nothing was reported
    (``eu_total <= 0`` / ``open_prs == 0``) so an empty portfolio reads as
    honest-empty rather than a fabricated ``0.0/0.0`` ratio.

    Args:
        totals: The folded :class:`PortfolioTotals`.

    Returns:
        The one-line totals summary (no trailing newline).
    """
    eu = f"{totals.eu_consumed:g}/{totals.eu_total:g}" if totals.eu_total > 0 else "—"
    prs = str(totals.open_prs) if totals.open_prs > 0 else "—"
    return (
        f"{TOTALS_ROW_LABEL} {totals.repo_count} repos  "
        f"waves {totals.wave_done}/{totals.wave_total}  "
        f"EU {eu}  PR {prs}"
    )


def build_repo_rows(state: State | None) -> list[RepoRow]:
    """Build the workspace table's rows from a bound workspace *state*.

    One :class:`RepoRow` per repo in the bound
    :class:`~eawf.kernel.state.models.WorkspaceIndex`, ordered by repo code so
    the table is deterministic. Each row's bar inputs come from reading
    the repo's own ``state.json`` (best-effort; a missing / unreadable
    file leaves the bars empty). A ``None`` / non-workspace state yields
    an empty list — the host renders no rows, never crashes.

    Args:
        state: The bound workspace state, or ``None``.

    Returns:
        The repo rows in code order (possibly empty).
    """
    if state is None or state.workspace is None:
        return []
    rows: list[RepoRow] = []
    for code in sorted(state.workspace.repos):
        ref = state.workspace.repos[code]
        rows.append(_repo_row(ref))
    return rows


def _repo_row(ref: WorkspaceRepoRef) -> RepoRow:
    """Build one :class:`RepoRow` from a workspace repo *ref*.

    Reads the repo's own ``state.json`` (best-effort) for the bar inputs
    and derives the last-touch age from the same file's mtime.

    Args:
        ref: One :class:`~eawf.kernel.state.models.WorkspaceRepoRef`.

    Returns:
        The populated :class:`RepoRow`.
    """
    return repo_row_from_path(ref.code, ref.path)


def repo_row_from_path(code: str, path: str) -> RepoRow:
    """Build one :class:`RepoRow` from a repo *code* + on-disk *path*.

    The shared off-disk row builder both the live workspace table (via a
    :class:`~eawf.kernel.state.models.WorkspaceRepoRef`) and the headless
    offline render (via a registry entry) use, so the two surfaces fold
    identical per-repo inputs into the portfolio totals. Reads the repo's
    own ``state.json`` (best-effort) for the bar inputs and derives the
    last-touch age + stale flag from the same file's mtime.

    Args:
        code: The repo's project code (the row key).
        path: Absolute on-disk path to the repo working tree.

    Returns:
        The populated :class:`RepoRow`.
    """
    repo_path = Path(path)
    repo_state = read_repo_state(repo_path)
    phase_id, done, total = active_phase_completion(repo_state)
    consumed, eu_total = eu_pair(repo_state)
    return RepoRow(
        code=code,
        path=path,
        phase_id=phase_id,
        phase_done=done,
        phase_total=total,
        eu_consumed=consumed,
        eu_total=eu_total,
        age=_repo_age(repo_path),
        blocker=repo_has_blocker(repo_state),
        stale=repo_is_stale(repo_path),
        # No live PR source spans the workspace index; the open-PR count
        # stays at the honest-empty default until a source resolves one.
        open_prs=0,
    )


def _repo_age(repo_path: Path) -> str:
    """Return a coarse last-touch age for *repo_path*'s ``state.json``.

    Reads the per-repo state-file mtime and buckets the elapsed time into
    a compact ``Nm`` / ``Nh`` / ``Nd`` cell. A missing / unreadable file
    yields a dash so the row still renders.

    Args:
        repo_path: The repo working-tree root.

    Returns:
        A compact age cell, or ``"—"`` when undetermined.
    """
    from eawf.platform.registry.staleness import repo_state_mtime

    mtime = repo_state_mtime(repo_path)
    if mtime is None:
        return "—"
    elapsed = (datetime.now(UTC) - mtime).total_seconds()
    if elapsed < 3600:
        return f"{int(elapsed // 60)}m"
    if elapsed < 86400:
        return f"{int(elapsed // 3600)}h"
    return f"{int(elapsed // 86400)}d"


def _green_bar_markup(bar: str, *, palette: Mapping[str, str]) -> str:
    """Wrap a plain completion-bar string in the green status-tint span.

    The reskin tints the phase-completion bar the green status hue
    (:func:`_green_hex`) so a per-repo / totals row carries a green
    status-tinted bar. The empty-state sentinel is left untinted -- there is
    no progress to colour green, and tinting the "no data" text would imply a
    fill that is not there. The hue is a concrete ``#rrggbb`` because a
    :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed.

    Args:
        bar: The plain completion-bar string (or the empty-state sentinel).
        palette: The status-tint band map (see :func:`_band_palette`).

    Returns:
        The bar wrapped in the green hex span, or the untinted sentinel.
    """
    if bar == EMPTY_STATE:
        return bar
    return f"[{_green_hex(palette)}]{bar}[/]"


def _brand_bar_markup(bar: str, *, palette: Mapping[str, str]) -> str:
    """Wrap a plain completion-bar string in the brand-accent span.

    The roll-up totals twin of :func:`_green_bar_markup`: the totals row's
    summed completion bar carries the brand accent (:func:`_brand_hex`)
    rather than the per-repo band ``ok`` green, so the summary reads in the
    portfolio's own brand voice. The empty-state sentinel is left untinted --
    there is no progress to colour, and tinting the "no data" text would
    imply a fill that is not there. The hue is a concrete ``#rrggbb`` because
    a :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed.

    Args:
        bar: The plain completion-bar string (or the empty-state sentinel).
        palette: The status-tint band map (see :func:`_band_palette`).

    Returns:
        The bar wrapped in the brand-accent hex span, or the untinted
        sentinel.
    """
    if bar == EMPTY_STATE:
        return bar
    return f"[{_brand_hex(palette)}]{bar}[/]"


def _totals_sigil_markup(sigil: Sigil, *, mode: RenderMode, palette: Mapping[str, str]) -> str:
    """Render the totals row's leading lifecycle sigil in the brand accent.

    The roll-up twin of :func:`_sigil_hex`: the totals sigil carries the
    brand accent (:func:`_brand_hex`) regardless of which lifecycle shape
    (RUNNING / CLOSED) :func:`totals_row_sigil` maps it to, so the summary's
    leading mark reads in the brand voice rather than picking up a per-repo
    status hue. The hue is baked to a concrete ``#rrggbb`` because the
    :class:`textual.widgets.DataTable` ``str`` cell is Rich-parsed.

    Args:
        sigil: The aggregate lifecycle mark (see :func:`totals_row_sigil`).
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.
        palette: The status-tint band map (see :func:`_band_palette`); the
            brand accent is read from its ``accent`` key.

    Returns:
        A Rich-markup span: the brand-tinted lifecycle glyph.
    """
    mark = glyph(sigil, mode=mode)
    return f"[{_brand_hex(palette)}]{mark}[/]"


def _phase_cell(
    row: RepoRow,
    *,
    mode: RenderMode,
    palette: Mapping[str, str] | None = None,
    bar_cells: int = _WIDE_BAR_CELLS,
) -> str:
    """Render *row*'s active-phase id + green status-tinted completion bar cell.

    The phase id leads the cell; the completion bar is tinted the green status
    hue (see :func:`_green_bar_markup`) so a per-repo row carries a green
    status-tinted bar in the Eae cosmic-terminal language. The tint is baked
    to a concrete hex because the cell is Rich-parsed.

    Args:
        row: The repo row to render.
        mode: The active bar render mode.
        palette: The status-tint band map (see :func:`_band_palette`);
            defaults to the built-in palette when omitted.
        bar_cells: The completion bar's cell count -- shrunk on a narrow grid
            (see :func:`phase_bar_cells`) so the degraded row fits an 80-cell
            terminal while the bar keeps its status tint.

    Returns:
        The phase cell markup (``<phase_id> <green-bar>``).
    """
    bar = render_completion_bar(row.phase_done, row.phase_total, width=bar_cells, mode=mode)
    colours = palette if palette is not None else DEFAULT_BAND_PALETTE
    return f"{row.phase_id or '—'} {_green_bar_markup(bar, palette=colours)}"


def _pr_cell(open_prs: int) -> str:
    """Render the open-PR total cell for a repo row or the totals row.

    A repo with no open PRs renders a dash so a calm column reads as
    honest-empty rather than a noisy ``0`` on every row; a positive count
    renders the integer verbatim.

    Args:
        open_prs: The repo's (or the portfolio's) open-PR total.

    Returns:
        The PR-count cell text.
    """
    return str(open_prs) if open_prs > 0 else "—"


def _band_palette(app: App[object]) -> dict[str, str]:
    """Resolve the EU-burn band + brand-accent colours from the active theme.

    DataTable ``str`` cells are Rich-parsed and cannot resolve the Textual
    ``$ok`` / ``$warn`` / ``$err`` / ``$accent`` palette vars, so the tint
    must be baked to a concrete hex at row-build time. The map carries the
    three EU-burn band keys plus the ``accent`` key the totals row's brand
    tint reads (:func:`_brand_hex`). Falls back to
    :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_BAND_PALETTE` (and the
    brand :data:`~eawf.surfaces.render.brand.ACCENT_HEX` for ``accent``) when
    the active theme is unavailable (e.g. an unmounted test harness).

    Args:
        app: The host app whose active theme carries the palette.

    Returns:
        A ``{"ok"|"warn"|"err"|"accent": "#rrggbb"}`` map, theme values where
        present and the default palette otherwise.
    """
    theme = getattr(app, "current_theme", None)
    variables = getattr(theme, "variables", None) or {}
    defaults = {**DEFAULT_BAND_PALETTE, "accent": BRAND_ACCENT_HEX}
    return {key: variables.get(key, default) for key, default in defaults.items()}


def _eu_cell(row: RepoRow, *, mode: RenderMode, palette: Mapping[str, str] | None = None) -> str:
    """Render *row*'s EU-burn bar cell, or the empty sentinel.

    The EU bar is status-tinted (the consumed-fraction colour band) via
    :func:`~eawf.surfaces.tui.widgets.eu_bar.render_bar_rich`, which bakes the tint to
    a Rich-parseable ``#rrggbb`` span — the cell is a Rich-parsed
    :class:`textual.widgets.DataTable` ``str`` cell, so the Textual ``$``
    palette vars cannot be used here. A non-positive total surfaces
    :data:`~eawf.surfaces.tui.widgets.eu_bar.EMPTY_STATE` rather than a fabricated 0 %
    bar.

    Args:
        row: The repo row to render.
        mode: The active bar render mode.
        palette: Band-colour map (see :func:`_band_palette`); defaults to the
            built-in palette when omitted.

    Returns:
        The EU bar markup, or the empty-state sentinel.
    """
    if row.eu_total <= 0:
        return EMPTY_STATE
    return render_bar_rich(row.eu_consumed, row.eu_total, mode=mode, palette=palette)


def _totals_phase_cell(
    totals: PortfolioTotals,
    *,
    mode: RenderMode,
    palette: Mapping[str, str] | None = None,
    bar_cells: int = _WIDE_BAR_CELLS,
) -> str:
    """Render the totals row's phase cell: the summed brand-accent-tinted bar.

    Mirrors :func:`_phase_cell` shape (a leading label + a tinted completion
    bar) so the summary row lines up under the per-repo phase column, but
    carries the BRAND accent (:func:`_brand_bar_markup`) rather than the
    per-repo band ``ok`` green so the roll-up reads in the portfolio's own
    brand voice. The label is the repo count rather than a phase id, since the
    totals span every repo's active phase.

    Args:
        totals: The folded :class:`PortfolioTotals`.
        mode: The active bar render mode.
        palette: The status-tint band map (see :func:`_band_palette`);
            defaults to the built-in palette + brand accent when omitted.
        bar_cells: The completion bar's cell count -- shrunk on a narrow grid
            (see :func:`phase_bar_cells`) so the degraded totals row fits an
            80-cell terminal while the bar keeps its brand tint.

    Returns:
        The totals phase cell (``<repo_count> repos <brand-bar> done/total``).
    """
    bar = render_completion_bar(totals.wave_done, totals.wave_total, width=bar_cells, mode=mode)
    colours = palette if palette is not None else _DEFAULT_BRAND_PALETTE
    return f"{totals.repo_count} repos {_brand_bar_markup(bar, palette=colours)}"


def _totals_eu_cell(
    totals: PortfolioTotals, *, mode: RenderMode, palette: Mapping[str, str] | None = None
) -> str:
    """Render the totals row's EU cell, or the empty sentinel.

    Mirrors :func:`_eu_cell`: a status-tinted EU-burn bar over the summed
    consumed / total EU, or the empty-state sentinel when no repo reported
    an EU estimate.

    Args:
        totals: The folded :class:`PortfolioTotals`.
        mode: The active bar render mode.
        palette: Band-colour map (see :func:`_band_palette`).

    Returns:
        The totals EU bar markup, or the empty-state sentinel.
    """
    if totals.eu_total <= 0:
        return EMPTY_STATE
    return render_bar_rich(totals.eu_consumed, totals.eu_total, mode=mode, palette=palette)


class WorkspaceTable(DataTable[str]):
    """Per-repo workspace grid with a live git column + zoom-on-Enter.

    Public surface for a host screen:

    * :attr:`state` — assign the bound workspace state; the rows rebuild.
    * :meth:`refresh_git` — re-probe every repo's git column (subject to
      the per-repo TTL cache); bind to the host's refresh tick.
    * :meth:`focused_repo` — the repo code under the row cursor, or
      ``None`` (the host reloads the zoom target off this).
    * :class:`RowZoomed` — posted on Enter / z; carries the repo code the
      host zooms into a 2x2 quadrant.
    """

    DEFAULT_CSS: ClassVar[str] = """
    WorkspaceTable {
        height: 1fr;
        width: 1fr;
        overflow-x: hidden;
    }
    """

    class RowZoomed(Message):
        """Posted when the operator zooms a repo row (Enter / z).

        The host :class:`~eawf.surfaces.tui.scopes.workspace.WorkspaceScreen`
        mounts the focused repo's 2x2 quadrant in response.

        Attributes:
            repo_code: The zoomed repo's project code (the row key).
        """

        def __init__(self, repo_code: str) -> None:
            self.repo_code = repo_code
            super().__init__()

    #: Bound workspace state, watched so a fresh revision rebuilds rows.
    state: reactive[State | None] = reactive(None)

    #: Active bar render mode, watched so a Braille ↔ ASCII flip repaints
    #: the bar cells. Seeded from the App's reactive on mount.
    render_mode: reactive[RenderMode] = reactive[RenderMode](DEFAULT_RENDER_MODE)

    def __init__(self, **kwargs: Any) -> None:
        """Construct the table with row-cursor selection.

        Args:
            **kwargs: Forwarded to :class:`textual.widgets.DataTable`.
        """
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._rebuilding = False
        #: Per-repo cached git status cell text.
        self._git_cells: dict[str, str] = {}
        #: Monotonic timestamp of the last probe per repo, for the TTL.
        self._git_probed_at: dict[str, float] = {}
        #: The column set last installed on the table, so a rebuild only
        #: re-adds columns when the width crosses the narrow threshold (a
        #: same-width rebuild keeps the existing columns).
        self._installed_columns: tuple[str, ...] = ()

    def on_mount(self) -> None:
        """Add columns, seed state + render mode from the app, watch both."""
        self._install_columns(visible_columns(self._content_width()))
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        app_mode = getattr(self.app, "render_mode", None)
        if app_mode is not None:
            self.render_mode = app_mode
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_app_render_mode)
        self._rebuild()
        self.refresh_git(force=True)

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def _on_app_render_mode(self, mode: RenderMode) -> None:
        """Mirror an app-level render-mode flip onto this widget's reactive."""
        self.render_mode = mode

    def watch_state(self) -> None:
        """Rebuild rows + re-probe git when the bound state changes."""
        self._rebuild()
        self.refresh_git(force=True)

    def watch_render_mode(self) -> None:
        """Rebuild rows so the bar cells repaint in the new glyph set."""
        self._rebuild()

    def on_resize(self, event: Resize) -> None:
        """Re-cut the column set + bar width to the new grid width on resize.

        A shrink past :data:`_NARROW_WIDTH_THRESHOLD` degrades the row to the
        load-bearing :data:`_NARROW_COLUMNS` (dropping ``git`` / ``pr`` /
        ``age``) and narrows the phase bar, so the row reflows inside the pane
        rather than overflowing the ``overflow-x: hidden`` edge and clipping the
        leading sigil; a grow back restores the full row.

        Args:
            event: The Textual resize event (unused; the new width is read from
                :attr:`size` during the rebuild).
        """
        del event
        self._rebuild()

    def _content_width(self) -> int:
        """Return the grid's measured content width in cells.

        The width the responsive degrade reads (``0`` pre-layout / bare harness,
        which both width helpers treat as the wide no-clip regime).
        """
        return self.size.width

    def _install_columns(self, columns: tuple[str, ...]) -> None:
        """Replace the table's columns with *columns*, recording the new set.

        Clears every row + column (``clear(columns=True)``) and re-adds the
        target columns keyed by id, so :meth:`add_row` / ``get_cell`` keep
        addressing cells by the same keys across a width-regime change.

        Args:
            columns: The ordered column-id tuple to install.
        """
        self.clear(columns=True)
        for column in columns:
            self.add_column(column, key=column)
        self._installed_columns = columns

    def rows_data(self) -> list[RepoRow]:
        """Return the current repo rows (pure accessor for host / tests)."""
        return build_repo_rows(self.state)

    def focused_repo(self) -> str | None:
        """Return the repo code under the row cursor, or ``None``.

        The host reads this to reload the zoom target so a re-zoom always
        scopes to the current focus rather than a cached target.

        Returns:
            The focused repo code, or ``None`` when the table is empty.
        """
        rows = self.rows_data()
        if not rows:
            return None
        index = self.cursor_row
        if index < 0 or index >= len(rows):
            index = 0
        return rows[index].code

    def refresh_git(self, *, force: bool = False) -> None:
        """Re-probe every repo's git column off the event loop.

        Each repo's probe is subject to the per-repo
        :data:`GIT_CACHE_TTL_S` cache: a refresh inside the window reuses
        the cached cell. ``exclusive`` drops any in-flight probe group so
        back-to-back ticks coalesce. The probe runs in a worker so a slow
        or hung ``git`` never blocks the render loop.

        Args:
            force: When ``True``, bypass the TTL cache and probe every
                repo immediately (used on mount + state revision).
        """
        rows = self.rows_data()
        now = time.monotonic()
        for row in rows:
            last = self._git_probed_at.get(row.code)
            if not force and last is not None and now - last < GIT_CACHE_TTL_S:
                continue
            self._git_probed_at[row.code] = now
            if row.code not in self._git_cells:
                self._git_cells[row.code] = GIT_PENDING_CELL
            # Pass a zero-arg coroutine factory (not a coroutine object) so an
            # ``exclusive`` worker that supersedes a not-yet-started one never
            # leaves an un-awaited coroutine — Textual builds the coroutine
            # only when it actually runs the worker.
            self.run_worker(
                self._probe_factory(row.code, row.path),
                group=f"git-probe-{row.code}",
                exclusive=True,
            )
        self._rebuild()

    def _probe_factory(self, repo_code: str, repo_path: str) -> Callable[[], Awaitable[None]]:
        """Return a zero-arg coroutine factory bound to *repo_code* / *repo_path*."""

        async def _run() -> None:
            await self._probe_git(repo_code, repo_path)

        return _run

    async def _probe_git(self, repo_code: str, repo_path: str) -> None:
        """Worker body: probe *repo_path* off-thread, then repaint its cell.

        A probe that resolves no branch (missing binary / non-git path /
        timeout) dims the cell to :data:`GIT_UNAVAILABLE_CELL`; a clean /
        dirty tree renders the porcelain status summary. Never raises out
        of the worker so one bad repo never tears down the table.

        Args:
            repo_code: The repo whose cell this probe repaints.
            repo_path: The repo working-tree root to probe.
        """
        fields = await asyncio.to_thread(gather_git_fields, Path(repo_path))
        self._git_cells[repo_code] = _git_cell_text(fields)
        self._rebuild()

    def _rebuild(self) -> None:
        """Repopulate the rows from the current state + git cells + mode + width.

        Width-responsive: the column set + the phase bar width are re-cut to
        the grid's current content width (see :func:`visible_columns` /
        :func:`phase_bar_cells`) so a narrow terminal degrades to the
        load-bearing ``repo / phase / eu`` columns rather than overflowing the
        ``overflow-x: hidden`` pane edge and clipping the leading sigil. Columns
        are re-installed only when the visible set crosses the narrow threshold
        (a same-regime rebuild keeps them).

        The :attr:`_rebuilding` re-entrancy guard coalesces the nested
        calls the ``state`` / ``render_mode`` watchers + the explicit
        :meth:`on_mount` call can trigger in one flush, mirroring the
        :class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable` guard.
        """
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            width = self._content_width()
            columns = visible_columns(width)
            bar_cells = phase_bar_cells(width)
            if columns != self._installed_columns:
                self._install_columns(columns)
            else:
                self.clear()
            if not self.columns:
                return
            palette = _band_palette(self.app)
            rows = self.rows_data()
            for row in rows:
                cells = self._repo_cell_map(row, palette=palette, bar_cells=bar_cells)
                self.add_row(*(cells[col] for col in columns), key=row.code)
            self._add_totals_row(rows, columns, palette=palette, bar_cells=bar_cells)
        finally:
            self._rebuilding = False

    def _repo_cell_map(
        self, row: RepoRow, *, palette: Mapping[str, str], bar_cells: int
    ) -> dict[str, str]:
        """Return *row*'s cells keyed by column id (the full six-column set).

        The width-responsive rebuild slices this map by the visible column set,
        so every cell renderer lives in one place regardless of which columns
        the current width drops. The repo cell always carries its leading
        lifecycle sigil + warn-marker status tint.

        Args:
            row: The repo row to render.
            palette: The status-tint band map (see :func:`_band_palette`).
            bar_cells: The phase-bar cell count for the current width (see
                :func:`phase_bar_cells`).

        Returns:
            A ``{column_id: cell_text}`` map over every :data:`_COLUMNS` id.
        """
        return {
            "repo": _repo_cell_markup(row, mode=self.render_mode, palette=palette),
            "phase": _phase_cell(row, mode=self.render_mode, palette=palette, bar_cells=bar_cells),
            "eu": _eu_cell(row, mode=self.render_mode, palette=palette),
            "git": self._git_cells.get(row.code, GIT_PENDING_CELL),
            "pr": _pr_cell(row.open_prs),
            "age": row.age,
        }

    def _add_totals_row(
        self,
        rows: list[RepoRow],
        columns: tuple[str, ...],
        *,
        palette: Mapping[str, str],
        bar_cells: int,
    ) -> None:
        """Append the portfolio-totals summary row under the repo rows.

        Folds *rows* via :func:`portfolio_totals` and renders one summary row in
        the brand voice (the repo column leads with the totals' brand-tinted
        lifecycle sigil + the :data:`TOTALS_ROW_LABEL` sigma; the phase / eu
        cells carry the summed bars). The row is sliced to the visible *columns*
        so the summary degrades with the per-repo rows at a narrow width. An
        empty repo set adds no totals row (nothing to sum).

        Args:
            rows: The repo rows already added above the totals row.
            columns: The visible column-id set the row is sliced to.
            palette: Band-colour map for the bars + sigil (see
                :func:`_band_palette`).
            bar_cells: The phase-bar cell count for the current width (see
                :func:`phase_bar_cells`).
        """
        if not rows:
            return
        totals = portfolio_totals(rows)
        sigil = _totals_sigil_markup(
            totals_row_sigil(totals), mode=self.render_mode, palette=palette
        )
        cells = {
            "repo": f"{sigil} {TOTALS_ROW_LABEL}",
            "phase": _totals_phase_cell(
                totals, mode=self.render_mode, palette=palette, bar_cells=bar_cells
            ),
            "eu": _totals_eu_cell(totals, mode=self.render_mode, palette=palette),
            "git": "",
            "pr": _pr_cell(totals.open_prs),
            "age": "",
        }
        self.add_row(*(cells[col] for col in columns), key=TOTALS_ROW_KEY)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Post :class:`RowZoomed` for the Enter-selected row.

        Args:
            event: The Textual row-selected event; ``row_key.value`` is
                the repo code used as the row key.
        """
        repo_code = event.row_key.value
        if repo_code is not None and repo_code != TOTALS_ROW_KEY:
            self.post_message(self.RowZoomed(repo_code))

    def action_zoom_row(self) -> None:
        """Zoom the focused repo (the ``z`` alias for Enter)."""
        repo_code = self.focused_repo()
        if repo_code is not None:
            self.post_message(self.RowZoomed(repo_code))


def _git_cell_text(fields: object) -> str:
    """Render a :class:`~eawf.surfaces.tui.widgets.git_pane.GitFields` into a cell.

    A probe that resolved no branch (the :data:`~eawf.surfaces.tui.widgets.git_pane.DASH`
    sentinel) dims to :data:`GIT_UNAVAILABLE_CELL`; otherwise the cell is
    the dirty/clean status summary.

    Args:
        fields: The probed :class:`~eawf.surfaces.tui.widgets.git_pane.GitFields`.

    Returns:
        The git column cell text.
    """
    from eawf.surfaces.tui.widgets.git_pane import DASH, GitFields

    if not isinstance(fields, GitFields) or fields.branch == DASH:
        return GIT_UNAVAILABLE_CELL
    return fields.dirty


__all__ = [
    "BLOCKER_CHIP",
    "GIT_CACHE_TTL_S",
    "GIT_PENDING_CELL",
    "GIT_UNAVAILABLE_CELL",
    "STALE_CHIP",
    "TOTALS_ROW_KEY",
    "TOTALS_ROW_LABEL",
    "WARN_BLOCKER_TEXT",
    "WARN_STALE_TEXT",
    "PortfolioTotals",
    "RepoRow",
    "WorkspaceTable",
    "active_phase_completion",
    "attention_chip",
    "build_repo_rows",
    "completion_pair",
    "eu_pair",
    "format_totals_line",
    "phase_bar_cells",
    "portfolio_totals",
    "repo_has_blocker",
    "repo_is_stale",
    "repo_row_from_path",
    "repo_row_sigil",
    "totals_row_sigil",
    "visible_columns",
    "warn_chip_markup",
]
