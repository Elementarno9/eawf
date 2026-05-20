"""``DetailModal`` — scrollable detail card for a selected entity (C06 §5.7).

The drill-in overlay opened when the operator presses ``Enter`` on a row:
the W17 widgets emit a selection message
(:class:`~eawf.tui_v2.widgets.backlog_table.BacklogTable.RowActivated`
carrying a backlog-item id,
:class:`~eawf.tui_v2.widgets.roadmap_tree.RoadmapTree.WaveSelected`
carrying a wave id), the shared
:class:`~eawf.tui_v2.scopes.ScopeScreen` routes the message here, and this
modal renders the resolved entity's detail in a scrollable card.

Per the C06 brief §5.7 modal-stack row for ``DetailModal``: a scrollable
detail card, ``Esc`` to close, stack depth ≤ 3 enforced by the App. The
``g <id>`` cross-jump and the per-overlay h/d/m/e/dp tabs the brief lists
ride later waves of this band; this wave lands the card itself + the
message-routing seam the W17 widgets were waiting for.

Entity resolution is a pure function (:func:`resolve_detail`) that takes
the reactive :class:`~eawf.state.models.State` and the selection id and
returns a typed :class:`DetailCard` (title + ordered field rows). Keeping
the formatting pure means the rendered detail is unit-testable without
mounting Textual, and the modal stays a thin view over it. Construct the
modal with a pre-built :class:`DetailCard` (the host screen builds it from
``app.state``) so the overlay never reaches back into App state itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

if TYPE_CHECKING:
    from eawf.state.models import State

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetailCard:
    """A resolved detail card: a title plus ordered label/value rows.

    Attributes:
        title: The card heading (e.g. ``wave P26-I01-W19`` /
            ``backlog B042``).
        rows: Ordered ``(label, value)`` pairs rendered one per line.
    """

    title: str
    rows: tuple[tuple[str, str], ...]


def _wave_card(state: State, wave_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the wave *wave_id*, or ``None``.

    Args:
        state: The bound state to resolve the wave from.
        wave_id: The selected wave id.

    Returns:
        The card, or ``None`` when the id is not a known wave.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    rows: list[tuple[str, str]] = [
        ("id", wave.id),
        ("iter", wave.iter_id),
        ("title", wave.title),
        ("status", wave.status.value),
    ]
    if wave.agent_role is not None:
        rows.append(("role", wave.agent_role.value))
    if wave.effort_bucket is not None:
        rows.append(("effort", wave.effort_bucket.value))
    if wave.deps:
        rows.append(("deps", ", ".join(wave.deps)))
    if wave.file_scopes:
        rows.append(("files", ", ".join(wave.file_scopes)))
    for criterion in wave.success_criteria:
        rows.append(("criterion", criterion))
    return DetailCard(title=f"wave {wave.id}", rows=tuple(rows))


def _backlog_card(state: State, item_id: str) -> DetailCard | None:
    """Build a :class:`DetailCard` for the backlog item *item_id*, or ``None``.

    Args:
        state: The bound state to resolve the item from.
        item_id: The selected backlog item id.

    Returns:
        The card, or ``None`` when the id is not a known backlog item.
    """
    if state.backlog is None:
        return None
    item = state.backlog.get(item_id)
    if item is None:
        return None
    rows: list[tuple[str, str]] = [
        ("id", item.id),
        ("title", item.title),
        ("priority", item.priority.value),
        ("status", item.status.value),
    ]
    if item.resolution is not None:
        rows.append(("resolution", item.resolution))
    return DetailCard(title=f"backlog {item.id}", rows=tuple(rows))


def resolve_detail(state: State | None, selection_id: str) -> DetailCard:
    """Resolve *selection_id* to a :class:`DetailCard` from *state*.

    Tries the wave table first, then the backlog table. An unresolvable id
    (or a ``None`` state) yields a fallback card naming the id so the
    operator sees *something* rather than a crash — the drill-in seam must
    stay total even when the state and the widget row briefly disagree
    (e.g. mid daemon-push).

    Args:
        state: The bound state, or ``None`` when no state is loaded.
        selection_id: The id carried by the selection message.

    Returns:
        The resolved detail card, or a fallback card for an unknown id.
    """
    if state is not None:
        card = _wave_card(state, selection_id) or _backlog_card(state, selection_id)
        if card is not None:
            return card
    return DetailCard(
        title=f"detail {selection_id}",
        rows=(("id", selection_id), ("note", "no detail available")),
    )


class DetailModal(ModalScreen[None]):
    """Scrollable detail card for a row-selected entity (Esc to close).

    Built with a pre-resolved :class:`DetailCard`; the host screen
    resolves the card from ``app.state`` via :func:`resolve_detail` when
    it routes the W17 selection message. The modal owns only the
    presentation + the ``Esc`` close binding.
    """

    DEFAULT_CSS: ClassVar[str] = """
    DetailModal {
        align: center middle;
    }
    DetailModal > #detail-card {
        width: 70%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    DetailModal .detail-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    DetailModal .detail-row {
        height: auto;
    }
    DetailModal .detail-label {
        color: $text-muted;
    }
    DetailModal .detail-hint {
        color: $text-muted;
        height: 1;
    }
    """

    #: ``Esc`` closes; this is the only binding the detail card owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, card: DetailCard) -> None:
        """Construct the modal for a pre-resolved card.

        Args:
            card: The detail card to render (built by the host screen from
                the selection id + the bound state).
        """
        super().__init__()
        self._card = card

    def compose(self) -> ComposeResult:
        """Yield the scrollable card: title, field rows, close hint."""
        with VerticalScroll(id="detail-card"):
            yield Static(self._card.title, classes="detail-title")
            for label, value in self._card.rows:
                yield Static(f"{label}: {value}", classes="detail-row")
            yield Static("[ Esc to close ]", classes="detail-hint")

    def action_close(self) -> None:
        """Dismiss the detail modal (``Esc``)."""
        self.dismiss(None)


__all__ = [
    "DetailCard",
    "DetailModal",
    "resolve_detail",
]
