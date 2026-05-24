"""``GitPane`` — live git branch / status / ahead-behind widget.

A :class:`~textual.widgets.Static` composite that surfaces the working
tree's git context — current branch, dirty/clean status, the last few
commit subjects, and the ahead/behind counts versus the upstream —
refreshed from short ``git`` shell-outs with a ~1 s cache so a
high-frequency repaint never re-pays the subprocess cost.

Unlike the other widgets this pane is **not** driven by the reactive
:class:`~eawf.state.models.State`: there is no ``state['git']`` producer
(the earlier surface always rendered dashes off that unwritten slot), so
the pane reads the live repo directly. Every shell-out is total — a missing
``git`` binary, a non-git cwd, a timeout, or no configured upstream each
render a ``—`` for the affected field rather than raising, so the render
loop stays alive on any path.

The git probe + parsing live in pure module functions
(:func:`gather_git_fields`, :func:`format_git_lines`) so the rendered
text is unit-testable by feeding a :class:`GitFields` value directly,
without spawning ``git``. Colours resolve against the ``theme.tcss``
palette vars — never hardcoded hex.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.reactive import reactive
from textual.widgets import Static

from eawf.tui.widgets.markup import escape_markup, style_labeled_line

logger = logging.getLogger(__name__)

#: Placeholder rendered for any field the git probe could not resolve.
DASH: str = "—"

#: Per-call timeout (seconds) for each ``git`` subprocess — short enough
#: that a stuck command can never freeze the render loop.
GIT_SUBPROCESS_TIMEOUT_S: float = 1.0

#: How many recent commit subjects the pane lists.
RECENT_COMMIT_COUNT: int = 3

#: Cache TTL (seconds) for the git probe per the brief's 1 s citation.
GIT_CACHE_TTL_S: float = 1.0


@dataclass(frozen=True)
class GitFields:
    """Parsed git context for the pane.

    Attributes:
        branch: Current branch name, or :data:`DASH` when undetermined.
        dirty: Working-tree dirty summary (``"clean"`` /
            ``"N changed"`` / :data:`DASH`).
        ahead_behind: Upstream divergence (``"up-to-date"`` /
            ``"+A / -B"`` / :data:`DASH` when no upstream).
        recent_commits: Up to :data:`RECENT_COMMIT_COUNT` recent commit
            subject lines (already shortened by ``git``).
    """

    branch: str
    dirty: str
    ahead_behind: str
    recent_commits: tuple[str, ...]


def _git_run(args: list[str], *, cwd: Path) -> str | None:
    """Run a ``git`` subprocess; return stripped stdout or ``None``.

    Never raises out of the pane: a missing binary, timeout, or non-zero
    exit all return ``None`` so the caller renders a dash.

    Args:
        args: git argument vector (without the leading ``git``).
        cwd: Directory to run in.

    Returns:
        Stripped stdout on success, else ``None``.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(f"_git_run args={args!r} cwd={cwd!s} failed cause={exc!r}")
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def gather_git_fields(cwd: Path) -> GitFields:
    """Probe *cwd* via short ``git`` shell-outs into a :class:`GitFields`.

    Args:
        cwd: The repo working directory to inspect.

    Returns:
        The parsed git context; unresolved fields fall back to
        :data:`DASH` (or ``clean`` / ``up-to-date`` for the explicit
        no-change cases).
    """
    branch = _git_run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd) or DASH
    porcelain = _git_run(["status", "--porcelain"], cwd=cwd)
    if porcelain is None:
        dirty = DASH
    elif not porcelain:
        dirty = "clean"
    else:
        changed = sum(1 for line in porcelain.splitlines() if line)
        dirty = f"{changed} changed"
    ahead = _git_run(["rev-list", "--count", "@{u}..HEAD"], cwd=cwd)
    behind = _git_run(["rev-list", "--count", "HEAD..@{u}"], cwd=cwd)
    if ahead is None or behind is None:
        ahead_behind = DASH
    elif ahead == "0" and behind == "0":
        ahead_behind = "up-to-date"
    else:
        ahead_behind = f"+{ahead} / -{behind}"
    log_out = _git_run(
        ["log", f"-{RECENT_COMMIT_COUNT}", "--format=%h %s"],
        cwd=cwd,
    )
    recent = tuple(log_out.splitlines()) if log_out else ()
    return GitFields(
        branch=branch,
        dirty=dirty,
        ahead_behind=ahead_behind,
        recent_commits=recent,
    )


def format_git_lines(fields: GitFields) -> list[str]:
    """Render :class:`GitFields` into the pane's text lines.

    Pure helper so the rendered text is unit-testable without spawning
    ``git``.

    Args:
        fields: The parsed git context.

    Returns:
        The ordered list of plain-text lines.
    """
    lines = [
        f"branch:   {fields.branch}",
        f"status:   {fields.dirty}",
        f"upstream: {fields.ahead_behind}",
    ]
    if fields.recent_commits:
        lines.append("recent:")
        lines.extend(f"  {subject}" for subject in fields.recent_commits)
    return lines


class GitPane(Static):
    """Live git context pane with a short subprocess cache.

    Set the working directory via :paramref:`cwd` (defaults to the process
    cwd). The pane refreshes on mount and exposes :meth:`refresh_git` so a
    host screen can re-probe on a force-refresh keypress; results are
    cached for :data:`GIT_CACHE_TTL_S` so back-to-back refreshes coalesce.
    """

    DEFAULT_CSS: ClassVar[str] = """
    GitPane {
        height: auto;
        width: 1fr;
    }
    """

    #: Monotonic timestamp of the last probe, for the TTL cache.
    _last_probe: reactive[float] = reactive(0.0, init=False)

    def __init__(self, *, cwd: Path | None = None, **kwargs: object) -> None:
        """Construct the pane.

        Args:
            cwd: Repo working directory to inspect; defaults to the
                process cwd when ``None``.
            **kwargs: Forwarded to :class:`textual.widgets.Static`.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._cwd = cwd if cwd is not None else Path.cwd()
        self._fields: GitFields | None = None

    def on_mount(self) -> None:
        """Kick off the first git probe off the event loop."""
        self.refresh_git(force=True)

    def refresh_git(self, *, force: bool = False) -> None:
        """Re-probe git off the event loop (subject to the TTL cache) and repaint.

        The probe is four ``git`` subprocesses; running it in a worker keeps a
        slow or hung ``git`` from blocking the first paint or the render loop.
        The cached fields repaint when the worker returns. ``exclusive`` drops
        any in-flight probe so back-to-back force-refreshes coalesce.

        Args:
            force: When ``True``, bypass the :data:`GIT_CACHE_TTL_S`
                cache and probe immediately (used by force-refresh).
        """
        now = time.monotonic()
        if not force and self._fields is not None and now - self._last_probe < GIT_CACHE_TTL_S:
            return
        self._last_probe = now
        self.run_worker(self._probe_git(), group="git-probe", exclusive=True)

    async def _probe_git(self) -> None:
        """Worker body: probe git off-thread, then apply + repaint on the loop."""
        fields = await asyncio.to_thread(gather_git_fields, self._cwd)
        self._fields = fields
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the pane from the cached fields.

        Each ``label: value`` line carries the accent label tint
        (:func:`~eawf.tui.widgets.markup.style_labeled_line`), matching the
        status pane and the detail modal; the indented recent-commit
        subjects (which may contain ``[P##-W##]`` brackets) are
        markup-escaped so Textual renders them literally rather than
        parsing the bracket run as a style tag.
        """
        if self._fields is None:
            self.update(escape_markup(DASH))
            return
        lines = format_git_lines(self._fields)
        self.update("\n".join(style_labeled_line(line) for line in lines))


__all__ = [
    "DASH",
    "GIT_CACHE_TTL_S",
    "GIT_SUBPROCESS_TIMEOUT_S",
    "RECENT_COMMIT_COUNT",
    "GitFields",
    "GitPane",
    "format_git_lines",
    "gather_git_fields",
]
