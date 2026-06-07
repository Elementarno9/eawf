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

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable

from eawf.platform.registry.staleness import read_repo_state
from eawf.surfaces.tui.widgets.eu_bar import (
    DEFAULT_BAND_PALETTE,
    DEFAULT_RENDER_MODE,
    EMPTY_STATE,
    RenderMode,
    render_bar_rich,
    render_completion_bar,
)
from eawf.surfaces.tui.widgets.git_pane import gather_git_fields

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State, WorkspaceRepoRef

logger = logging.getLogger(__name__)

#: Column ids in display order. ``repo`` and ``age`` are fixed-shape; the
#: two bar columns + git absorb the middle of the row; ``pr`` carries the
#: open-PR total per repo between the live git cell and the age cell.
_COLUMNS: tuple[str, ...] = ("repo", "phase", "eu", "git", "pr", "age")

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


def _phase_cell(row: RepoRow, *, mode: RenderMode) -> str:
    """Render *row*'s active-phase id + completion bar cell (status-tinted)."""
    bar = render_completion_bar(row.phase_done, row.phase_total, width=6, mode=mode)
    return f"{row.phase_id or '—'} {bar}"


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
    """Resolve the EU-burn band colours from the app's active theme.

    DataTable ``str`` cells are Rich-parsed and cannot resolve the Textual
    ``$ok`` / ``$warn`` / ``$err`` palette vars, so the tint must be baked to
    a concrete hex at row-build time. Falls back to
    :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_BAND_PALETTE` when the active
    theme is unavailable (e.g. an unmounted test harness).

    Args:
        app: The host app whose active theme carries the palette.

    Returns:
        A ``{"ok"|"warn"|"err": "#rrggbb"}`` map, theme values where present
        and the default palette otherwise.
    """
    theme = getattr(app, "current_theme", None)
    variables = getattr(theme, "variables", None) or {}
    return {key: variables.get(key, default) for key, default in DEFAULT_BAND_PALETTE.items()}


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


def _totals_phase_cell(totals: PortfolioTotals, *, mode: RenderMode) -> str:
    """Render the totals row's phase cell: the summed completion bar.

    Mirrors :func:`_phase_cell` shape (a leading label + a completion bar)
    so the summary row lines up under the per-repo phase column. The label
    is the repo count rather than a phase id, since the totals span every
    repo's active phase.

    Args:
        totals: The folded :class:`PortfolioTotals`.
        mode: The active bar render mode.

    Returns:
        The totals phase cell (``<repo_count> repos <bar> done/total``).
    """
    bar = render_completion_bar(totals.wave_done, totals.wave_total, width=6, mode=mode)
    return f"{totals.repo_count} repos {bar}"


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

    def on_mount(self) -> None:
        """Add columns, seed state + render mode from the app, watch both."""
        for column in _COLUMNS:
            self.add_column(column, key=column)
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
        """Repopulate the rows from the current state + git cells + mode.

        The :attr:`_rebuilding` re-entrancy guard coalesces the nested
        calls the ``state`` / ``render_mode`` watchers + the explicit
        :meth:`on_mount` call can trigger in one flush, mirroring the
        :class:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable` guard.
        """
        if not self.columns or self._rebuilding:
            return
        self._rebuilding = True
        try:
            self.clear()
            palette = _band_palette(self.app)
            rows = self.rows_data()
            for row in rows:
                git_cell = self._git_cells.get(row.code, GIT_PENDING_CELL)
                self.add_row(
                    _repo_cell(row),
                    _phase_cell(row, mode=self.render_mode),
                    _eu_cell(row, mode=self.render_mode, palette=palette),
                    git_cell,
                    _pr_cell(row.open_prs),
                    row.age,
                    key=row.code,
                )
            self._add_totals_row(rows, palette=palette)
        finally:
            self._rebuilding = False

    def _add_totals_row(self, rows: list[RepoRow], *, palette: Mapping[str, str]) -> None:
        """Append the portfolio-totals summary row under the repo rows.

        Folds *rows* via :func:`portfolio_totals` and renders one summary
        row whose phase + EU cells carry the portfolio-wide completion + EU
        bars and whose PR cell carries the summed open-PR count. The git +
        age columns are blank on the summary row (no live probe spans the
        portfolio); the repo column carries the :data:`TOTALS_ROW_LABEL`
        sigma so the row reads as a roll-up rather than a repo.

        Args:
            rows: The repo rows already added above the totals row.
            palette: Band-colour map for the EU bar (see :func:`_band_palette`).
        """
        totals = portfolio_totals(rows)
        self.add_row(
            TOTALS_ROW_LABEL,
            _totals_phase_cell(totals, mode=self.render_mode),
            _totals_eu_cell(totals, mode=self.render_mode, palette=palette),
            "",
            _pr_cell(totals.open_prs),
            "",
            key=TOTALS_ROW_KEY,
        )

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
    "PortfolioTotals",
    "RepoRow",
    "WorkspaceTable",
    "active_phase_completion",
    "attention_chip",
    "build_repo_rows",
    "completion_pair",
    "eu_pair",
    "format_totals_line",
    "portfolio_totals",
    "repo_has_blocker",
    "repo_is_stale",
    "repo_row_from_path",
]
