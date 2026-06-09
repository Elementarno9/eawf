"""``GitPane`` — live git branch / status / ahead-behind widget.

A :class:`~textual.widgets.Static` composite that surfaces the working
tree's git context — current branch, dirty/clean status, the last few
commit subjects, and the ahead/behind counts versus the upstream —
refreshed from short ``git`` shell-outs with a ~1 s cache so a
high-frequency repaint never re-pays the subprocess cost.

Unlike the other widgets this pane is **not** driven by the reactive
:class:`~eawf.kernel.state.models.State`: there is no ``state['git']`` producer
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

from eawf.surfaces.tui.widgets.markup import escape_markup, style_labeled_line

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


#: Content-markup palette vars the working-tree status segments tint
#: through. ``added`` reads through the green-rotated ``$ok`` (the same
#: family the reskin's accent rotation lands on), ``removed`` through the
#: ``$err`` red, and ``changed`` through the ``$warn`` amber -- a salience
#: ladder that lets the operator scan the dirty summary by hue. They are
#: ``$`` palette vars (not concrete hex) because the pane renders through
#: Textual content markup, which resolves the vars against the live theme,
#: so a ``/theme`` swap recolours the segments for free.
ADDED_VAR: str = "$ok"
REMOVED_VAR: str = "$err"
CHANGED_VAR: str = "$warn"


@dataclass(frozen=True)
class GitFields:
    """Parsed git context for the pane.

    Attributes:
        branch: Current branch name, or :data:`DASH` when undetermined.
        dirty: Working-tree dirty summary (``"clean"`` /
            ``"N changed"`` / :data:`DASH`).
        added: Count of added (staged-new / untracked) working-tree paths.
        removed: Count of removed (deleted) working-tree paths.
        changed: Count of modified / renamed / copied working-tree paths.
        ahead_behind: Upstream divergence (``"up-to-date"`` /
            ``"+A / -B"`` / :data:`DASH` when no upstream).
        recent_commits: Up to :data:`RECENT_COMMIT_COUNT` recent commit
            subject lines (already shortened by ``git``).
    """

    branch: str
    dirty: str
    ahead_behind: str
    recent_commits: tuple[str, ...]
    added: int = 0
    removed: int = 0
    changed: int = 0


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


def classify_porcelain(porcelain: str) -> tuple[int, int, int]:
    """Tally ``git status --porcelain`` lines into (added, removed, changed).

    Each porcelain line opens with a two-char ``XY`` code (index status +
    worktree status) followed by the path. A path is counted once, by the
    most salient code across its two status columns:

    * **added** -- a staged-new (``A``) path or an untracked (``??``) path.
    * **removed** -- a deleted (``D``) path in either column.
    * **changed** -- a modified / renamed / copied / type-changed path
      (``M`` / ``R`` / ``C`` / ``T``) when it is neither added nor removed.

    Deletion and addition dominate a plain modification when both codes are
    present (a rename surfaces ``R`` -> changed; a delete-then-readd is rare
    and counted as removed) so the ladder stays unambiguous.

    Args:
        porcelain: The raw ``git status --porcelain`` stdout (may be empty).

    Returns:
        The ``(added, removed, changed)`` path counts.
    """
    added = removed = changed = 0
    for line in porcelain.splitlines():
        if not line:
            continue
        code = line[:2]
        if "D" in code:
            removed += 1
        elif code == "??" or "A" in code:
            added += 1
        elif any(ch in code for ch in "MRCT"):
            changed += 1
    return added, removed, changed


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
    added = removed = changed = 0
    if porcelain is None:
        dirty = DASH
    elif not porcelain:
        dirty = "clean"
    else:
        added, removed, changed = classify_porcelain(porcelain)
        total = sum(1 for line in porcelain.splitlines() if line)
        dirty = f"{total} changed"
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
        added=added,
        removed=removed,
        changed=changed,
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


def format_status_markup(fields: GitFields, *, label_style: str = "$accent") -> str:
    """Build the ``status:`` line as tinted Textual content markup.

    The label token keeps the accent tint (matching the other
    :func:`~eawf.surfaces.tui.widgets.markup.style_labeled_line` rows), and
    the working-tree breakdown is rendered as three hue-tinted segments
    through the rotated palette vars: ``+A`` added in :data:`ADDED_VAR`,
    ``-R`` removed in :data:`REMOVED_VAR`, and ``C changed`` in
    :data:`CHANGED_VAR`. When the tree is ``clean`` / :data:`DASH` (no
    counts) the value is the plain summary string, untinted, so the no-change
    cases stay quiet.

    Pure helper (no widget mount) so the rendered markup is unit-testable;
    the ``$`` vars resolve against the live theme when the pane renders, so
    a ``/theme`` swap recolours the segments without re-parsing.

    Args:
        fields: The parsed git context.
        label_style: The content-markup style for the leading ``status:``
            label token; defaults to the theme accent var.

    Returns:
        The content-markup string for the pane's ``status:`` line.
    """
    label = f"[{label_style}]status:[/]"
    if fields.added == 0 and fields.removed == 0 and fields.changed == 0:
        return f"{label}   {fields.dirty}"
    segments = [
        f"[{ADDED_VAR}]+{fields.added}[/]",
        f"[{REMOVED_VAR}]-{fields.removed}[/]",
        f"[{CHANGED_VAR}]{fields.changed} changed[/]",
    ]
    return f"{label}   {' / '.join(segments)}"


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
        (:func:`~eawf.surfaces.tui.widgets.markup.style_labeled_line`), matching the
        status pane and the detail modal; the indented recent-commit
        subjects (which may contain ``[P##-W##]`` brackets) are
        markup-escaped so Textual renders them literally rather than
        parsing the bracket run as a style tag.

        The ``status:`` line is the exception: its working-tree breakdown
        renders through :func:`format_status_markup` so the added / removed
        / changed counts carry the rotated palette-var tints, not the plain
        accent-label-only styling the other rows use.
        """
        if self._fields is None:
            self.update(escape_markup(DASH))
            return
        rendered: list[str] = []
        for line in format_git_lines(self._fields):
            if line.startswith("status:"):
                rendered.append(format_status_markup(self._fields))
            else:
                rendered.append(style_labeled_line(line))
        self.update("\n".join(rendered))


__all__ = [
    "ADDED_VAR",
    "CHANGED_VAR",
    "DASH",
    "GIT_CACHE_TTL_S",
    "GIT_SUBPROCESS_TIMEOUT_S",
    "RECENT_COMMIT_COUNT",
    "REMOVED_VAR",
    "GitFields",
    "GitPane",
    "classify_porcelain",
    "format_git_lines",
    "format_status_markup",
    "gather_git_fields",
]
