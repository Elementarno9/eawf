"""``PrListModal`` — the ``/pr`` open-PRs overlay.

The ``/pr`` palette verb opens a list of the repo's open pull requests:
per-repo PR rows, ``Enter`` opens the highlighted PR via
``gh pr view --web``, a 60 s cache balances freshness against the
``gh pr list`` cost, and the overlay degrades gracefully when ``gh`` is
absent. ``Esc`` closes.

**Read-only ``gh`` shell-out.** The data source is a lazy
``gh pr list --json`` shell-out cached for 60 s. ``gh pr list`` is
read-only, so per the daemon-authority rule (reads bypass the daemon) a
raw ``subprocess`` mirroring :func:`~eawf.surfaces.tui.widgets.git_pane._git_run`
is the correct, consistent pattern: :func:`fetch_open_prs` spawns the
probe with a short timeout, decodes the JSON via :func:`parse_pr_rows`,
and degrades to an ``unavailable`` status (rather than raising) when
``gh`` is missing, unauthenticated, slow, or returns junk. A module-level
TTL cache keyed by cwd reuses the result within :data:`PR_CACHE_TTL_S`.

The row model (:class:`PrRow`) + the parser are pure so decoding +
truncation are unit-testable without a live ``gh``; the modal is a thin
scrollable view over them and the :class:`PrFetch` status it renders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The ``gh pr list`` cache TTL in seconds — 60 s balances freshness
#: against the ~200-500 ms ``gh pr list`` cost per repo.
PR_CACHE_TTL_S: float = 60.0

#: Per-call timeout (seconds) for the ``gh`` subprocess — short enough
#: that a stuck command can never freeze the overlay open.
GH_TIMEOUT_S: float = 5.0

#: Upper bound on the rows ``gh pr list`` returns — the overlay only shows
#: open PRs, so a high cap is plenty without an unbounded fetch.
GH_PR_LIMIT: int = 50

#: The ``gh pr list --json`` field set the parser expects.
GH_PR_FIELDS: tuple[str, ...] = ("number", "title", "author", "state", "url")


class PrFetchStatus(StrEnum):
    """Outcome of a :func:`fetch_open_prs` shell-out.

    Attributes:
        OK: ``gh`` ran and returned a (possibly empty) list — empty rows
            mean genuinely zero open PRs.
        UNAVAILABLE: ``gh`` was missing, unauthenticated, timed out, or
            returned undecodable output — the rows are empty but unknown,
            not known-zero.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PrRow:
    """One pull-request row (a flattened ``gh pr list --json`` record).

    Attributes:
        number: The PR number.
        title: The PR title.
        author: The PR author's login (``""`` when unknown).
        state: The PR state (``OPEN`` / ``MERGED`` / ``CLOSED``).
        url: The PR web URL (passed to ``gh pr view --web``).
    """

    number: int
    title: str
    author: str
    state: str
    url: str


@dataclass(frozen=True)
class PrFetch:
    """The result of a :func:`fetch_open_prs` shell-out.

    Attributes:
        rows: The decoded open-PR rows (empty when ``gh`` returned no PRs
            or when the fetch was unavailable).
        status: Whether ``gh`` ran (:attr:`PrFetchStatus.OK`) or the fetch
            degraded (:attr:`PrFetchStatus.UNAVAILABLE`).
    """

    rows: tuple[PrRow, ...]
    status: PrFetchStatus


#: Module-level TTL cache keyed by the resolved cwd. Maps the working
#: directory to ``(monotonic_ts, PrFetch)``; reused within
#: :data:`PR_CACHE_TTL_S`. Only the ``/pr`` verb path populates it, so
#: unrelated tests never observe a stale or cross-test entry.
_PR_CACHE: dict[Path, tuple[float, PrFetch]] = {}


def reset_pr_cache() -> None:
    """Clear the open-PR TTL cache (force a fresh fetch on next open)."""
    _PR_CACHE.clear()


def _author_login(author: Any) -> str:
    """Extract the login string from a ``gh`` author field.

    ``gh pr list --json author`` returns an object (``{"login": ...}``);
    tolerate a bare string or a missing field too so a schema drift on the
    ``gh`` side degrades to an empty author rather than raising.

    Args:
        author: The raw ``author`` field value.

    Returns:
        The author login, or ``""`` when unresolvable.
    """
    if isinstance(author, dict):
        return str(author.get("login", ""))
    if isinstance(author, str):
        return author
    return ""


def parse_pr_rows(records: list[dict[str, Any]]) -> tuple[PrRow, ...]:
    """Decode ``gh pr list --json`` records into :class:`PrRow` rows.

    Tolerant of partial records: a row missing ``number`` is skipped
    (the number is the load-bearing key for ``gh pr view``), and the other
    fields fall back to empty / their natural defaults so one odd record
    never aborts the decode.

    Args:
        records: The decoded ``gh pr list --json`` array (a list of
            per-PR dicts).

    Returns:
        The decoded rows in input order.
    """
    rows: list[PrRow] = []
    for record in records:
        if not isinstance(record, dict) or "number" not in record:
            continue
        try:
            number = int(record["number"])
        except TypeError, ValueError:
            continue
        rows.append(
            PrRow(
                number=number,
                title=str(record.get("title", "")),
                author=_author_login(record.get("author")),
                state=str(record.get("state", "")),
                url=str(record.get("url", "")),
            )
        )
    return tuple(rows)


def _gh_list_open_prs(cwd: Path) -> PrFetch:
    """Run ``gh pr list --json`` once and decode it into a :class:`PrFetch`.

    Mirrors :func:`~eawf.surfaces.tui.widgets.git_pane._git_run`: a missing ``gh``
    binary, a non-zero exit (not authenticated / not a gh repo), a timeout,
    or undecodable output each return an
    :attr:`PrFetchStatus.UNAVAILABLE` fetch with empty rows rather than
    raising, so the overlay opens on any path.

    Args:
        cwd: The repo working directory to query.

    Returns:
        An :attr:`PrFetchStatus.OK` fetch with the decoded rows on
        success, else an :attr:`PrFetchStatus.UNAVAILABLE` fetch.
    """
    try:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(GH_PR_LIMIT),
                "--json",
                ",".join(GH_PR_FIELDS),
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(f"_gh_list_open_prs cwd={cwd!s} failed cause={exc!r}")
        return PrFetch(rows=(), status=PrFetchStatus.UNAVAILABLE)
    if completed.returncode != 0:
        logger.debug(
            f"_gh_list_open_prs cwd={cwd!s} nonzero rc={completed.returncode} "
            f"stderr={completed.stderr.strip()!r}"
        )
        return PrFetch(rows=(), status=PrFetchStatus.UNAVAILABLE)
    try:
        records = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.debug(f"_gh_list_open_prs cwd={cwd!s} bad json cause={exc!r}")
        return PrFetch(rows=(), status=PrFetchStatus.UNAVAILABLE)
    if not isinstance(records, list):
        logger.debug(f"_gh_list_open_prs cwd={cwd!s} non-list payload type={type(records)!r}")
        return PrFetch(rows=(), status=PrFetchStatus.UNAVAILABLE)
    return PrFetch(rows=parse_pr_rows(records), status=PrFetchStatus.OK)


def fetch_open_prs(cwd: Path | None = None, *, force: bool = False) -> PrFetch:
    """Fetch the repo's open PRs via ``gh`` (lazy, cached for 60 s).

    Reuses the cached :class:`PrFetch` for *cwd* when the last fetch is
    younger than :data:`PR_CACHE_TTL_S`; otherwise shells out via
    :func:`_gh_list_open_prs` and caches the result. The shell-out is
    read-only — it mutates nothing and never raises (a missing or errored
    ``gh`` degrades to an :attr:`PrFetchStatus.UNAVAILABLE` fetch).

    Args:
        cwd: The repo working directory to query; defaults to the process
            cwd when ``None``.
        force: When ``True``, bypass the TTL cache and re-fetch.

    Returns:
        The open-PR fetch (rows + status).
    """
    resolved = (cwd if cwd is not None else Path.cwd()).resolve()
    now = time.monotonic()
    if not force:
        cached = _PR_CACHE.get(resolved)
        if cached is not None and now - cached[0] < PR_CACHE_TTL_S:
            return cached[1]
    fetch = _gh_list_open_prs(resolved)
    _PR_CACHE[resolved] = (now, fetch)
    return fetch


def _gh_view_web(number: int) -> None:
    """Open PR *number* in the browser via ``gh pr view --web`` (fire-and-forget).

    Mirrors the read-only ``gh`` shell-out pattern: a missing binary, a
    timeout, or an error is swallowed (logged at debug) so a bad ``gh`` never
    crashes the overlay. Runs in a worker thread off the event loop.

    Args:
        number: The PR number to open.
    """
    try:
        subprocess.run(
            ["gh", "pr", "view", "--web", str(number)],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug(f"_gh_view_web number={number} failed cause={exc!r}")


def _render_row(row: PrRow) -> str:
    """Render one :class:`PrRow` as a single content-markup line.

    Args:
        row: The PR row to render.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    author = f" @{row.author}" if row.author else ""
    return f"[$accent]#{row.number}[/]  {row.title}{author}"


#: Placeholder shown when ``gh`` ran and returned zero open PRs.
_EMPTY_OK_TEXT: str = "no open pull requests"

#: Placeholder shown when the ``gh`` fetch was unavailable.
_EMPTY_UNAVAILABLE_TEXT: str = "gh unavailable — install + authenticate gh to list PRs"


class PrListModal(ModalScreen[None]):
    """Scrollable open-PR list (Enter opens web, Esc closes).

    Built with a pre-fetched tuple of :class:`PrRow` and the
    :class:`PrFetchStatus` that produced them (the host resolves both from
    :func:`fetch_open_prs`) so the overlay never spawns the list probe
    itself. ``Enter`` opens the highlighted PR via ``gh pr view --web``;
    ``Esc`` closes. When the row set is empty the overlay shows a
    status-aware placeholder: "no open pull requests" when ``gh`` ran and
    found none, or the ``gh``-unavailable hint when the fetch degraded.
    """

    DEFAULT_CSS: ClassVar[str] = """
    PrListModal {
        align: center middle;
    }
    PrListModal > #pr-card {
        width: 80%;
        max-width: 120;
        height: 70%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    PrListModal .pr-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    PrListModal #pr-list {
        height: 1fr;
    }
    PrListModal .pr-row {
        height: auto;
    }
    PrListModal .pr-row.-selected {
        text-style: bold reverse;
    }
    PrListModal .pr-empty {
        color: $text-muted;
        height: auto;
    }
    PrListModal .pr-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``↑`` / ``↓`` move the highlight, ``Enter`` opens the PR in the
    #: browser, ``Esc`` closes. Vim ``j`` / ``k`` ride the arrows.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "open_web", "open", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the highlighted PR row (``-1`` when the list is empty).
    selected: reactive[int] = reactive(0)

    def __init__(
        self,
        rows: tuple[PrRow, ...],
        status: PrFetchStatus = PrFetchStatus.OK,
    ) -> None:
        """Construct the overlay for a pre-fetched PR row set.

        Args:
            rows: The open PRs (built by the host from
                :func:`fetch_open_prs`).
            status: Whether the ``gh`` fetch ran
                (:attr:`PrFetchStatus.OK`) or degraded
                (:attr:`PrFetchStatus.UNAVAILABLE`); selects the empty-list
                placeholder.
        """
        super().__init__()
        self._rows = rows
        self._status = status

    def _empty_text(self) -> str:
        """Return the status-aware placeholder for an empty row set."""
        if self._status is PrFetchStatus.UNAVAILABLE:
            return _EMPTY_UNAVAILABLE_TEXT
        return _EMPTY_OK_TEXT

    def compose(self) -> ComposeResult:
        """Yield the titled card, the PR list (or placeholder), and hint."""
        with VerticalScroll(id="pr-card"):
            yield Static(f"Open PRs · {len(self._rows)}", classes="pr-title")
            with VerticalScroll(id="pr-list"):
                if self._rows:
                    for index, row in enumerate(self._rows):
                        yield Static(
                            _render_row(row),
                            classes="pr-row",
                            id=f"pr-row-{index}",
                        )
                else:
                    yield Static(self._empty_text(), classes="pr-empty")
            yield Static("[ Enter open in browser · Esc to close ]", classes="pr-hint")

    def on_mount(self) -> None:
        """Paint the initial highlight on the first row (when any)."""
        if not self._rows:
            self.selected = -1
            return
        self._repaint_selection()

    def watch_selected(self) -> None:
        """Repaint the row highlight when the selection moves."""
        if self.is_mounted and self._rows:
            self._repaint_selection()

    def _repaint_selection(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted row."""
        for index in range(len(self._rows)):
            row_widget = self.query_one(f"#pr-row-{index}", Static)
            row_widget.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, clamped to the row range.

        Args:
            delta: ``-1`` to move up, ``+1`` to move down.
        """
        if not self._rows:
            return
        self.selected = max(0, min(self.selected + delta, len(self._rows) - 1))

    def action_open_web(self) -> None:
        """Open the highlighted PR in the browser via ``gh pr view --web``.

        Offloads the ``gh pr view --web`` shell-out to a worker thread so the
        Enter keypress returns immediately and the overlay never blocks on a
        slow ``gh``; :func:`_gh_view_web` swallows a missing binary / timeout /
        error so a bad ``gh`` never crashes the overlay. A no-op when the list
        is empty.
        """
        if not self._rows or not (0 <= self.selected < len(self._rows)):
            return
        target = self._rows[self.selected]
        logger.info(f"pr_open_web number={target.number} url={target.url!r}")
        self.run_worker(
            partial(_gh_view_web, target.number),
            group="pr-web",
            exclusive=True,
            thread=True,
        )

    def action_close(self) -> None:
        """Dismiss the PR-list overlay (``Esc``)."""
        self.dismiss(None)


def open_pr_list(
    app: App[None],
    rows: tuple[PrRow, ...],
    *,
    status: PrFetchStatus = PrFetchStatus.OK,
) -> bool:
    """Push the PR-list overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        rows: The pre-fetched open-PR rows.
        status: Whether the ``gh`` fetch ran or degraded; threaded into the
            modal so the empty-list placeholder matches the cause.

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = PrListModal(rows, status)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


def request_pr_list(app: App[None]) -> None:
    """Fetch the repo's open PRs off the event loop, then open the overlay.

    Runs :func:`fetch_open_prs` (a ``gh pr list`` shell-out, up to
    :data:`GH_TIMEOUT_S`) in a worker so the ``/pr`` keypress returns
    immediately; :func:`open_pr_list` pushes the overlay from the worker once
    the fetch lands (rows + degraded status threaded through).

    Args:
        app: The running App the overlay is pushed onto.
    """

    async def _fetch_and_open() -> None:
        fetch = await asyncio.to_thread(fetch_open_prs)
        open_pr_list(app, fetch.rows, status=fetch.status)

    app.run_worker(_fetch_and_open(), group="pr-fetch", exclusive=True)


__all__ = [
    "GH_PR_FIELDS",
    "GH_PR_LIMIT",
    "GH_TIMEOUT_S",
    "PR_CACHE_TTL_S",
    "PrFetch",
    "PrFetchStatus",
    "PrListModal",
    "PrRow",
    "fetch_open_prs",
    "open_pr_list",
    "parse_pr_rows",
    "request_pr_list",
    "reset_pr_cache",
]
