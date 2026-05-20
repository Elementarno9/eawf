"""``PrListModal`` — the ``/pr`` open-PRs overlay (C06 §5.7 / D21).

The ``/pr`` palette verb opens a list of the repo's open pull requests
(per the C06 brief §5.7 modal row + ``tui-ux-resolved`` §``:pr overlay``
[7:418-450] / Decision D21): per-repo PR rows, ``Enter`` opens the
highlighted PR via ``gh pr view --web``, a 60 s cache balances freshness
against the ``gh pr list`` cost, and the overlay degrades gracefully when
``gh`` is absent. ``Esc`` closes.

**Shell-out seam.** D21's data source is a lazy ``gh pr list --json``
shell-out cached for 60 s. The subprocess plumbing (spawn, timeout, cache
TTL) belongs to the daemon-mediated command surface that lands later in
this band; wiring a raw ``subprocess`` call here would duplicate that and
sidestep the daemon authority boundary. So this wave lands the **overlay
structure + the pure JSON parser** (:func:`parse_pr_rows`, which decodes
exactly the ``gh pr list --json number,title,author,state,url`` shape) and
the ``gh``-missing degraded path: the modal is constructed with a
pre-fetched row tuple (empty until the shell-out lands) and renders the
"run the gh shell-out" placeholder otherwise. The ``Enter`` →
``gh pr view --web <number>`` handler records the target and is a no-op
until the same shell-out wave wires the spawn.

The row model (:class:`PrRow`) + the parser are pure so decoding +
truncation are unit-testable without a live ``gh``; the modal is a thin
scrollable view over them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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

#: The ``gh pr list`` cache TTL in seconds (D21 — 60 s balances freshness
#: against the ~200-500 ms ``gh pr list`` cost per repo).
PR_CACHE_TTL_S: float = 60.0

#: The ``gh pr list --json`` field set the parser expects.
GH_PR_FIELDS: tuple[str, ...] = ("number", "title", "author", "state", "url")


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


def _render_row(row: PrRow) -> str:
    """Render one :class:`PrRow` as a single content-markup line.

    Args:
        row: The PR row to render.

    Returns:
        A content-markup string for one :class:`~textual.widgets.Static`.
    """
    author = f" @{row.author}" if row.author else ""
    return f"[$accent]#{row.number}[/]  {row.title}{author}"


class PrListModal(ModalScreen[None]):
    """Scrollable open-PR list (Enter opens web, Esc closes); D21 overlay.

    Built with a pre-fetched tuple of :class:`PrRow` (the host resolves
    them from the ``gh pr list`` shell-out once it lands) so the overlay
    never spawns a subprocess itself. ``Enter`` records the highlighted
    PR's ``gh pr view --web`` target; ``Esc`` closes. When the row set is
    empty the overlay shows the ``gh``-shell-out placeholder.
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

    def __init__(self, rows: tuple[PrRow, ...]) -> None:
        """Construct the overlay for a pre-fetched PR row set.

        Args:
            rows: The open PRs (built by the host from the ``gh pr list``
                shell-out; empty until that shell-out lands).
        """
        super().__init__()
        self._rows = rows

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
                    yield Static(
                        "no PRs cached yet — the gh pr list shell-out lands later "
                        "this band (degrades gracefully if gh is missing)",
                        classes="pr-empty",
                    )
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
        """Open the highlighted PR via ``gh pr view --web`` (seamed).

        Records the target PR url; the actual ``gh pr view --web <number>``
        spawn rides the same shell-out wave that fetches the list (the
        subprocess surface is daemon-mediated, not spawned from the
        overlay). A no-op when the list is empty.
        """
        if not self._rows or not (0 <= self.selected < len(self._rows)):
            return
        target = self._rows[self.selected]
        logger.info(f"pr_open_web number={target.number} url={target.url!r}")

    def action_close(self) -> None:
        """Dismiss the PR-list overlay (``Esc``)."""
        self.dismiss(None)


def open_pr_list(app: App[None], rows: tuple[PrRow, ...]) -> bool:
    """Push the PR-list overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack
    depth cap (C06 §5.7) is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        rows: The pre-fetched open-PR rows (empty until the shell-out
            lands).

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    modal = PrListModal(rows)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


__all__ = [
    "GH_PR_FIELDS",
    "PR_CACHE_TTL_S",
    "PrListModal",
    "PrRow",
    "open_pr_list",
    "parse_pr_rows",
]
