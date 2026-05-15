"""Layout helpers for the Eä Rich TUI (P20-I01-W02; refreshed P20-I03-W01).

This module owns the structural primitives used by :mod:`eawf.tui.app`
and any future TUI surface. It deliberately stays state-shape-agnostic
so future panes can reuse the chassis without coupling to a particular
``state.json`` schema:

* :func:`build_brand_text` — bold-accent ``Eä`` outside-left of the
  scope breadcrumb per ``feedback_tui_branding``.
* :func:`build_breadcrumb` — ``project / phase / iter`` from a state
  dict.
* :func:`build_header_panel`, :func:`build_footer_panel` —
  Panel-wrapped strips for the top and bottom of the frame.
* :func:`build_quadrant` — composes four named Layouts into a 2x2
  grid via :meth:`rich.layout.Layout.split_row` /
  :meth:`~rich.layout.Layout.split_column`.
* :func:`build_frame` — assembles header + body + footer rows, with
  the body slot pre-populated by a quadrant.

The repo-scope quadrant exposed to the app layer is roadmap (top
left), status (top right), git (bottom left), backlog (bottom right);
:func:`repo_quadrant_panes` is the canonical wiring callers use.

Keymap conventions follow ``feedback_tui_keymap_conventions``: arrow
keys + PageUp/PageDown/Home/End/Enter/Esc are the primary surface,
vim aliases (``h/j/k/l/g/G``) appear in parentheses as secondary
shorthand. The quadrant footer carries quadrant-level keys (board /
config / overlay verb-prefix / quit); the wave-board and overlay
footers carry their own keymap.

P20-I03-W01: the git pane reads live from ``git`` CLI rather than the
unwritten ``state['git']`` snapshot (no producer existed for it). The
shell-out result is cached for ~500ms so a 30Hz repaint loop only
incurs one ``git status`` per cache window. The status pane now
surfaces the most recent non-planned iter when no iter is active so
phase activity stays visible after iter closeout.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Brand / breadcrumb / keymap constants
# ---------------------------------------------------------------------------

#: Literal ``Eä`` (U+0045 + U+00E4) brand string. Outside-left of the
#: scope breadcrumb in the header strip per the branding memory note.
BRAND: str = "Eä"

#: Bold-accent style applied to the brand text. The remainder of the
#: header uses ``cyan`` so the brand stays visually outside-left.
BRAND_STYLE: str = "bold white"

#: Style applied to the breadcrumb (project / phase / iter chain).
BREADCRUMB_STYLE: str = "cyan"

#: Default project code used when ``state.json`` is missing or unreadable.
DEFAULT_PROJECT_CODE: str = "EAWF"

#: Quadrant footer keymap. The previous string was wave-board-shaped
#: (arrows + PageUp/PageDown/Home/End/Enter), which leaked board keys
#: into the quadrant. The quadrant only has quadrant-level keys: open
#: wave board (``b``), open config modal (``c``), open overlays via
#: the ``o<letter>`` verb-prefix, and quit (``Esc``/``q``). The wave
#: board keeps its own keymap string in :mod:`eawf.tui.wave_board`.
FOOTER_KEYMAP: str = "b board  c config  oH/oD/oM/oE/oR overlay  Esc/q quit"

#: Footer keymap shown while the overlay verb-prefix is pending —
#: i.e. the operator pressed ``o`` and is now picking the overlay
#: object (Hypothesis / Decision / Memory / Events / Render). Esc
#: cancels the pending state.
FOOTER_KEYMAP_OVERLAY_PENDING: str = (
    "H hypothesis  D decision  M memory  E events  R dispatch  Esc cancel"
)

#: Pane order in the 2x2 quadrant — top-left, top-right, bottom-left,
#: bottom-right. Callers building panes for the quadrant MUST emit four
#: panels in this order; :func:`build_quadrant` rejects any other count.
QUADRANT_PANE_NAMES: tuple[str, str, str, str] = (
    "roadmap",
    "status",
    "git",
    "backlog",
)


# ---------------------------------------------------------------------------
# Brand + breadcrumb helpers
# ---------------------------------------------------------------------------


def build_brand_text(breadcrumb: str) -> Text:
    """Return the bold-accent ``Eä`` brand followed by the breadcrumb.

    The brand sits outside-left of the breadcrumb with two spaces of
    padding so the visual hierarchy stays consistent across terminal
    widths.

    Args:
        breadcrumb: ``project / phase / iter`` string built by
            :func:`build_breadcrumb`.

    Returns:
        Rich :class:`~rich.text.Text` with two styled segments.
    """
    text = Text()
    text.append(f"{BRAND}  ", style=BRAND_STYLE)
    text.append(breadcrumb, style=BREADCRUMB_STYLE)
    return text


def build_breadcrumb(state: dict[str, Any]) -> str:
    """Build the ``project / phase / iter`` breadcrumb from state.

    Falls back to :data:`DEFAULT_PROJECT_CODE` when the state dict is
    empty or missing the ``project.code`` field so the header stays
    informative for a fresh workspace.
    """
    project = (state.get("project") or {}).get("code") or DEFAULT_PROJECT_CODE
    current = state.get("current") or {}
    phase = current.get("phase_id")
    iter_id = current.get("iter_id")
    parts: list[str] = [project]
    if phase:
        parts.append(phase)
    if iter_id:
        parts.append(iter_id)
    return " / ".join(parts)


def build_header_panel(state: dict[str, Any]) -> Panel:
    """Build the top-strip header Panel with brand + breadcrumb."""
    breadcrumb = build_breadcrumb(state)
    return Panel(build_brand_text(breadcrumb), title=None, border_style="dim")


def build_weekly_burn_line(state: dict[str, Any]) -> str | None:
    """Return the ``weekly burn: <consumed> / <target>`` line, or None.

    The line is emitted only when ``state.project.weekly_eu_target`` is a
    non-None numeric value (per P20-I01-W09 success criterion 3 — render
    nothing at all when the field is unset). Loading the typed
    :class:`~eawf.state.models.State` is deferred until the field is
    present so the offline fast-path stays cheap when the operator has
    not opted in.

    Args:
        state: Loaded ``state.json`` dict (may be empty or missing
            ``project``).

    Returns:
        Formatted weekly-burn string when the target is set, else
        ``None`` so the footer composer can omit the line entirely.
    """
    project = state.get("project") or {}
    target = project.get("weekly_eu_target")
    if target is None:
        return None
    # Defer import so the offline fast-path doesn't pay the metrics-module
    # cost when the operator has not opted into the weekly cadence.
    from eawf.estimation.metrics import compute_weekly_burn
    from eawf.state.models import State

    try:
        typed_state = State.model_validate(state)
    except Exception:
        # Render nothing rather than crash the frame when the loaded dict
        # fails schema validation (e.g. partial state, stale fixture).
        logger.debug("build_weekly_burn_line schema validation failed; suppressing line")
        return None
    metric = compute_weekly_burn(typed_state)
    if metric.target_eu is None:  # defensive — target gated above
        return None
    return f"weekly burn: {metric.consumed_eu:g} / {metric.target_eu:g} EU"


def build_footer_panel(state: dict[str, Any] | None = None, *, keymap: str | None = None) -> Panel:
    """Build the bottom-strip footer Panel with the keymap hint.

    The default keymap is :data:`FOOTER_KEYMAP` (quadrant keys). The
    caller may override via ``keymap=`` to render the overlay-pending
    string :data:`FOOTER_KEYMAP_OVERLAY_PENDING` while the operator is
    mid-verb-prefix.

    When *state* carries a ``project.weekly_eu_target`` value the footer
    appends a ``weekly burn: <consumed> / <target> EU`` line below the
    keymap (per P20-I01-W09 success criterion 2). When the field is
    unset the panel renders only the keymap.
    """
    body = Text(keymap if keymap is not None else FOOTER_KEYMAP)
    if state is not None:
        burn_line = build_weekly_burn_line(state)
        if burn_line is not None:
            body.append(f"\n{burn_line}")
    return Panel(body, title=None, border_style="dim")


# ---------------------------------------------------------------------------
# Pane summary helpers (state-shape agnostic counters)
# ---------------------------------------------------------------------------


def summary_counts(state: dict[str, Any]) -> dict[str, int]:
    """Count phases, iters, waves, audits visible in state.

    All keys are zero when the state dict is empty so panes can render
    a deterministic frame even before any roadmap activity. The
    ``iters_closed`` field (P20-I03-W01) lets the roadmap pane show
    historical iter activity once the live iter closes.
    """
    iters = (state.get("iters") or {}).values()
    return {
        "phases_open": sum(
            1 for p in (state.get("phases") or {}).values() if p.get("status") == "active"
        ),
        "iters_open": sum(1 for it in iters if it.get("status") == "active"),
        "iters_closed": sum(1 for it in iters if it.get("status") == "closed"),
        "waves_pending": sum(
            1 for w in (state.get("waves") or {}).values() if w.get("status") == "pending"
        ),
        "waves_in_progress": sum(
            1 for w in (state.get("waves") or {}).values() if w.get("status") == "in_progress"
        ),
        "audits": len(state.get("audits") or {}),
    }


def backlog_counts(state: dict[str, Any]) -> dict[str, int]:
    """Tally backlog items by status for the backlog pane.

    Treats missing keys as zero. Returns ``open`` / ``closed`` /
    ``total`` so the pane can show ``open/total`` ratios without the
    caller re-iterating the state.
    """
    items = (state.get("backlog") or {}).values()
    open_count = 0
    closed_count = 0
    total = 0
    for item in items:
        total += 1
        if item.get("status") == "open":
            open_count += 1
        elif item.get("status") == "closed":
            closed_count += 1
    return {"open": open_count, "closed": closed_count, "total": total}


# ---------------------------------------------------------------------------
# Iter display helpers (status pane — P20-I03-W01 success criterion 4)
# ---------------------------------------------------------------------------


def _format_latest_iter_line(state: dict[str, Any]) -> str:
    """Return the ``iter: ...`` line for the status pane.

    Decision matrix:

    * Active iter → ``iter:    <id> (<status>)``.
    * No active iter but at least one non-planned iter exists → show
      the most recent one as ``iter:    <id> (closed <YYYY-MM-DD>)``
      where the date is :class:`~eawf.state.models.Iter.closed_at`
      truncated to ``YYYY-MM-DD``. When ``closed_at`` is unset the
      date suffix is omitted.
    * Otherwise → ``iter:    — (no iter started)`` so the operator
      sees a deliberate placeholder rather than a bare dash.
    """
    current = state.get("current") or {}
    iter_id = current.get("iter_id")
    iters = state.get("iters") or {}
    if iter_id:
        record = iters.get(iter_id) if isinstance(iters, dict) else None
        status = record.get("status") if isinstance(record, dict) else None
        if status:
            return f"iter:    {iter_id} ({status})"
        return f"iter:    {iter_id}"
    # No active iter — find the most recent non-planned iter.
    non_planned: list[tuple[str, dict[str, Any]]] = [
        (key, value)
        for key, value in iters.items()
        if isinstance(value, dict) and value.get("status") != "planned"
    ]
    if not non_planned:
        return "iter:    — (no iter started)"

    # Prefer iters with a ``closed_at`` for ordering; fall back to id
    # descending so the surfaced iter is the highest W## under the
    # phase. ``closed_at`` may be missing on abandoned iters.
    def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[str, str]:
        key, value = item
        closed_at = value.get("closed_at") or ""
        return (closed_at, key)

    non_planned.sort(key=_sort_key, reverse=True)
    surfaced_id, surfaced = non_planned[0]
    status = surfaced.get("status") or "closed"
    closed_at = surfaced.get("closed_at")
    if isinstance(closed_at, str) and closed_at:
        date_part = closed_at[:10]
        return f"iter:    {surfaced_id} ({status} {date_part})"
    return f"iter:    {surfaced_id} ({status})"


# ---------------------------------------------------------------------------
# Pane builders (the four quadrant panels)
# ---------------------------------------------------------------------------


def build_roadmap_pane(state: dict[str, Any]) -> Panel:
    """Top-left pane: phases / iters / waves overview.

    P20-I03-W01: surface ``iters (closed)`` alongside ``iters (active)``
    so the operator sees historical iter activity even after the live
    iter closes. Aggregate counts only — per-iter detail belongs in a
    wave-board / phase-tree wave.
    """
    counts = summary_counts(state)
    body = Text(
        "\n".join(
            [
                f"phases (active):  {counts['phases_open']}",
                f"iters  (active):  {counts['iters_open']}",
                f"iters  (closed):  {counts['iters_closed']}",
                f"waves  (pending): {counts['waves_pending']}",
                f"waves  (in-prog): {counts['waves_in_progress']}",
            ]
        )
    )
    return Panel(body, title="roadmap", border_style="cyan")


def build_status_pane(state: dict[str, Any]) -> Panel:
    """Top-right pane: current scope + audit count.

    P20-I03-W01: when no iter is active, surface the most recent
    non-planned iter so the operator sees phase activity rather than
    a bare ``iter: —``.
    """
    current = state.get("current") or {}
    counts = summary_counts(state)
    project = (state.get("project") or {}).get("code") or DEFAULT_PROJECT_CODE
    lines = [
        f"project: {project}",
        f"phase:   {current.get('phase_id') or '-'}",
        _format_latest_iter_line(state),
        f"audits:  {counts['audits']}",
    ]
    return Panel(Text("\n".join(lines)), title="status", border_style="cyan")


# ---------------------------------------------------------------------------
# Git pane — live shell-outs with a small monotonic cache
# ---------------------------------------------------------------------------


#: Cache TTL for the git-pane shell-outs (seconds). At 30Hz that's
#: ~15 frames per refresh — comfortably hides the ``git status`` cost
#: even when the operator presses keys back-to-back.
GIT_PANE_CACHE_TTL: float = 0.5

#: Per-call timeout for each ``git`` subprocess (seconds). Set short
#: enough that a stuck command can't freeze the render loop.
GIT_PANE_SUBPROCESS_TIMEOUT: float = 1.0


_GIT_CACHE_LOCK: threading.Lock = threading.Lock()
_GIT_CACHE: dict[Path, tuple[float, dict[str, str]]] = {}


def _resolve_repo_root(workspace: Path | None = None) -> Path:
    """Return the workspace root for the git-pane shell-outs.

    Prefers the *workspace* argument when supplied (matches the
    ``--workspace`` knob threaded through :func:`eawf.tui.app.run_tui`).
    Otherwise walks upward from :func:`Path.cwd` until a ``.ea``
    directory is found; if none is found, returns ``Path.cwd()``. The
    walk is bounded so a CWD outside any workspace returns deterministic
    results without scanning the entire filesystem.
    """
    if workspace is not None:
        return Path(workspace)
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".ea").is_dir():
            return candidate
    return cwd


def _git_run(args: list[str], *, cwd: Path) -> str | None:
    """Run a git subprocess and return stripped stdout, or ``None`` on failure.

    Failures we treat as "render a dash" rather than raising:

    * ``FileNotFoundError`` — no ``git`` on PATH.
    * ``subprocess.TimeoutExpired`` — git hung past
      :data:`GIT_PANE_SUBPROCESS_TIMEOUT`.
    * Non-zero exit (e.g. not a git repo, no upstream).

    The function never raises out of the pane builder — the render
    loop must stay alive even when the workspace is on a non-git path.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_PANE_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(f"_git_run args={args!r} cwd={cwd!r} failed: {exc!r}")
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _gather_git_fields(cwd: Path) -> dict[str, str]:
    """Shell out to ``git`` and return branch/head/status/upstream fields.

    All values are strings. Missing data renders as ``-`` so the pane
    composer never has to handle ``None``.
    """
    branch = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd) or "-"
    head = _git_run(["rev-parse", "--short", "HEAD"], cwd=cwd) or "-"
    porcelain = _git_run(["status", "--porcelain"], cwd=cwd)
    if porcelain is None:
        status = "-"
    elif not porcelain:
        status = "clean"
    else:
        modified_count = sum(1 for line in porcelain.splitlines() if line)
        status = f"{modified_count} modified"
    ahead = _git_run(["rev-list", "--count", "@{u}..HEAD"], cwd=cwd)
    behind = _git_run(["rev-list", "--count", "HEAD..@{u}"], cwd=cwd)
    if ahead is None or behind is None:
        # No upstream configured (or git missing); render a single dash.
        upstream = "-"
    elif ahead == "0" and behind == "0":
        upstream = "up-to-date"
    else:
        upstream = f"+{ahead} / -{behind}"
    return {"branch": branch, "head": head, "status": status, "upstream": upstream}


def _git_pane_fields(cwd: Path, *, now: float | None = None) -> dict[str, str]:
    """Return cached git fields for *cwd*, refreshing past the TTL.

    The cache key is the resolved workspace path so concurrent TUI
    instances pointing at different workspaces don't collide. We hold
    a small global :class:`threading.Lock` so the rare race between
    the render thread and a future background poll stays consistent.
    """
    timestamp = time.monotonic() if now is None else now
    with _GIT_CACHE_LOCK:
        cached = _GIT_CACHE.get(cwd)
        if cached is not None:
            cached_at, cached_fields = cached
            if timestamp - cached_at < GIT_PANE_CACHE_TTL:
                return cached_fields
    fresh = _gather_git_fields(cwd)
    with _GIT_CACHE_LOCK:
        _GIT_CACHE[cwd] = (timestamp, fresh)
    return fresh


def _reset_git_pane_cache() -> None:
    """Test seam: clear the cache between assertions.

    Production callers never need this — the TTL handles eviction.
    """
    with _GIT_CACHE_LOCK:
        _GIT_CACHE.clear()


def build_git_pane(state: dict[str, Any], *, workspace: Path | None = None) -> Panel:
    """Bottom-left pane: git context from a cached ``git`` CLI poll.

    Reads live from ``git`` rather than the unwritten ``state['git']``
    snapshot (no producer existed for it, so the pane previously
    always showed dashes). Each render either hits the
    :data:`GIT_PANE_CACHE_TTL` cache or runs three short
    :func:`subprocess.run` calls (rev-parse + rev-parse + status).
    On any failure (no git on PATH, non-git cwd, timeout) the relevant
    field renders ``-`` — the pane builder is total.

    Args:
        state: Ignored — kept in the signature so callers that wire the
            pane via :func:`repo_quadrant_panes` don't need to change.
            The ``state['git']`` slot is no longer consulted; if your
            project still emits one it is silently overridden by the
            live read.
        workspace: Optional workspace root override. When ``None``, the
            helper walks upward from :func:`Path.cwd` looking for a
            ``.ea`` directory.

    Returns:
        :class:`Panel` titled ``git`` with branch / head / status /
        upstream rows.
    """
    del state  # the live shell-out supersedes any stale snapshot
    cwd = _resolve_repo_root(workspace)
    fields = _git_pane_fields(cwd)
    lines = [
        f"branch:   {fields['branch']}",
        f"head:     {fields['head']}",
        f"status:   {fields['status']}",
        f"upstream: {fields['upstream']}",
    ]
    return Panel(Text("\n".join(lines)), title="git", border_style="cyan")


def build_backlog_pane(state: dict[str, Any]) -> Panel:
    """Bottom-right pane: backlog open/closed/total counters."""
    counts = backlog_counts(state)
    lines = [
        f"open:   {counts['open']}",
        f"closed: {counts['closed']}",
        f"total:  {counts['total']}",
    ]
    return Panel(Text("\n".join(lines)), title="backlog", border_style="cyan")


def repo_quadrant_panes(
    state: dict[str, Any], *, workspace: Path | None = None
) -> tuple[Panel, Panel, Panel, Panel]:
    """Build the four panes for the repo-scope quadrant in canonical order.

    Order matches :data:`QUADRANT_PANE_NAMES`: roadmap (top-left),
    status (top-right), git (bottom-left), backlog (bottom-right).
    """
    return (
        build_roadmap_pane(state),
        build_status_pane(state),
        build_git_pane(state, workspace=workspace),
        build_backlog_pane(state),
    )


# ---------------------------------------------------------------------------
# Quadrant + frame composition
# ---------------------------------------------------------------------------


def build_quadrant(panes: tuple[Panel, Panel, Panel, Panel]) -> Layout:
    """Compose four Panels into a 2x2 grid via row/column splits.

    Args:
        panes: Four-tuple of panels in canonical
            (top-left, top-right, bottom-left, bottom-right) order.

    Returns:
        A :class:`~rich.layout.Layout` named ``quadrant`` containing
        two named sub-rows ``top`` and ``bottom``, each split into two
        columns whose names come from :data:`QUADRANT_PANE_NAMES`.

    Raises:
        ValueError: panes is not exactly four panels.
    """
    if len(panes) != 4:
        raise ValueError(f"quadrant requires exactly 4 panes, got {len(panes)!r}")
    top_left, top_right, bottom_left, bottom_right = panes
    quadrant = Layout(name="quadrant")
    top = Layout(name="top")
    bottom = Layout(name="bottom")
    top.split_row(
        Layout(top_left, name=QUADRANT_PANE_NAMES[0], ratio=1),
        Layout(top_right, name=QUADRANT_PANE_NAMES[1], ratio=1),
    )
    bottom.split_row(
        Layout(bottom_left, name=QUADRANT_PANE_NAMES[2], ratio=1),
        Layout(bottom_right, name=QUADRANT_PANE_NAMES[3], ratio=1),
    )
    quadrant.split_column(top, bottom)
    return quadrant


def build_frame(
    state: dict[str, Any],
    *,
    workspace: Path | None = None,
    footer_keymap: str | None = None,
) -> Layout:
    """Assemble header + body (quadrant) + footer into one Layout.

    The header row carries the ``Eä`` brand + scope breadcrumb; the
    body row holds the 2x2 quadrant produced by
    :func:`repo_quadrant_panes` and :func:`build_quadrant`; the
    footer row carries :data:`FOOTER_KEYMAP` plus the optional weekly
    burn divisor (P20-I01-W09) when ``state.project.weekly_eu_target``
    is set.

    Args:
        state: Loaded ``state.json`` dict.
        workspace: Optional workspace root forwarded to the git pane.
            When ``None`` the git pane resolves the root via cwd.
        footer_keymap: Optional override for the footer keymap string
            — set to :data:`FOOTER_KEYMAP_OVERLAY_PENDING` while the
            operator is mid-``o<letter>`` verb prefix.
    """
    # Bump the footer row by one when the weekly burn line is present;
    # otherwise the burn line and panel border collide on narrow
    # terminals. Stays at 3 rows when the field is unset so the
    # existing golden snapshot is byte-stable.
    footer_size = 4 if build_weekly_burn_line(state) is not None else 3
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=footer_size),
    )
    layout["header"].update(build_header_panel(state))
    layout["body"].update(build_quadrant(repo_quadrant_panes(state, workspace=workspace)))
    layout["footer"].update(build_footer_panel(state, keymap=footer_keymap))
    return layout


__all__ = [
    "BRAND",
    "BRAND_STYLE",
    "BREADCRUMB_STYLE",
    "DEFAULT_PROJECT_CODE",
    "FOOTER_KEYMAP",
    "FOOTER_KEYMAP_OVERLAY_PENDING",
    "GIT_PANE_CACHE_TTL",
    "GIT_PANE_SUBPROCESS_TIMEOUT",
    "QUADRANT_PANE_NAMES",
    "backlog_counts",
    "build_backlog_pane",
    "build_brand_text",
    "build_breadcrumb",
    "build_footer_panel",
    "build_frame",
    "build_git_pane",
    "build_header_panel",
    "build_quadrant",
    "build_roadmap_pane",
    "build_status_pane",
    "build_weekly_burn_line",
    "repo_quadrant_panes",
    "summary_counts",
]
