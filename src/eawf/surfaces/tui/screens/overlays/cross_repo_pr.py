"""``CrossRepoPrModal`` -- the advisory cross-repo open-PRs overlay.

The ``/prs`` palette verb opens an ADVISORY, READ-ONLY view of open pull
requests across every repo in the explicit ``~/.eawf/registry.json``
registry -- the same repos the W24 workspace dashboard lists. Each
registered repo contributes a header line (``CODE  title  N open``) and,
under it, one row per open PR (number + title + author). A repo whose
``gh`` fetch fails shows an honest ``(unavailable)`` header rather than
tearing down the whole view; an empty registry, or one with no open PRs
anywhere, renders an honest-empty placeholder.

**Advisory + read-only.** The view is informational only: there are NO
PR actions (no merge / close / comment) and NO cross-repo writes -- those
are deferred. The only interactions are navigation (``up`` / ``down``)
and opening the highlighted PR in the browser via ``gh pr view --web``
(itself a read-only shell-out that mutates nothing); ``Esc`` closes.

**Spans repos via the registry, never a scan.** The repo set comes solely
from :func:`~eawf.platform.registry.read_registry` (the explicit
``~/.eawf/registry.json``) -- the same read-only boundary the
:class:`~eawf.surfaces.tui.widgets.registry_pane.RegistryPane` uses. Nothing
here scans, walks, or imports-from-discovery the filesystem; the registry
grows only via ``eawf init`` / ``eawf repo add``.

**Per-repo fetch reuses the single-repo path.** Each repo's open PRs come
from :func:`~eawf.surfaces.tui.screens.overlays.pr_list.fetch_open_prs` with the
repo's working directory as ``cwd`` -- the same read-only ``gh pr list
--json`` shell-out (TTL-cached, degrading to
:attr:`~eawf.surfaces.tui.screens.overlays.pr_list.PrFetchStatus.UNAVAILABLE`
on a missing / errored ``gh``) the ``/pr`` overlay already ships; the gh
integration is not reimplemented here. The grouping + row builder
(:func:`gather_cross_repo_prs`) is a pure function so the decode +
per-repo degradation are unit-testable without a live ``gh``; the modal is
a thin scrollable view over the groups it produces.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.platform.registry import Registry, RegistryReadError, read_registry
from eawf.surfaces.tui.screens.overlays.pr_list import (
    PrFetch,
    PrFetchStatus,
    PrRow,
    _gh_view_web,
    _render_row,
    fetch_open_prs,
)
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: Render-mode label threaded into the sigil helper when the host App
#: exposes no ``render_mode`` (a bare standalone harness): the unicode
#: column is the default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

#: A per-repo PR fetcher: ``(cwd) -> PrFetch``. Defaults to the
#: single-repo :func:`~eawf.surfaces.tui.screens.overlays.pr_list.fetch_open_prs`
#: shell-out; a test injects a deterministic stub so no test ever invokes
#: real ``gh`` or hits the network.
RepoFetcher = Callable[[Path], PrFetch]


@dataclass(frozen=True)
class CrossRepoGroup:
    """One registered repo's open-PR group in the cross-repo view.

    Attributes:
        code: The repo's project code (the group key, from the registry).
        title: The repo's human-readable title (falls back to ``code``).
        rows: The repo's open-PR rows (empty when it has none, or when its
            fetch was unavailable).
        status: Whether the repo's ``gh`` fetch ran
            (:attr:`~eawf.surfaces.tui.screens.overlays.pr_list.PrFetchStatus.OK`)
            or degraded
            (:attr:`~eawf.surfaces.tui.screens.overlays.pr_list.PrFetchStatus.UNAVAILABLE`);
            an unavailable repo shows an honest header rather than
            breaking the whole view.
    """

    code: str
    title: str
    rows: tuple[PrRow, ...]
    status: PrFetchStatus


def _default_fetcher(cwd: Path) -> PrFetch:
    """Fetch one repo's open PRs via the single-repo ``gh`` shell-out.

    Thin adapter over
    :func:`~eawf.surfaces.tui.screens.overlays.pr_list.fetch_open_prs` so the
    pure builder takes an injectable :data:`RepoFetcher` whose default is
    the real (read-only, TTL-cached) path.

    Args:
        cwd: The repo working directory to query.

    Returns:
        The repo's open-PR fetch (rows + status).
    """
    return fetch_open_prs(cwd)


def gather_cross_repo_prs(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    fetcher: RepoFetcher = _default_fetcher,
) -> tuple[CrossRepoGroup, ...]:
    """Build the cross-repo open-PR groups from the explicit registry.

    Resolves the repo set read-only from ``~/.eawf/registry.json`` via
    :func:`~eawf.platform.registry.read_registry` (never a filesystem
    scan / walk), then fetches each repo's open PRs through *fetcher* (the
    single-repo ``gh pr list`` path by default). Repos are grouped in code
    order so the view is deterministic.

    Degrades gracefully per repo: a repo whose fetch returns
    :attr:`~eawf.surfaces.tui.screens.overlays.pr_list.PrFetchStatus.UNAVAILABLE`
    keeps its group (with an honest unavailable status + empty rows) so one
    failing repo never aborts the others. A missing / corrupt registry
    yields an empty tuple -- the host renders the honest-empty placeholder.

    Args:
        registry_path: Explicit registry path. When ``None``, falls back
            to ``~/.eawf/registry.json`` (resolved via *home*).
        home: Test seam for the default-path branch; pass a ``tmp_path``
            root so tests never touch the operator's real registry.
            Ignored when *registry_path* is supplied directly.
        fetcher: Per-repo open-PR fetcher; defaults to the read-only
            single-repo ``gh`` shell-out. Injected by tests with a
            deterministic stub.

    Returns:
        The per-repo groups in code order (empty when the registry is
        unavailable or has zero repos).
    """
    registry: Registry
    try:
        registry = read_registry(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"gather_cross_repo_prs registry unavailable cause={exc!r}")
        return ()
    groups: list[CrossRepoGroup] = []
    for code in sorted(registry.repos):
        entry = registry.repos[code]
        fetch = fetcher(Path(entry.path))
        logger.info(
            f"gather_cross_repo_prs repo={code} status={fetch.status.value} prs={len(fetch.rows)}"
        )
        groups.append(
            CrossRepoGroup(
                code=code,
                title=entry.title or entry.code,
                rows=fetch.rows,
                status=fetch.status,
            )
        )
    return tuple(groups)


def total_open_prs(groups: tuple[CrossRepoGroup, ...]) -> int:
    """Return the total open-PR count across *groups* (for the card title)."""
    return sum(len(group.rows) for group in groups)


def _group_header(group: CrossRepoGroup) -> str:
    """Render one repo group's header line (code + title + count / status).

    Args:
        group: The repo group to head.

    Returns:
        A content-markup string for the group's header
        :class:`~textual.widgets.Static`.
    """
    label = escape_markup(f"{group.code}  {group.title}")
    if group.status is PrFetchStatus.UNAVAILABLE:
        return f"[$accent]{label}[/]  [$text-muted](unavailable)[/]"
    return f"[$accent]{label}[/]  {len(group.rows)} open"


#: Placeholder shown when the registry resolved zero repos -- the
#: cross-repo view has nothing to span. The substring ``no repos
#: registered`` mirrors the registry pane's honest-empty contract.
_EMPTY_NO_REPOS_TEXT: str = "no repos registered -- add a repo: eawf init / eawf repo add <path>"

#: Placeholder shown when every registered repo reported zero open PRs.
#: Distinct from the no-repos line so an empty-but-registered workspace
#: reads as honest-zero rather than no-registry.
_EMPTY_NO_PRS_TEXT: str = "no open pull requests across registered repos"

#: Per-repo line shown under an unavailable group's header so the reason a
#: repo contributes no rows is explicit (vs a genuinely empty repo).
_UNAVAILABLE_ROW_TEXT: str = "gh unavailable -- install + authenticate gh in this repo"

#: Per-repo line shown under an OK group with zero open PRs.
_REPO_EMPTY_ROW_TEXT: str = "no open pull requests"


class CrossRepoPrModal(ModalScreen[None]):
    """Advisory, read-only cross-repo open-PR view (Enter opens web, Esc closes).

    Built from a pre-fetched tuple of :class:`CrossRepoGroup` (the host
    resolves them off the event loop via :func:`gather_cross_repo_prs`) so
    the overlay never spawns a fetch itself. Each repo group renders a
    header line followed by its open-PR rows; an unavailable repo shows an
    honest header + hint rather than breaking the view. ``up`` / ``down``
    move the highlight across the flattened PR rows; ``Enter`` opens the
    highlighted PR in the browser via ``gh pr view --web`` (read-only);
    ``Esc`` closes. When no repo has an open PR the overlay shows the
    honest-empty placeholder.

    Read-only / advisory: there are NO action bindings that mutate -- no
    merge / close / comment, no cross-repo writes.
    """

    DEFAULT_CSS: ClassVar[str] = """
    CrossRepoPrModal {
        align: center middle;
    }
    CrossRepoPrModal > #xpr-card {
        width: 80%;
        max-width: 120;
        height: 70%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    CrossRepoPrModal .xpr-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    CrossRepoPrModal #xpr-list {
        height: 1fr;
    }
    CrossRepoPrModal .xpr-group {
        text-style: bold;
        height: auto;
        margin-top: 1;
    }
    CrossRepoPrModal .xpr-row {
        height: auto;
    }
    CrossRepoPrModal .xpr-row.-selected {
        text-style: bold reverse;
    }
    CrossRepoPrModal .xpr-muted {
        color: $text-muted;
        height: auto;
    }
    CrossRepoPrModal .xpr-empty {
        color: $text-muted;
        height: auto;
    }
    CrossRepoPrModal .xpr-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``up`` / ``down`` move the highlight across PR rows, ``Enter`` opens
    #: the PR in the browser, ``Esc`` closes. Vim ``j`` / ``k`` ride the
    #: arrows. There are no mutating bindings -- the view is advisory.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move(-1)", "up", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("enter", "open_web", "open", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index into the flattened PR-row list of the highlighted row
    #: (``-1`` when no repo has an open PR).
    selected: reactive[int] = reactive(0)

    def __init__(self, groups: tuple[CrossRepoGroup, ...]) -> None:
        """Construct the overlay for a pre-fetched set of repo groups.

        Args:
            groups: The per-repo open-PR groups (built by the host from
                :func:`gather_cross_repo_prs`).
        """
        super().__init__()
        self._groups = groups
        #: Flattened ``(repo_code, PrRow)`` pairs across all groups, in
        #: group order, so the highlight + ``Enter`` resolve a PR without
        #: re-walking the grouped structure.
        self._flat: tuple[tuple[str, PrRow], ...] = tuple(
            (group.code, row) for group in groups for row in group.rows
        )

    def _has_any_repo(self) -> bool:
        """Return whether the registry resolved at least one repo."""
        return bool(self._groups)

    def compose(self) -> ComposeResult:
        """Yield the titled card, the narrow-sigil grouped PR grid (or placeholder), + hint.

        Each per-PR row is drawn by
        :func:`~eawf.surfaces.tui.screens.overlays.pr_list._render_row` in the
        App's resolved render mode so its leading ``dispatch`` sigil tracks a
        unicode <-> ASCII flip; the card title leads with the shared
        ``overview`` sigil, resolved through the single
        :mod:`~eawf.surfaces.tui.widgets.sigils` SHAPE home.
        """
        mode = self._render_mode()
        title_sigil = chrome("overview", mode=mode)
        total = total_open_prs(self._groups)
        with VerticalScroll(id="xpr-card"):
            yield Static(
                f"{title_sigil} Cross-repo PRs * {len(self._groups)} repos * {total} open",
                classes="xpr-title",
            )
            with VerticalScroll(id="xpr-list"):
                yield from self._compose_body(mode=mode)
            yield Static(
                "[ Enter open in browser * Esc to close * read-only ]",
                classes="xpr-hint",
            )

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`~eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helper so an ``ascii`` flip swaps the title + row sigils to
        their ASCII column; falls back to the unicode column under a bare
        test harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def _compose_body(self, *, mode: str) -> ComposeResult:
        """Yield the grouped repo rows, or the honest-empty placeholder.

        Walks the groups in order, emitting a header per repo and a row per
        open PR. A flat row index is threaded so each rendered PR row gets a
        stable ``#xpr-row-<index>`` id matching :attr:`_flat`. An
        unavailable repo emits its honest hint line; an OK repo with zero
        PRs emits the empty-repo line. When no repo exists, or no repo has a
        PR, the single honest-empty placeholder is emitted instead.
        """
        if not self._has_any_repo():
            yield Static(_EMPTY_NO_REPOS_TEXT, classes="xpr-empty")
            return
        yield from self._compose_groups(mode=mode)
        if not self._flat:
            # Every registered repo was checked but none has an open PR --
            # the per-group lines above already say so; this is the
            # aggregate honest-empty summary.
            yield Static(_EMPTY_NO_PRS_TEXT, classes="xpr-empty")

    def _compose_groups(self, *, mode: str) -> ComposeResult:
        """Yield each repo group's header + its narrow-sigil PR / status rows.

        Walks the groups in order. Each group emits a header line; an
        unavailable repo follows with its honest hint line, an OK repo with
        zero PRs follows with the empty-repo line, and an OK repo with PRs
        follows with one row per PR carrying a stable ``#xpr-row-<index>``
        id matching :attr:`_flat`. Each PR row is drawn through the shared
        narrow-sigil :func:`~eawf.surfaces.tui.screens.overlays.pr_list._render_row`
        in *mode* so the leading sigil never strands against the selection
        rectangle.

        Args:
            mode: The App's resolved render-mode label threaded into the row
                sigil helper.
        """
        flat_index = 0
        for group in self._groups:
            yield Static(_group_header(group), classes="xpr-group", id=f"xpr-group-{group.code}")
            if group.status is PrFetchStatus.UNAVAILABLE:
                yield Static(f"  {_UNAVAILABLE_ROW_TEXT}", classes="xpr-muted")
                continue
            if not group.rows:
                yield Static(f"  {_REPO_EMPTY_ROW_TEXT}", classes="xpr-muted")
                continue
            for row in group.rows:
                yield Static(
                    f"  {_render_row(row, mode=mode)}",
                    classes="xpr-row",
                    id=f"xpr-row-{flat_index}",
                )
                flat_index += 1

    def on_mount(self) -> None:
        """Paint the initial highlight, then watch for a render-mode flip.

        Wires a ``render_mode`` watcher so a unicode <-> ASCII flip repaints
        the title + every flattened PR row's leading sigil in the active
        glyph column.
        """
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        if not self._flat:
            self.selected = -1
            return
        self._repaint_selection()

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the title + flattened PR rows when the render mode flips."""
        mode = self._render_mode()
        title_sigil = chrome("overview", mode=mode)
        total = total_open_prs(self._groups)
        self.query_one(".xpr-title", Static).update(
            f"{title_sigil} Cross-repo PRs * {len(self._groups)} repos * {total} open"
        )
        for index, (_code, row) in enumerate(self._flat):
            self.query_one(f"#xpr-row-{index}", Static).update(f"  {_render_row(row, mode=mode)}")
        if self._flat:
            self._repaint_selection()

    def watch_selected(self) -> None:
        """Repaint the row highlight when the selection moves."""
        if self.is_mounted and self._flat:
            self._repaint_selection()

    def _repaint_selection(self) -> None:
        """Toggle the ``-selected`` class onto the highlighted PR row."""
        for index in range(len(self._flat)):
            row_widget = self.query_one(f"#xpr-row-{index}", Static)
            row_widget.set_class(index == self.selected, "-selected")

    def action_move(self, delta: int) -> None:
        """Move the highlight by *delta*, clamped to the flattened-row range.

        Args:
            delta: ``-1`` to move up, ``+1`` to move down.
        """
        if not self._flat:
            return
        self.selected = max(0, min(self.selected + delta, len(self._flat) - 1))

    def action_open_web(self) -> None:
        """Open the highlighted PR in the browser via ``gh pr view --web``.

        Advisory + read-only: opening a PR in the browser mutates nothing.
        Offloads the ``gh pr view --web`` shell-out to a worker thread so the
        Enter keypress returns immediately and the overlay never blocks on a
        slow ``gh``; :func:`~eawf.surfaces.tui.screens.overlays.pr_list._gh_view_web`
        swallows a missing binary / timeout / error so a bad ``gh`` never
        crashes the overlay. A no-op when no repo has an open PR.
        """
        if not self._flat or not (0 <= self.selected < len(self._flat)):
            return
        repo_code, target = self._flat[self.selected]
        logger.info(
            f"cross_repo_pr_open_web repo={repo_code} number={target.number} url={target.url!r}"
        )
        self.run_worker(
            partial(_gh_view_web, target.number),
            group="xpr-web",
            exclusive=True,
            thread=True,
        )

    def action_close(self) -> None:
        """Dismiss the cross-repo PR overlay (``Esc``)."""
        self.dismiss(None)


def open_cross_repo_pr(app: App[None], groups: tuple[CrossRepoGroup, ...]) -> bool:
    """Push the cross-repo PR overlay onto *app* (modal-cap-aware).

    Routes through the App's ``push_modal`` helper so the modal-stack depth
    cap is enforced in one place; falls back to a plain ``push_screen``
    under a bare harness that lacks the cap helper.

    Args:
        app: The running App.
        groups: The pre-fetched per-repo open-PR groups.

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap rejected
        it.
    """
    modal = CrossRepoPrModal(groups)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


def request_cross_repo_pr(app: App[None]) -> None:
    """Gather cross-repo open PRs off the event loop, then open the overlay.

    Runs :func:`gather_cross_repo_prs` (a per-repo ``gh pr list`` sweep over
    the explicit registry) in a worker so the ``/prs`` keypress returns
    immediately even when several repos are queried;
    :func:`open_cross_repo_pr` pushes the overlay from the worker once the
    sweep lands. Read-only throughout.

    Args:
        app: The running App the overlay is pushed onto.
    """

    async def _gather_and_open() -> None:
        groups = await asyncio.to_thread(gather_cross_repo_prs)
        open_cross_repo_pr(app, groups)

    app.run_worker(_gather_and_open(), group="xpr-gather", exclusive=True)


__all__ = [
    "CrossRepoGroup",
    "CrossRepoPrModal",
    "RepoFetcher",
    "gather_cross_repo_prs",
    "open_cross_repo_pr",
    "request_cross_repo_pr",
    "total_open_prs",
]
