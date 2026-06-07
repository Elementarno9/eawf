"""``VerifierDrillModal`` -- the verifier-role drill overlay.

Drills into a scope's scored evidence: a small centred
:class:`~textual.screen.ModalScreen` that renders, one row per scored
criterion, the **oracle tier** that settled it and **who produced** the
evidence (``human`` / ``agent`` / ``tool`` / ``canary``). ``Esc`` closes.

The overlay is opened from the Trust mode's advertised ``v verifier`` key,
so the operator can see WHICH oracle tier verified each criterion and who
minted the evidence -- the verifier-role view over the same evidence rows
the oracle-determinism ratio is computed from -- without leaving the pane.

The row content is assembled by pure module functions
(:func:`verifier_rows` / :func:`render_verifier_rows`) so it is
unit-testable without mounting Textual; the screen is a thin scrollable
view over them. The modal holds no domain logic: it presents evidence rows
and renders the tier + producer per row.
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
    from collections.abc import Sequence

    from eawf.kernel.store.kinds.evidence import EvidenceRecord

logger = logging.getLogger(__name__)

#: Rendered when no scored evidence row joins the scope -- the honest-empty
#: verifier-chain path (nothing has been verified yet).
NO_VERIFIER_ROWS_NOTICE: str = "no scored evidence yet"

#: The evidence statuses that count as scored (reached a terminal verdict),
#: so an unscored row is not listed in the verifier drill.
_SCORED_STATUSES: frozenset[str] = frozenset({"pass", "fail", "blocked", "waived"})

#: Metrics key the deterministic-pass producer stamps the originating oracle
#: tier under. A row without it renders the dash placeholder for the tier.
_ORACLE_TIER_KEY: str = "oracle_tier"

#: Dash placeholder for a row with no recorded oracle tier. ASCII per the
#: source-glyph convention.
_TIER_DASH: str = "-"


@dataclass(frozen=True)
class VerifierRow:
    """One scored criterion's oracle tier + evidence producer.

    Attributes:
        scope_id: The scope the evidence row backs (the criterion / wave).
        tier: The oracle tier that settled the criterion, rendered as
            ``T<n>`` when the row stamped ``metrics["oracle_tier"]``, else
            the dash placeholder.
        produced_by: Who produced the evidence row (``human`` / ``agent`` /
            ``tool`` / ``canary``).
        status: The evidence row's terminal status.
    """

    scope_id: str
    tier: str
    produced_by: str
    status: str


def _tier_label(record: EvidenceRecord) -> str:
    """Return the ``T<n>`` oracle-tier label for *record*, or the dash.

    Reads the oracle tier the producer stamped under
    ``metrics["oracle_tier"]``. A row with no recorded tier (a non-
    deterministic attestation, or a row minted before the stamp landed)
    renders the dash placeholder rather than a fabricated tier.

    Args:
        record: The evidence row to read.

    Returns:
        ``T<n>`` when the tier is recorded, else :data:`_TIER_DASH`.
    """
    metrics = record.metrics or {}
    tier = metrics.get(_ORACLE_TIER_KEY)
    if isinstance(tier, int):
        return f"T{tier}"
    return _TIER_DASH


def verifier_rows(records: Sequence[EvidenceRecord]) -> tuple[VerifierRow, ...]:
    """Build one :class:`VerifierRow` per scored evidence row in *records*.

    Selects the rows that reached a terminal status (in
    :data:`_SCORED_STATUSES`) and projects each to its scope id, oracle tier
    (:func:`_tier_label`), producer, and status -- the verifier-role view.
    An unscored row is skipped. The honest-empty path (no scored rows) is an
    empty tuple, which the renderer surfaces as the no-rows notice.

    Args:
        records: The scope's evidence rows.

    Returns:
        The verifier rows in input order; empty when none is scored.
    """
    rows = tuple(
        VerifierRow(
            scope_id=record.scope_id,
            tier=_tier_label(record),
            produced_by=record.produced_by,
            status=record.status,
        )
        for record in records
        if record.status in _SCORED_STATUSES
    )
    logger.info(f"verifier_rows scored={len(rows)}")
    return rows


def render_verifier_rows(rows: tuple[VerifierRow, ...]) -> str:
    """Render the verifier rows as one text block.

    Lays out one ``<scope> <tier> <produced_by> <status>`` line per scored
    criterion so the operator reads which oracle tier verified each and who
    produced the evidence. An empty *rows* yields a single
    :data:`NO_VERIFIER_ROWS_NOTICE` line -- the honest-empty path the modal
    presents when nothing has been scored.

    Args:
        rows: The verifier rows to render.

    Returns:
        The newline-joined block (no trailing newline).
    """
    if not rows:
        return NO_VERIFIER_ROWS_NOTICE
    return "\n".join(f"{row.scope_id} {row.tier} {row.produced_by} {row.status}" for row in rows)


class VerifierDrillModal(ModalScreen[None]):
    """Verifier-role overlay: oracle tier + producer per scored row (Esc closes).

    Renders one row per scored evidence row -- its scope, the oracle tier
    that settled it, the producer, and the status -- in a scrollable card.
    Built thin over the pure :func:`verifier_rows` / :func:`render_verifier_rows`
    helpers so the content is testable without Textual.
    """

    #: One verifier drill at a time per scope: a re-fired drill over an
    #: already-open verifier drill is deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal` on the dedupe key.
    dedupe_singleton: ClassVar[bool] = False

    DEFAULT_CSS: ClassVar[str] = """
    VerifierDrillModal {
        align: center middle;
    }
    VerifierDrillModal > #verifier-drill-box {
        width: 70%;
        max-width: 90;
        height: auto;
        max-height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    VerifierDrillModal .verifier-drill-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    VerifierDrillModal .verifier-drill-section {
        text-style: bold;
        color: $accent;
        margin-top: 1;
        height: 1;
    }
    VerifierDrillModal .verifier-drill-row {
        height: auto;
        color: $text;
    }
    VerifierDrillModal .verifier-drill-hint {
        color: $text-muted;
        margin-top: 1;
        height: 1;
    }
    """

    #: ``Esc`` closes the drill overlay; the only binding it owns.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
    ]

    def __init__(self, records: Sequence[EvidenceRecord] = ()) -> None:
        """Construct the verifier drill over the scope's evidence *records*.

        Args:
            records: The scope's evidence rows (defaults to none -- the modal
                then renders the honest-empty verifier section).
        """
        super().__init__()
        self._records = tuple(records)
        self._rows = verifier_rows(self._records)
        #: Dedupe key so the App push chokepoint suppresses a duplicate
        #: verifier drill while one is already open.
        self.dedupe_key = "verifier-drill"

    def compose(self) -> ComposeResult:
        """Yield the scrollable drill card with the per-row tier + producer."""
        with VerticalScroll(id="verifier-drill-box"):
            yield Static("verifier: oracle tier + producer", classes="verifier-drill-title")
            yield Static("scope  tier  producer  status", classes="verifier-drill-section")
            for line in render_verifier_rows(self._rows).splitlines():
                yield Static(f"  {line}", classes="verifier-drill-row")
            yield Static("[ Esc to close ]", classes="verifier-drill-hint")

    def action_close(self) -> None:
        """Dismiss the verifier drill overlay (``Esc``)."""
        logger.info(f"verifier_drill_close rows={len(self._rows)}")
        self.dismiss(None)


__all__ = [
    "NO_VERIFIER_ROWS_NOTICE",
    "VerifierDrillModal",
    "VerifierRow",
    "render_verifier_rows",
    "verifier_rows",
]
