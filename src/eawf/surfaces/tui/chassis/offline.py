"""Deterministic, non-interactive workspace dashboard for ``workspace registry-status``.

:func:`offline_render` renders the workspace registry dashboard to a plain-text
frame for ``workspace registry-status`` (and its JSON envelope's ``rendered``
field). It is strictly read-only over the registry and never opens a Textual
screen, so it runs piped, in CI and with no daemon.

The frame heads with the brand mark the console header paints: the leading
``◉`` brand glyph then the two-tone green ``Eä`` wordmark, emitted via the
shared :func:`eawf.surfaces.render.brand.render_wordmark_ansi` ANSI channel (the
``E`` plain, the ``ä`` carrying the green accent). ``width`` is honoured via
:func:`textwrap.fill` so narrow callers wrap the body lines.
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eawf.platform.registry.staleness import read_repo_state
from eawf.surfaces.render.brand import render_wordmark_ansi
from eawf.surfaces.tui.chassis.sigils import chrome

if TYPE_CHECKING:
    from eawf.platform.registry import Registry

logger = logging.getLogger(__name__)

#: Two-space gap between the brand literal and the first breadcrumb
#: segment, matching :func:`eawf.surfaces.render.brand.render_breadcrumb_head`.
_BRAND_GAP: str = "  "

#: Single space between the leading brand glyph and the ``Eä`` wordmark,
#: matching the interactive header (``◉ Eä``).
_GLYPH_GAP: str = " "

#: The leading brand glyph the offline frame gains so the headless splash
#: matches the live header's brand mark (UX-19). The offline frame is plain
#: text on a non-graphics path, so it always renders the unicode ``◉`` glyph
#: (the terminal stand-in for the Seal image the live header mounts when
#: graphics-capable), never the image -- there is no graphics protocol in a
#: piped / CI / daemon-down splash.
_BRAND_GLYPH: str = chrome("brand", mode="unicode")

#: Placeholder rendered in the workspace strip when the registry file is
#: missing or fails to load. The substring ``registry unavailable`` is
#: part of the ``workspace registry-status`` text contract.
_REGISTRY_UNAVAILABLE: str = "registry unavailable (read failed)"

#: The breadcrumb head a registry with no repo to name falls back to.
DEFAULT_PROJECT_CODE: str = "EAWF"

#: The sigma that opens the portfolio totals line.
TOTALS_ROW_LABEL: str = "Σ"


def _brand_head(breadcrumb: str) -> str:
    """Render the offline daemon-down brand head ``◉ Eä  <breadcrumb>``.

    The headless brand frame paints the SAME brand mark the console header
    renders: the leading
    ``◉`` brand glyph (the terminal stand-in for the Seal image, UX-19) then the
    two-tone green ``Eä`` wordmark -- the ``E`` plain, the ``ä`` carrying the
    reskin-green accent -- so the daemon-down splash and the live app are visually
    identical rather than the old glyph-less / colourless / teal head. The wordmark
    is emitted via the shared
    :func:`eawf.surfaces.render.brand.render_wordmark_ansi` ANSI channel (the
    header threads the same accent through its markup channel), so both surfaces
    draw from one accent token and cannot drift apart. The visible glyphs stay the
    ``◉ Eä`` brand mark; only the accent layer wraps the umlaut.

    Args:
        breadcrumb: The pre-rendered scope breadcrumb that trails the brand,
            joined to the wordmark by the canonical two-space gap.

    Returns:
        ``"◉ E<accent>ä<reset>  <breadcrumb>"`` -- the brand glyph, the two-tone
        green wordmark, the canonical gap, then the breadcrumb.
    """
    return f"{_BRAND_GLYPH}{_GLYPH_GAP}{render_wordmark_ansi()}{_BRAND_GAP}{breadcrumb}"


def _workspace_breadcrumb(registry: Registry | None) -> str:
    """Build the workspace dashboard breadcrumb head from the registry.

    Uses the active repo's code when the registry resolves one, else the
    :data:`DEFAULT_PROJECT_CODE` placeholder so the header stays
    informative for an empty or unavailable registry.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.

    Returns:
        The breadcrumb string (without the brand prefix).
    """
    if registry is None:
        return DEFAULT_PROJECT_CODE
    if registry.active_code is not None:
        return registry.active_code
    if registry.repos:
        return sorted(registry.repos)[0]
    return DEFAULT_PROJECT_CODE


def _active_phase_completion(repo_state: dict[str, Any] | None) -> tuple[int, int]:
    """Return ``(closed, total)`` wave counts for a repo's active phase.

    The ``current.phase_id`` pointer wins when it names a phase whose status
    is ``"active"``; otherwise the single active phase is used. A ``None``,
    malformed or phase-less state yields ``(0, 0)`` so the totals never carry a
    fabricated ratio.

    Args:
        repo_state: A decoded per-repo ``state.json`` dict, or ``None``.

    Returns:
        The ``(closed_waves, total_waves)`` pair for the active phase.
    """
    if not repo_state or not isinstance(phases := repo_state.get("phases"), dict):
        return (0, 0)
    current = repo_state.get("current")
    pointer = current.get("phase_id") if isinstance(current, dict) else None
    active = [
        pid
        for pid, phase in phases.items()
        if isinstance(phase, dict) and phase.get("status") == "active"
    ]
    phase_id = pointer if pointer in active else next(iter(active), None)
    iters, waves = repo_state.get("iters"), repo_state.get("waves")
    if phase_id is None or not isinstance(iters, dict) or not isinstance(waves, dict):
        return (0, 0)
    iter_ids = {
        iid for iid, it in iters.items() if isinstance(it, dict) and it.get("phase_id") == phase_id
    }
    phase_waves = [
        w for w in waves.values() if isinstance(w, dict) and w.get("iter_id") in iter_ids
    ]
    return (sum(1 for w in phase_waves if w.get("status") == "closed"), len(phase_waves))


def _sum_field(summaries: object, field: str) -> float:
    """Sum a numeric *field* across a mapping of summary dicts, zero when absent."""
    if not isinstance(summaries, dict):
        return 0.0
    return sum(
        float(row[field])
        for row in summaries.values()
        if isinstance(row, dict) and isinstance(row.get(field), int | float)
    )


def _totals_line(registry: Registry | None) -> str:
    """Build the portfolio-totals line for the workspace strip.

    Folds every registered repo's off-disk state into one line: the repo count,
    the active-phase ``closed/total`` waves summed across repos, and the summed
    EU ``consumed/estimated``. An unavailable or empty registry folds to
    ``Σ 0 repos ...`` rather than omitting the line. EU and PR degrade to a dash
    when nothing was reported; no offline source counts open pull requests.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.

    Returns:
        The one-line totals summary.
    """
    done = total = 0
    consumed = estimated = 0.0
    repos = registry.repos if registry is not None else {}
    for entry in repos.values():
        repo_state = read_repo_state(Path(entry.path))
        closed, count = _active_phase_completion(repo_state)
        done, total = done + closed, total + count
        if repo_state:
            consumed += _sum_field(repo_state.get("actuals"), "elapsed_eu")
            estimated += _sum_field(repo_state.get("estimates"), "expected_eu")
    eu = f"{consumed:g}/{estimated:g}" if estimated > 0 else "—"
    return f"{TOTALS_ROW_LABEL} {len(repos)} repos  waves {done}/{total}  EU {eu}  PR —"


def _strip_line(registry: Registry | None, *, is_stale_at: dict[str, bool]) -> str:
    """Build the one-line repo strip ``CODE [chips]  CODE [chips]`` row.

    Each repo renders as its code, an ``(active)`` chip on the active
    entry, and a ``(stale)`` chip when the staleness predicate fired.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.
        is_stale_at: Per-code staleness flags keyed by repo code.

    Returns:
        The strip row text, or the unavailable placeholder.
    """
    if registry is None or not registry.repos:
        return _REGISTRY_UNAVAILABLE
    cells: list[str] = []
    for code in sorted(registry.repos):
        chips: list[str] = []
        if code == registry.active_code:
            chips.append("(active)")
        if is_stale_at.get(code):
            chips.append("(stale)")
        suffix = f" {' '.join(chips)}" if chips else ""
        cells.append(f"{code}{suffix}")
    return "  ".join(cells)


def offline_render(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    width: int = 100,
) -> str:
    """Render one workspace-dashboard frame to a plain-text string.

    Strictly read-only over ``~/.eawf/registry.json``: resolves the
    registry, computes per-repo staleness, and emits a labelled-section
    text frame (``workspace`` strip · ``roadmap`` / ``status`` /
    ``git`` / ``backlog`` sections). When the registry is missing or
    fails validation the frame still renders, with the strip carrying the
    ``registry unavailable`` placeholder instead of repo cells.

    Used by ``workspace registry-status`` (and its JSON envelope's
    ``rendered`` field). Replaces the deleted legacy workspace
    ``offline_render`` Rich-Layout renderer.

    Args:
        registry_path: Explicit registry path; ``None`` falls back to
            :func:`eawf.platform.registry.default_registry_path`.
        home: Test seam for the default-path branch.
        now: Override for the current timestamp threaded to the staleness
            predicate so freshness comparisons stay deterministic.
        width: Column width for line wrapping; narrow widths wrap the
            strip + section lines so the output differs from a wide
            render.

    Returns:
        The rendered text frame (terminated by a trailing newline).
    """
    from eawf.platform.registry import (
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )

    registry: Registry | None
    try:
        registry = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"offline_render registry unavailable cause={exc!r}")
        registry = None
        mtime = None

    is_stale_at: dict[str, bool] = {}
    if registry is not None:
        for code, entry in registry.repos.items():
            is_stale_at[code] = is_stale(entry, registry_mtime_at=mtime, now=now)

    breadcrumb = _workspace_breadcrumb(registry)
    code = breadcrumb if registry is not None else DEFAULT_PROJECT_CODE
    repo_count = len(registry.repos) if registry is not None else 0
    active = registry.active_code if registry is not None else None

    # Body lines wrap to ``width``; the two-tone brand head is rendered
    # separately and prepended verbatim. Its embedded ANSI accent escape
    # carries no display width, so feeding it through ``textwrap.fill`` would
    # mis-count the invisible bytes against the column budget -- the brand
    # splash head is a fixed one-line label, not wrap-eligible prose.
    lines: list[str] = [
        "workspace",
        _strip_line(registry, is_stale_at=is_stale_at),
        _totals_line(registry),
        "",
        "roadmap",
        f"  repos: {repo_count}",
        f"  active: {active if active is not None else '-'}",
        "",
        "status",
        f"  project: {code}",
        f"  registry: {'available' if registry is not None else 'unavailable'}",
        "",
        "git",
        "  branch: -",
        "",
        "backlog",
        f"  repos tracked: {repo_count}",
    ]

    wrapped: list[str] = [_brand_head(breadcrumb), ""]
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.append(
            textwrap.fill(
                line,
                width=max(1, width),
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(wrapped) + "\n"


__all__ = [
    "offline_render",
]
