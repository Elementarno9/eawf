"""User-scope portfolio dashboard for the Eä Rich TUI (P20-I01-W06).

Distinct from the workspace dashboard (:mod:`eawf.tui.workspace`, W05):
the portfolio is a **read-only summary table** of every repo in
``~/.eawf/registry.json`` with a one-row-per-repo snapshot of key
metrics — active phase, open iter, ready-wave count, stale flag — so
the operator can scan the entire portfolio without drilling into any
single repo's quadrant.

Layout sketch::

    +-------------------------------------------------------------------+
    | Eä  portfolio (3 repos)                                           |  ← header
    +-------------------------------------------------------------------+
    |                       portfolio summary                           |
    | code      title       phase       iter        ready  stale active |
    | EAWF      Eä          P20 active  P20-I01     2      no    yes    |
    | DEMO      Demo        P03 active  P03-I02     0      no    no     |
    | OTHER     Other       (none)      (none)      0      yes   no     |
    +-------------------------------------------------------------------+
    | ↑↓ navigate  Enter open  Esc back  q quit                         |  ← footer
    +-------------------------------------------------------------------+

**Strict read-only registry surface.** Per the
``feedback_explicit_registry_only`` memory note this module never
grows the registry. It calls :func:`eawf.registry.read_registry`
(W05 helper) and bails to an empty-table placeholder when the
registry is missing or malformed. Repo-state reads go through
:func:`eawf.registry.read_repo_state` so the staleness signals
stay consistent between the W05 workspace strip and the W06
portfolio table.

**Builds on W05.** The :class:`Registry` model + the
:func:`read_registry` / :func:`read_repo_state` / :func:`is_stale`
helpers come from :mod:`eawf.registry` (W05). The header chassis
(``Eä`` brand outside-left) reuses :data:`eawf.tui.layout.BRAND`
and :data:`eawf.tui.layout.BRAND_STYLE` so the brand surface stays
byte-identical across W02 / W05 / W06.

**Out of scope.** This module does not mutate registry, does not
write any state, does not invoke W07's audit overlay, and does
not touch W09's state model. Live keypress wiring (Rich Live loop)
will land in a later wave; for now :func:`offline_render` is the
public entry point and the W06 wave-board reuses it for headless
preview.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from eawf.registry import (
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    is_stale,
    read_registry,
    read_repo_state,
    registry_mtime,
)
from eawf.tui.layout import BRAND, BRAND_STYLE, BREADCRUMB_STYLE

logger = logging.getLogger(__name__)


#: Footer keymap hint for the portfolio dashboard. Lists navigation
#: keys first so the operator sees row-cycling ahead of the higher-
#: level shortcuts. ``Enter open`` drills into the selected repo's
#: workspace dashboard (caller wires the transition); ``q``/``Esc``
#: exits.
PORTFOLIO_FOOTER_KEYMAP: str = "↑↓ navigate  Enter open  Esc back  q quit  (vim: j k)"

#: Style applied to the selected row's cells when the dashboard
#: renders an interactive cursor.
SELECTED_ROW_STYLE: str = "bold cyan"

#: Style applied to the active-repo marker chip.
ACTIVE_MARKER_STYLE: str = "bold green"

#: Style applied to the stale-flag chip.
STALE_MARKER_STYLE: str = "yellow"

#: Placeholder rendered into the title when the registry has no
#: repos. Keeps the panel structure stable so the empty branch is
#: not mistaken for a render bug.
_EMPTY_TITLE: str = "portfolio (no repos registered — run `eawf init`)"

#: Placeholder rendered when the registry file failed to load. The
#: empty branch above is "successful read with zero entries"; this
#: branch is "could not read at all".
_UNAVAILABLE_TITLE: str = "portfolio (registry unavailable)"

#: Placeholder strings for cells where the repo's state.json is
#: missing or has no value for the field. Bare ``-`` chosen to match
#: the W02 quadrant's "no value" convention.
_MISSING_CELL: str = "-"


# ---------------------------------------------------------------------------
# View state
# ---------------------------------------------------------------------------


class PortfolioViewState(BaseModel):
    """Ephemeral view state for the portfolio dashboard.

    Tracks the row cursor only — the dashboard is read-only so there
    is no "focused" concept distinct from "selected". Strict
    (``extra="forbid"``) so a typo at construction fails fast.

    Attributes:
        selected_index: 0-based cursor into the sorted repo list.
            Sorted alphabetical-by-code (same order as the W05 strip)
            so the cursor binding stays stable across renders.
    """

    model_config = ConfigDict(extra="forbid")

    selected_index: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Per-repo metric extraction (pure; state-shape agnostic)
# ---------------------------------------------------------------------------


class PortfolioRow(BaseModel):
    """One row in the portfolio summary table.

    Aggregates the loaded :class:`RegistryRepoEntry` with metrics
    pulled from the repo's ``.ea/state.json`` so the table renderer
    stays decoupled from state-shape concerns. Strict
    (``extra="forbid"``).

    Attributes:
        code: Project code (mirrors :class:`RegistryRepoEntry.code`).
        title: Display title; falls back to ``code`` when absent.
        active_phase: ``state.current.phase_id`` when known. ``None``
            when the repo state file is missing/unreadable.
        active_phase_status: PhaseStatus string for ``active_phase``
            (e.g. ``active``, ``planned``, ``closed``). ``None`` when
            the phase row is missing.
        open_iter: ``state.current.iter_id`` when known.
        open_iter_status: IterStatus string for ``open_iter``.
        ready_waves: Count of waves whose status is ``pending`` AND
            whose ``deps`` are all closed (or empty) — i.e. waves the
            DAG would dispatch next. Zero when no wave plan exists.
        stale: ``True`` when any of the three staleness signals fires
            (registry mtime, repo state mtime, state load failure).
        active: ``True`` when this entry matches the registry's
            ``active_code`` field.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    title: str
    active_phase: str | None = None
    active_phase_status: str | None = None
    open_iter: str | None = None
    open_iter_status: str | None = None
    ready_waves: int = 0
    stale: bool = False
    active: bool = False


def _count_ready_waves(state: dict[str, Any]) -> int:
    """Count waves whose status is ``pending`` with all deps closed.

    Pure function over the state dict. A wave is "ready" when:

    - its status is ``pending`` (not yet claimed), AND
    - every wave id in its ``deps`` list refers to a wave whose
      status is ``closed``.

    Unknown deps (referencing a wave the state dict does not list)
    count as un-met so we never over-report readiness.

    Args:
        state: Loaded repo state dict; missing keys are treated as
            empty.

    Returns:
        Non-negative integer count.
    """
    waves = state.get("waves") or {}
    if not isinstance(waves, dict):
        return 0
    closed_codes: set[str] = {
        wave_id
        for wave_id, wave in waves.items()
        if isinstance(wave, dict) and wave.get("status") == "closed"
    }
    ready = 0
    for wave in waves.values():
        if not isinstance(wave, dict):
            continue
        if wave.get("status") != "pending":
            continue
        deps = wave.get("deps") or []
        if not isinstance(deps, list):
            continue
        if all(dep in closed_codes for dep in deps):
            ready += 1
    return ready


def _resolve_phase_status(state: dict[str, Any], phase_id: str | None) -> str | None:
    """Return the status string for *phase_id* from the state dict."""
    if not phase_id:
        return None
    phases = state.get("phases") or {}
    if not isinstance(phases, dict):
        return None
    phase = phases.get(phase_id)
    if not isinstance(phase, dict):
        return None
    status = phase.get("status")
    return status if isinstance(status, str) else None


def _resolve_iter_status(state: dict[str, Any], iter_id: str | None) -> str | None:
    """Return the status string for *iter_id* from the state dict."""
    if not iter_id:
        return None
    iters = state.get("iters") or {}
    if not isinstance(iters, dict):
        return None
    iter_row = iters.get(iter_id)
    if not isinstance(iter_row, dict):
        return None
    status = iter_row.get("status")
    return status if isinstance(status, str) else None


def build_row(
    entry: RegistryRepoEntry,
    *,
    registry_active_code: str | None,
    is_stale_for_entry: bool,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
) -> PortfolioRow:
    """Compose one :class:`PortfolioRow` from a registry entry.

    Loads the repo's ``.ea/state.json`` lazily through
    *repo_state_loader* (defaults to :func:`eawf.registry.read_repo_state`)
    so tests inject deterministic state without touching the real
    filesystem.

    Args:
        entry: Registry entry for the repo.
        registry_active_code: ``Registry.active_code``; used to mark
            the row as the active repo.
        is_stale_for_entry: Pre-computed stale verdict (computed by
            :func:`build_rows` so the registry mtime is read once
            per render).
        repo_state_loader: Test seam for the state loader.

    Returns:
        Populated :class:`PortfolioRow`. When the repo state is
        unreadable the row keeps ``None`` placeholders for phase /
        iter / status fields and ``ready_waves=0``.
    """
    loader = repo_state_loader or read_repo_state
    state = loader(Path(entry.path))
    if state is None:
        return PortfolioRow(
            code=entry.code,
            title=entry.title or entry.code,
            stale=is_stale_for_entry,
            active=(entry.code == registry_active_code),
        )
    current = state.get("current") or {}
    phase_id = current.get("phase_id") if isinstance(current, dict) else None
    iter_id = current.get("iter_id") if isinstance(current, dict) else None
    return PortfolioRow(
        code=entry.code,
        title=entry.title or entry.code,
        active_phase=phase_id if isinstance(phase_id, str) else None,
        active_phase_status=_resolve_phase_status(state, phase_id),
        open_iter=iter_id if isinstance(iter_id, str) else None,
        open_iter_status=_resolve_iter_status(state, iter_id),
        ready_waves=_count_ready_waves(state),
        stale=is_stale_for_entry,
        active=(entry.code == registry_active_code),
    )


def build_rows(
    registry: Registry,
    *,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
) -> list[PortfolioRow]:
    """Compose every :class:`PortfolioRow` for *registry*.

    Sorts alphabetically by code so the table render stays
    deterministic across reloads (matches W05's strip ordering).

    Args:
        registry: Loaded :class:`Registry`.
        now: Override for the "current" timestamp passed to
            :func:`eawf.registry.is_stale`. Tests inject a fixed
            value.
        registry_mtime_at: Pre-resolved registry mtime; threaded
            into :func:`eawf.registry.is_stale`.
        repo_state_loader: Test seam for the repo state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate. Goldens inject a deterministic predicate so
            the snapshot does not depend on filesystem state.

    Returns:
        List of :class:`PortfolioRow`, alphabetical by code.
    """
    evaluator = is_stale_evaluator or (
        lambda entry: is_stale(entry, registry_mtime_at=registry_mtime_at, now=now)
    )
    return [
        build_row(
            registry.repos[code],
            registry_active_code=registry.active_code,
            is_stale_for_entry=evaluator(registry.repos[code]),
            repo_state_loader=repo_state_loader,
        )
        for code in sorted(registry.repos)
    ]


# ---------------------------------------------------------------------------
# Header / footer / table builders
# ---------------------------------------------------------------------------


def build_portfolio_header(registry: Registry | None) -> Panel:
    """Build the top-strip header Panel for the portfolio dashboard.

    The header carries the ``Eä`` brand outside-left followed by a
    ``portfolio (N repos)`` summary string so the operator sees the
    fleet size at a glance. When *registry* is ``None`` the count
    placeholder reads ``unavailable``.
    """
    count_segment = (
        f"portfolio ({len(registry.repos)} repos)"
        if registry is not None
        else "portfolio (unavailable)"
    )
    text = Text()
    text.append(f"{BRAND}  ", style=BRAND_STYLE)
    text.append(count_segment, style=BREADCRUMB_STYLE)
    return Panel(text, title=None, border_style="dim")


def build_portfolio_footer() -> Panel:
    """Portfolio-specific footer hint with navigation keys first."""
    return Panel(Text(PORTFOLIO_FOOTER_KEYMAP), title=None, border_style="dim")


def _format_phase_cell(row: PortfolioRow) -> str:
    """Render the phase cell as ``<phase> <status>`` or ``-``."""
    if not row.active_phase:
        return _MISSING_CELL
    if row.active_phase_status:
        return f"{row.active_phase} {row.active_phase_status}"
    return row.active_phase


def _format_iter_cell(row: PortfolioRow) -> str:
    """Render the iter cell as ``<iter> <status>`` or ``-``."""
    if not row.open_iter:
        return _MISSING_CELL
    if row.open_iter_status:
        return f"{row.open_iter} {row.open_iter_status}"
    return row.open_iter


def _format_marker_cell(*, flag: bool, label: str, style: str) -> Text:
    """Render an active/stale marker as styled Text (``yes``/``no``)."""
    text = Text()
    if flag:
        text.append(label, style=style)
    else:
        text.append("no", style="dim")
    return text


def build_portfolio_table(
    rows: list[PortfolioRow],
    *,
    view: PortfolioViewState | None = None,
) -> Table:
    """Compose a Rich :class:`Table` from the portfolio rows.

    Columns (in render order): ``code``, ``title``, ``phase``,
    ``iter``, ``ready``, ``stale``, ``active``. Selected-row
    highlighting comes from *view*; when omitted no row carries
    selected-style.

    Args:
        rows: Output of :func:`build_rows`.
        view: Optional view state. The row at
            ``view.selected_index`` (clamped) gets the
            :data:`SELECTED_ROW_STYLE`.

    Returns:
        A fully-populated :class:`rich.table.Table`.
    """
    selected_idx: int | None = None
    if view is not None and rows:
        selected_idx = max(0, min(view.selected_index, len(rows) - 1))
    table = Table(
        title=None,
        show_header=True,
        header_style="bold",
        expand=True,
        pad_edge=False,
    )
    table.add_column("code", no_wrap=True)
    table.add_column("title")
    table.add_column("phase", no_wrap=True)
    table.add_column("iter", no_wrap=True)
    table.add_column("ready", justify="right", no_wrap=True)
    table.add_column("stale", justify="center", no_wrap=True)
    table.add_column("active", justify="center", no_wrap=True)
    for idx, row in enumerate(rows):
        code_style = SELECTED_ROW_STYLE if idx == selected_idx else None
        code_text = Text(row.code, style=code_style or "")
        title_text = Text(row.title, style=code_style or "")
        phase_text = Text(_format_phase_cell(row), style=code_style or "")
        iter_text = Text(_format_iter_cell(row), style=code_style or "")
        ready_text = Text(str(row.ready_waves), style=code_style or "")
        stale_text = _format_marker_cell(flag=row.stale, label="yes", style=STALE_MARKER_STYLE)
        active_text = _format_marker_cell(flag=row.active, label="yes", style=ACTIVE_MARKER_STYLE)
        table.add_row(
            code_text,
            title_text,
            phase_text,
            iter_text,
            ready_text,
            stale_text,
            active_text,
        )
    return table


def build_portfolio_panel(
    rows: list[PortfolioRow],
    *,
    view: PortfolioViewState | None = None,
    title: str | None = None,
) -> Panel:
    """Wrap the portfolio table inside a titled Rich Panel.

    When *rows* is empty the panel body falls back to a placeholder
    string so the operator sees a meaningful message rather than a
    blank table frame.
    """
    if not rows:
        body: Text | Table = Text(
            "no repos registered — run `eawf repo add <path>` first",
            style="dim",
        )
    else:
        body = build_portfolio_table(rows, view=view)
    return Panel(body, title=title or "portfolio summary", border_style="dim")


# ---------------------------------------------------------------------------
# Frame composition + offline render
# ---------------------------------------------------------------------------


def build_portfolio_frame(
    registry: Registry | None,
    *,
    view: PortfolioViewState | None = None,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
) -> Layout:
    """Assemble header + body (portfolio table) + footer into a Layout.

    When the registry is ``None`` (read failed) the body falls back
    to an unavailable-title panel so the frame structure stays
    stable regardless of registry state.

    Args:
        registry: Loaded :class:`Registry` or ``None`` for the
            read-failed branch.
        view: Optional view state; defaults to ``None`` which leaves
            row highlighting off (suitable for snapshot rendering).
        now: Override for the current timestamp.
        registry_mtime_at: Pre-resolved registry mtime threaded into
            :func:`eawf.registry.is_stale`.
        repo_state_loader: Test seam for the repo state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate. Goldens inject a deterministic predicate so
            the snapshot does not depend on filesystem state.

    Returns:
        Rich :class:`Layout` ready for :class:`rich.live.Live` or
        :func:`render_portfolio`.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["header"].update(build_portfolio_header(registry))
    if registry is None:
        layout["body"].update(
            Panel(
                Text("registry unavailable (read failed)", style="dim"),
                title=_UNAVAILABLE_TITLE,
                border_style="dim",
            )
        )
    else:
        rows = build_rows(
            registry,
            now=now,
            registry_mtime_at=registry_mtime_at,
            repo_state_loader=repo_state_loader,
            is_stale_evaluator=is_stale_evaluator,
        )
        title = _EMPTY_TITLE if not rows else "portfolio summary"
        layout["body"].update(build_portfolio_panel(rows, view=view, title=title))
    layout["footer"].update(build_portfolio_footer())
    return layout


def render_portfolio(
    registry: Registry | None,
    *,
    view: PortfolioViewState | None = None,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
    console: Console | None = None,
    width: int = 100,
) -> str:
    """Render one portfolio frame into a string buffer.

    Mirrors :func:`eawf.tui.workspace.render_workspace` so the
    portfolio dashboard's offline path matches the workspace
    dashboard's offline path — both produce a captured-text frame
    suitable for golden snapshots, headless ``--plain`` mode, and
    one-shot CLI previews.

    Args:
        registry: Loaded registry, or ``None`` for the read-failed
            branch.
        view: Optional view state; threaded through to
            :func:`build_portfolio_frame`.
        now: Override for the current timestamp.
        registry_mtime_at: Pre-resolved registry mtime.
        repo_state_loader: Test seam for the repo state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate; threaded through to :func:`build_portfolio_frame`.
        console: Optional pre-built console; when supplied the helper
            writes into the caller's console and returns ``""``.
        width: Console width passed to the default :class:`Console`.

    Returns:
        Captured render text when ``console`` is ``None``; empty
        string otherwise.
    """
    buf = io.StringIO()
    real_console = console or Console(file=buf, force_terminal=False, width=width, record=False)
    layout = build_portfolio_frame(
        registry,
        view=view,
        now=now,
        registry_mtime_at=registry_mtime_at,
        repo_state_loader=repo_state_loader,
        is_stale_evaluator=is_stale_evaluator,
    )
    real_console.print(layout)
    return buf.getvalue() if console is None else ""


def offline_render(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
    width: int = 100,
) -> str:
    """Render one portfolio frame end-to-end from a registry path.

    Wraps :func:`read_registry` + :func:`render_portfolio` so a
    single helper resolves the registry, computes its filesystem
    mtime, and emits a frozen frame. Used by:

    - Golden snapshot tests under
      ``tests/golden/tui/portfolio_*.txt``.
    - The ``eawf repo`` CLI's portfolio preview hook (forthcoming).

    Args:
        registry_path: Explicit registry path; ``None`` falls back
            to :func:`eawf.registry.default_registry_path`.
        home: Test seam for the default-path branch.
        now: Override for the current timestamp.
        repo_state_loader: Test seam for the repo state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate; threaded through to :func:`render_portfolio`.
        width: Console width.

    Returns:
        Rendered text frame. When the registry is missing or fails
        validation the helper still returns a frame; the body
        carries the "registry unavailable" placeholder.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    try:
        registry: Registry | None = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"offline_render registry unavailable: {exc!r}")
        registry = None
        mtime = None
    layout = build_portfolio_frame(
        registry,
        view=None,
        now=now,
        registry_mtime_at=mtime,
        repo_state_loader=repo_state_loader,
        is_stale_evaluator=is_stale_evaluator,
    )
    console.print(layout)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Keypress / view-state transitions (pure)
# ---------------------------------------------------------------------------


#: Keys that move the row cursor up (towards alphabetically-lower codes).
_UP_KEYS: frozenset[str] = frozenset({"\x1b[A", "k"})
#: Keys that move the row cursor down.
_DOWN_KEYS: frozenset[str] = frozenset({"\x1b[B", "j"})
#: Keys that jump to the first row.
_HOME_KEYS: frozenset[str] = frozenset({"\x1b[H", "g"})
#: Keys that jump to the last row.
_END_KEYS: frozenset[str] = frozenset({"\x1b[F", "G"})


def apply_portfolio_key(
    view: PortfolioViewState,
    key: str,
    *,
    rows: list[PortfolioRow],
) -> PortfolioViewState:
    """Apply *key* to *view* against the row cursor model.

    Pure function — does not touch any live :class:`rich.live.Live`
    instance. Unknown keys return the view unchanged so the live-loop
    caller can treat the helper as a fallthrough.

    Transitions:

    - Up arrow / ``k``: decrement ``selected_index`` (clamped at 0).
    - Down arrow / ``j``: increment ``selected_index`` (clamped at
      ``len(rows) - 1``).
    - Home / ``g``: jump to row 0.
    - End / ``G``: jump to the last row.

    Args:
        view: Current view state.
        key: Single keystroke (or ESC-prefixed CSI sequence).
        rows: Pre-built portfolio rows — needed so the cursor stays
            in bounds when the table shrinks/grows between renders.

    Returns:
        Updated :class:`PortfolioViewState`.
    """
    count = len(rows)
    if not count:
        return view
    upper = count - 1
    if key in _UP_KEYS:
        return view.model_copy(update={"selected_index": max(0, view.selected_index - 1)})
    if key in _DOWN_KEYS:
        return view.model_copy(update={"selected_index": min(upper, view.selected_index + 1)})
    if key in _HOME_KEYS:
        return view.model_copy(update={"selected_index": 0})
    if key in _END_KEYS:
        return view.model_copy(update={"selected_index": upper})
    return view


__all__ = [
    "ACTIVE_MARKER_STYLE",
    "PORTFOLIO_FOOTER_KEYMAP",
    "SELECTED_ROW_STYLE",
    "STALE_MARKER_STYLE",
    "PortfolioRow",
    "PortfolioViewState",
    "apply_portfolio_key",
    "build_portfolio_footer",
    "build_portfolio_frame",
    "build_portfolio_header",
    "build_portfolio_panel",
    "build_portfolio_table",
    "build_row",
    "build_rows",
    "offline_render",
    "render_portfolio",
]
