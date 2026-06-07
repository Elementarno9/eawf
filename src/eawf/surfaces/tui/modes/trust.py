"""``TrustModeScreen`` -- the Trust mode pane over ``compute_trust_scorecard``.

The Trust mode (digit ``2``) renders the estimation trust scorecard
(:func:`eawf.workflow.estimation.trust_scorecard.compute_trust_scorecard`)
as an honest provenance surface: the per-tier output counts, the
sample sizes (store record counts), the EU-calibration drift residual,
the verifier-reliability pass-rate, and the per-output trust labels that
say what backs each tier.

Honesty contract
----------------
The scorecard is a *trust* signal, so the pane never fabricates one. When
the project is data-starved -- no closed waves produced an output label,
every append-only store is empty, and the EU-calibration metric has no
samples -- :func:`is_data_starved` reports the starved state and the pane
renders an honest-negative banner ("insufficient data for a trust
signal") instead of a green score from no data. A score / tier line only
appears once a tier label, a store row, or a calibration sample actually
backs it. Every populated tier surfaces its residuals: the calibration
drift percent, the verifier pass-rate, and the per-output evidence refs.

The render half is a set of pure, content-markup-returning helpers (one
per scorecard section) so the composition is unit-testable without
mounting Textual; the screen is a thin :class:`ScopeScreen` body over
them. The pane reads the host app's read-only ``state`` + ``_state_path``
and computes the scorecard the same way the ``eawf trust`` CLI does --
state plus the append-only stores under the resolved ``state.json``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.workflow.estimation.trust_scorecard import (
    TrustScorecard,
    compute_trust_scorecard,
)

logger = logging.getLogger(__name__)

#: Honest-negative banner shown when the scorecard has no signal to back a
#: trust verdict (no output labels, empty stores, no calibration samples).
#: The pane renders this -- never a fabricated green score -- so a
#: data-starved project reads as "no signal yet", not "trusted".
DATA_STARVED_NOTICE: str = "insufficient data for a trust signal"

#: Sentinel rendered for a section whose backing rows are empty while the
#: scorecard as a whole still carries signal elsewhere (e.g. labels exist
#: but no deterministic verifier rows have landed). Muted so the operator
#: reads it as "not measured yet", distinct from a measured zero.
NO_DATA: str = "no data"

#: Cap on per-output label rows rendered in the labels section so a large
#: project does not flood the pane; an overflow count is appended.
_MAX_LABEL_ROWS: int = 12

#: Cap on evidence refs shown inline per output label.
_MAX_REFS_PER_LABEL: int = 3

#: Footer hints for the Trust pane (arrows primary). The mode digits are
#: surfaced by the always-visible mode row, not duplicated in the hint strip.
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key
#: tokens stay pinned to the canonical vocabulary.
_TRUST_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("v", "verifier"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)

#: Trust-scorecard refresh cadence in seconds (matches the metrics surface
#: 5 s tick so the pane can switch to daemon-push later without changing
#: the visible contract).
TRUST_REFRESH_S: float = 5.0

#: The evidence-source family that counts as deterministic for the oracle-
#: determinism ratio -- a code-gated falsifier (pytest, mypy, ruff), the
#: cheapest oracle tier. A ``jury`` or ``attested`` row is scored but is NOT
#: a deterministic pass.
_DETERMINISTIC_KIND: str = "deterministic"

#: The evidence statuses that count as "scored" toward the determinism ratio
#: denominator -- a row that reached a terminal verdict. A row without one of
#: these is not yet scored and does not enter the ratio.
_SCORED_STATUSES: frozenset[str] = frozenset({"pass", "fail", "blocked", "waived"})

#: Notice rendered in the oracle-determinism section when no evidence row has
#: been scored yet -- the honest-empty path (the ratio's denominator is zero,
#: so no ratio can be formed). Muted so it reads as "not measured yet".
NO_SCORED_EVIDENCE: str = "no scored evidence yet"

#: The evidence status that marks a criterion as escaped -- an operator
#: waiver cleared it rather than a gate passing it. The escape ledger lists
#: every such row so a waived criterion stays visible (an escape is the
#: thing the trust surface must never hide).
_WAIVED_STATUS: str = "waived"

#: Notice rendered in the escape-ledger section when no criterion was waived
#: -- the COMMON path (nothing escaped the gate). Muted so a clean ledger
#: reads as "no escapes", the desired state, not as missing data.
NO_ESCAPES_NOTICE: str = "no escaped criteria"

#: Cap on escape-ledger rows so a project with many waivers does not flood
#: the section; an overflow count is appended past the cap.
_MAX_ESCAPE_ROWS: int = 12


def is_data_starved(scorecard: TrustScorecard) -> bool:
    """Return whether *scorecard* lacks any signal to back a trust verdict.

    A scorecard is data-starved when all three signal sources are empty:
    no per-output trust labels (no closed wave produced one), every
    append-only store record count is zero, and the EU-calibration metric
    has no samples. In that state the pane renders the honest-negative
    banner rather than a fabricated score.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        ``True`` when no label, store row, or calibration sample backs a
        trust signal; ``False`` when at least one source carries data.
    """
    has_labels = bool(scorecard.output_labels)
    has_store_rows = any(count > 0 for count in scorecard.store_record_counts.values())
    has_calibration = scorecard.eu_calibration.sample_count > 0
    return not (has_labels or has_store_rows or has_calibration)


@dataclass(frozen=True)
class OracleDeterminism:
    """The oracle-determinism ratio over a scope's scored evidence rows.

    The ratio answers "what fraction of the scored evidence was settled by
    the cheapest deterministic oracle (a code gate) rather than a jury /
    attestation?" -- a high ratio means most closed criteria were verified
    by a falsifier the project re-runs for free, a low one means the verdict
    leaned on the (idle, in v0.5) jury or an operator sign-off.

    Attributes:
        deterministic_passes: Count of scored rows that are a deterministic
            pass (``evidence_kind == "deterministic"`` and ``status ==
            "pass"``) -- the ratio numerator.
        total_scored: Count of evidence rows that reached a terminal verdict
            (status in :data:`_SCORED_STATUSES`) -- the ratio denominator.
    """

    deterministic_passes: int
    total_scored: int

    @property
    def ratio(self) -> float | None:
        """Return the deterministic-pass fraction, or ``None`` when unscored.

        ``None`` (not ``0.0``) when :attr:`total_scored` is zero so an
        unmeasured ratio is never mistaken for a measured zero.
        """
        if self.total_scored == 0:
            return None
        return self.deterministic_passes / self.total_scored


def compute_oracle_determinism(records: Iterable[EvidenceRecord]) -> OracleDeterminism:
    """Compute the oracle-determinism ratio over *records*.

    Counts the scored evidence rows (a terminal status in
    :data:`_SCORED_STATUSES`) as the denominator and the deterministic
    passes (``evidence_kind == "deterministic"`` and ``status == "pass"``)
    as the numerator. A row that has not reached a terminal verdict is
    excluded from both -- it is not yet scored.

    Args:
        records: The scope's evidence rows (the EvidenceRecord rows the
            closed criteria produced).

    Returns:
        The :class:`OracleDeterminism` tally; ``total_scored == 0`` on the
        honest-empty path (no scored rows).
    """
    deterministic_passes = 0
    total_scored = 0
    for record in records:
        if record.status not in _SCORED_STATUSES:
            continue
        total_scored += 1
        if record.evidence_kind == _DETERMINISTIC_KIND and record.status == "pass":
            deterministic_passes += 1
    return OracleDeterminism(
        deterministic_passes=deterministic_passes,
        total_scored=total_scored,
    )


def render_oracle_determinism(determinism: OracleDeterminism) -> str:
    """Render the oracle-determinism ratio section body.

    Surfaces the ratio as a percentage plus the raw ``<passes>/<scored>``
    fraction so the operator reads both the headline and the n behind it. An
    unscored tally (no terminal-status rows) renders :data:`NO_SCORED_EVIDENCE`
    rather than a fabricated ``0%`` that could read as a measured verdict.

    Args:
        determinism: The computed determinism tally.

    Returns:
        A content-markup section body.
    """
    ratio = determinism.ratio
    if ratio is None:
        return f"[$muted]{NO_SCORED_EVIDENCE}[/]"
    return "\n".join(
        [
            f"ratio {ratio * 100.0:.0f}%",
            f"deterministic passes {determinism.deterministic_passes}"
            f" / {determinism.total_scored} scored",
        ]
    )


@dataclass(frozen=True)
class EscapedCriterion:
    """One escaped (operator-waived) criterion + its waiver reason.

    An *escape* is a criterion cleared by an operator waiver rather than a
    passing gate -- the verdict the trust surface must always surface so a
    waived criterion never hides behind a green close. The reason is the
    waiver's one-line justification (the evidence row's summary).

    Attributes:
        scope_id: The scope the waived evidence row backs (the criterion /
            wave the waiver cleared).
        reason: The waiver's one-line justification.
    """

    scope_id: str
    reason: str


def build_escape_ledger(records: Iterable[EvidenceRecord]) -> tuple[EscapedCriterion, ...]:
    """Build one ledger row per waived evidence row in *records*.

    Selects the evidence rows whose ``status`` is ``waived`` (a criterion an
    operator escaped) and projects each to its scope id + waiver reason (the
    row summary). Non-waived rows are skipped. The COMMON path is the empty
    tuple -- nothing was waived -- which the renderer surfaces as the
    honest-empty notice.

    Args:
        records: The scope's evidence rows.

    Returns:
        The escaped-criterion rows in input order; empty when none was
        waived.
    """
    escapes = tuple(
        EscapedCriterion(scope_id=record.scope_id, reason=record.summary)
        for record in records
        if record.status == _WAIVED_STATUS
    )
    logger.info(f"build_escape_ledger escapes={len(escapes)}")
    return escapes


def render_escape_ledger(escapes: tuple[EscapedCriterion, ...]) -> str:
    """Render the escape-ledger section body.

    Lists one ``<scope> -- <reason>`` line per escaped criterion so the
    operator reads exactly which criterion was waived and why. The rows are
    capped at :data:`_MAX_ESCAPE_ROWS` with an overflow count. The
    honest-empty path (no waivers -- the common, desired state) renders the
    muted :data:`NO_ESCAPES_NOTICE`.

    Args:
        escapes: The escaped-criterion rows.

    Returns:
        A content-markup section body.
    """
    if not escapes:
        return f"[$muted]{NO_ESCAPES_NOTICE}[/]"
    lines = [
        f"[$warn]{escape.scope_id}[/] [$muted]{escape.reason}[/]"
        for escape in escapes[:_MAX_ESCAPE_ROWS]
    ]
    overflow = len(escapes) - _MAX_ESCAPE_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def render_overview(scorecard: TrustScorecard) -> str:
    """Render the scorecard overview line (window + honesty headline).

    When the scorecard is data-starved the overview leads with the
    honest-negative banner; otherwise it reports the window and the total
    labelled-output count so the operator sees how much backs the tiers.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup overview string.
    """
    if is_data_starved(scorecard):
        return (
            f"window {scorecard.window}\n"
            f"[$warn]{DATA_STARVED_NOTICE}[/]\n"
            f"[$muted]no closed waves, store rows, or calibration samples yet[/]"
        )
    total = len(scorecard.output_labels)
    return f"window {scorecard.window}\nlabelled outputs {total}"


def render_tier_counts(scorecard: TrustScorecard) -> str:
    """Render the per-tier output counts (verified / attested / ...).

    Data-starved scorecards render :data:`NO_DATA` rather than a row of
    zeroes that could read as a measured verdict.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup string of one ``<tier> <count>`` line per tier,
        or the muted no-data sentinel when starved.
    """
    if is_data_starved(scorecard):
        return f"[$muted]{NO_DATA}[/]"
    counts = scorecard.tier_counts
    return "\n".join(
        [
            f"[$ok]verified[/] {counts.verified}",
            f"[$accent]attested[/] {counts.attested}",
            f"[$warn]deferred[/] {counts.deferred_outcome}",
            f"[$muted]unavailable[/] {counts.unavailable}",
        ]
    )


def render_store_counts(scorecard: TrustScorecard) -> str:
    """Render the append-only store record counts (the sample sizes).

    These counts are the honest n behind the scorecard -- how many
    estimate / actual / audit / evidence rows fell in the window. An
    all-zero map renders :data:`NO_DATA`.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup string of ``<store> n=<count>`` lines, or the
        muted no-data sentinel when every count is zero.
    """
    counts = scorecard.store_record_counts
    if not any(count > 0 for count in counts.values()):
        return f"[$muted]{NO_DATA}[/]"
    return "\n".join(f"{name} n={count}" for name, count in sorted(counts.items()))


def render_eu_calibration(scorecard: TrustScorecard) -> str:
    """Render the EU-calibration drift residual + its sample size.

    Surfaces the calibration badge, the sample count behind it, and the
    max bucket-drift percent (the residual) when present. A no-data badge
    with zero samples renders the muted sentinel so an unbacked badge is
    not mistaken for a measured one.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup string describing the calibration residual.
    """
    metric = scorecard.eu_calibration
    if metric.sample_count == 0:
        return f"[$muted]{NO_DATA}[/]"
    lines = [
        f"badge {_badge_markup(metric.drift_badge)}",
        f"samples {metric.sample_count}",
        f"nudged buckets {metric.nudged_bucket_count}",
    ]
    if metric.max_drift_pct is not None:
        lines.append(f"max drift {metric.max_drift_pct:.1f}%")
    return "\n".join(lines)


def render_verifier_reliability(scorecard: TrustScorecard) -> str:
    """Render the verifier-reliability pass-rate + its sample size.

    The pass-rate is only meaningful with deterministic verifier rows
    behind it; when the metric is deferred or carries no samples the
    pane shows the status note rather than a fabricated rate.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup string describing verifier reliability.
    """
    metric = scorecard.verifier_reliability
    if metric.pass_rate is None or metric.sample_count == 0:
        return f"[$muted]{metric.note}[/]"
    return "\n".join(
        [
            f"pass-rate {metric.pass_rate * 100.0:.0f}%",
            f"samples {metric.sample_count}",
        ]
    )


def render_output_labels(scorecard: TrustScorecard) -> str:
    """Render the per-output trust labels (what backs each tier).

    Each line names the scope, its tier, the human reason, and the
    inline evidence refs that substantiate it (capped at
    :data:`_MAX_REFS_PER_LABEL`). The rows are capped at
    :data:`_MAX_LABEL_ROWS` with an overflow count so a large project
    stays readable. An empty label list renders :data:`NO_DATA`.

    Args:
        scorecard: The computed trust scorecard.

    Returns:
        A content-markup string of one labelled-output line per row.
    """
    labels = scorecard.output_labels
    if not labels:
        return f"[$muted]{NO_DATA}[/]"
    lines: list[str] = []
    for label in labels[:_MAX_LABEL_ROWS]:
        refs = label.evidence_refs[:_MAX_REFS_PER_LABEL]
        ref_suffix = ""
        if refs:
            # No square brackets here: Textual content markup parses ``[...]``
            # as a tag and would swallow the ref ids. A muted ``refs:`` prefix
            # keeps the substantiating evidence visible.
            shown = " ".join(refs)
            extra = len(label.evidence_refs) - len(refs)
            tail = shown if extra <= 0 else f"{shown} +{extra}"
            ref_suffix = f" [$muted]refs: {tail}[/]"
        lines.append(
            f"{label.scope_id} {_tier_markup(label.tier)} [$muted]{label.reason}[/]{ref_suffix}"
        )
    overflow = len(labels) - _MAX_LABEL_ROWS
    if overflow > 0:
        lines.append(f"[$muted]+{overflow} more[/]")
    return "\n".join(lines)


def _tier_markup(tier: str) -> str:
    """Return *tier* wrapped in its palette-var colour span.

    Args:
        tier: One of the scorecard trust tiers.

    Returns:
        The tier name wrapped in the matching ``theme.tcss`` palette var.
    """
    palette = {
        "verified": "$ok",
        "attested": "$accent",
        "deferred_outcome": "$warn",
        "unavailable": "$muted",
    }
    return f"[{palette.get(tier, '$muted')}]{tier}[/]"


def _badge_markup(badge: str) -> str:
    """Return the calibration *badge* wrapped in its palette-var span.

    Args:
        badge: One of ``ok`` / ``bucket-drift`` / ``no-data``.

    Returns:
        The badge wrapped in the matching ``theme.tcss`` palette var.
    """
    palette = {
        "ok": "$ok",
        "bucket-drift": "$warn",
        "no-data": "$muted",
    }
    return f"[{palette.get(badge, '$muted')}]{badge}[/]"


class TrustModeScreen(ScopeScreen):
    """Trust mode pane over ``compute_trust_scorecard`` (honest provenance).

    Composes the scorecard sections -- overview, tier counts, store
    sample sizes, EU-calibration residual, verifier reliability, and the
    per-output trust labels -- inside the shared :class:`ScopeScreen`
    chassis. Reads the host app's read-only ``state`` + ``_state_path``
    and computes the scorecard exactly as the ``eawf trust`` CLI does.
    When the project is data-starved the pane renders the honest-negative
    banner rather than a fabricated score.
    """

    DEFAULT_CSS: ClassVar[str] = """
    TrustModeScreen #trust-body {
        height: 1fr;
        padding: 1 2;
    }
    TrustModeScreen .trust-section {
        border: solid $accent;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    TrustModeScreen #trust-section-overview.-starved {
        border: solid $warn;
    }
    """

    #: ``up`` / ``down`` scroll the section column; the chrome bindings
    #: (palette / help / quit / scope / mode digits) come from the shared
    #: chassis + app-wide bindings.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "up", show=False),
        Binding("down", "scroll_down", "down", show=False),
        Binding("pageup", "page_up", "page up", show=False),
        Binding("pagedown", "page_down", "page down", show=False),
        Binding("home", "scroll_home", "home", show=False),
        Binding("end", "scroll_end", "end", show=False),
        Binding("k", "scroll_up", "up", show=False),
        Binding("j", "scroll_down", "down", show=False),
        # ``v`` opens the verifier-role drill (oracle tier + producer per
        # scored row). Declared at the SCREEN level -- not on a child widget
        # that hides when there is no data -- so the advertised ``v verifier``
        # footer key resolves to a live binding even in the honest-empty mount
        # the affordance-parity gate probes.
        Binding("v", "verifier_drill", "verifier", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _TRUST_HINTS

    #: The section column body specs: ``(widget id, heading)`` in render
    #: order. :meth:`_section_body` dispatches each id to its render helper.
    SECTIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("trust-section-overview", "TRUST"),
        ("trust-section-tiers", "TIERS"),
        ("trust-section-determinism", "ORACLE DETERMINISM"),
        ("trust-section-escapes", "ESCAPE LEDGER"),
        ("trust-section-stores", "SAMPLE SIZES"),
        ("trust-section-calibration", "EU CALIBRATION"),
        ("trust-section-verifier", "VERIFIER RELIABILITY"),
        ("trust-section-labels", "OUTPUT LABELS"),
    )

    #: ``True`` once the most recent compute saw a data-starved scorecard;
    #: drives the overview border tint so the honest-negative state is
    #: visible at a glance. Watched so a refresh repaints the tint.
    starved: reactive[bool] = reactive(False, init=False)

    def __init__(self) -> None:
        """Construct the Trust mode screen with no externally bound evidence."""
        super().__init__()
        #: Externally supplied evidence rows for the oracle-determinism +
        #: escape-ledger sections. ``None`` means "read the rows from the
        #: store on refresh" (the live path); a non-``None`` tuple (pushed via
        #: :meth:`set_evidence`) overrides the store read so a test / fixture
        #: drives the sections without a live store. The render seam never
        #: calls :func:`~eawf.workflow.verify.readiness.compute` (it spawns
        #: live gate subprocesses) -- it reads the deterministic evidence
        #: store or the pushed fixture only.
        self._evidence_override: tuple[EvidenceRecord, ...] | None = None

    def compose_body(self) -> ComposeResult:
        """Yield the scrollable section column for the trust scorecard."""
        scorecard = self._current_scorecard()
        self.starved = scorecard is not None and is_data_starved(scorecard)
        with VerticalScroll(id="trust-body"):
            for section_id, heading in self.SECTIONS:
                section = Static(
                    self._section_body(section_id, scorecard),
                    id=section_id,
                    classes="trust-section",
                )
                section.border_title = heading
                yield section

    def on_mount(self) -> None:
        """Apply footer hints, arm the refresh seam, and tint the overview."""
        super().on_mount()
        self.set_interval(TRUST_REFRESH_S, self._refresh_all)
        self._repaint_starved()

    def watch_starved(self) -> None:
        """Repaint the overview tint when the starved verdict changes."""
        if self.is_mounted:
            self._repaint_starved()

    def _repaint_starved(self) -> None:
        """Toggle the ``-starved`` class onto the overview section."""
        overview = self.query("#trust-section-overview")
        if overview:
            overview.first(Static).set_class(self.starved, "-starved")

    def _refresh_all(self) -> None:
        """Recompute the scorecard and repaint every section."""
        scorecard = self._current_scorecard()
        self.starved = scorecard is not None and is_data_starved(scorecard)
        for section_id, _heading in self.SECTIONS:
            sections = self.query(f"#{section_id}")
            if sections:
                sections.first(Static).update(self._section_body(section_id, scorecard))
        window = scorecard.window if scorecard is not None else None
        logger.info(f"trust_refresh starved={self.starved} window={window!r}")

    def _section_body(self, section_id: str, scorecard: TrustScorecard | None) -> str:
        """Render *section_id*'s body from *scorecard*.

        Args:
            section_id: The section widget id (one of :attr:`SECTIONS`).
            scorecard: The computed scorecard, or ``None`` before state
                loads (renders an awaiting placeholder).

        Returns:
            The section's content-markup body.
        """
        if section_id == "trust-section-determinism":
            return render_oracle_determinism(
                compute_oracle_determinism(self._current_evidence_records())
            )
        if section_id == "trust-section-escapes":
            return render_escape_ledger(build_escape_ledger(self._current_evidence_records()))
        if scorecard is None:
            return f"[$muted]{NO_DATA}[/]"
        if section_id == "trust-section-overview":
            return render_overview(scorecard)
        if section_id == "trust-section-tiers":
            return render_tier_counts(scorecard)
        if section_id == "trust-section-stores":
            return render_store_counts(scorecard)
        if section_id == "trust-section-calibration":
            return render_eu_calibration(scorecard)
        if section_id == "trust-section-verifier":
            return render_verifier_reliability(scorecard)
        if section_id == "trust-section-labels":
            return render_output_labels(scorecard)
        return f"[$muted]{NO_DATA}[/]"

    def set_evidence(self, records: Iterable[EvidenceRecord] | None) -> None:
        """Bind evidence rows for the determinism + escape-ledger sections.

        The render seam never calls
        :func:`~eawf.workflow.verify.readiness.compute` (it spawns live gate
        subprocesses), so the evidence rows that back the oracle-determinism
        and escape-ledger sections are supplied externally here (a fixture
        under test; the daemon close envelope at runtime). Passing ``None``
        clears the override so the sections read the deterministic evidence
        store again. Repaints the affected sections when the pane is mounted.

        Args:
            records: The evidence rows to bind, or ``None`` to fall back to
                the store read.
        """
        self._evidence_override = None if records is None else tuple(records)
        if self.is_mounted:
            self._refresh_all()

    def action_verifier_drill(self) -> None:
        """Open the verifier-role drill modal over the scope's scored evidence.

        Builds the modal from the same evidence rows the determinism + escape
        sections read (:meth:`_current_evidence_records`) and pushes it through
        the App's cap-aware ``push_modal`` (falling back to ``push_screen``
        under a bare harness). The binding always resolves -- even in the
        honest-empty mount with no scored rows -- so the advertised ``v
        verifier`` affordance is never dead; the modal then renders its own
        honest-empty notice.
        """
        from eawf.surfaces.tui.modals.verifier_drill import VerifierDrillModal

        modal = VerifierDrillModal(self._current_evidence_records())
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(modal)
            return
        self.app.push_screen(modal)

    def _current_evidence_records(self) -> tuple[EvidenceRecord, ...]:
        """Return the evidence rows backing the determinism + escape sections.

        Prefers the externally pushed override (:meth:`set_evidence`); when
        none is bound, reads the deterministic evidence store under the
        resolved ``state.json`` via
        :func:`~eawf.workflow.estimation.trust_scorecard.read_store_projection`
        (a pure JSONL read -- no subprocess, no live gate). A failed / absent
        store read degrades to no rows so the sections render honest-empty
        rather than crashing the mode.

        Returns:
            The evidence rows, empty on the honest-empty path.
        """
        if self._evidence_override is not None:
            return self._evidence_override
        state_path = self._resolved_state_path()
        if state_path is None:
            return ()
        from eawf.workflow.estimation.trust_scorecard import read_store_projection

        try:
            projection = read_store_projection(state_path)
        except (OSError, ValueError, TypeError) as exc:
            logger.debug(f"_current_evidence_records store_read_failed cause={exc!r}")
            return ()
        records: list[EvidenceRecord] = []
        for typed in projection.evidence:
            if isinstance(typed.payload, EvidenceRecord):
                records.append(typed.payload)
        return tuple(records)

    def _current_state(self) -> State | None:
        """Return the host app's current read-only state, if loaded."""
        try:
            state = getattr(self.app, "state", None)
        except RuntimeError:
            return None
        return state if isinstance(state, State) else None

    def _current_scorecard(self) -> TrustScorecard | None:
        """Compute the trust scorecard from state + append-only stores.

        Computes the scorecard the same way ``eawf trust`` does: from the
        read-only state plus the append-only stores under the resolved
        ``state.json``. When the store read fails the compute falls back
        to a state-only scorecard (empty projection) rather than raising,
        so a malformed store row degrades the pane to its state-backed
        signal instead of crashing the mode.

        Returns:
            The computed scorecard, or ``None`` before state loads.
        """
        state = self._current_state()
        if state is None:
            return None
        state_path = self._resolved_state_path()
        try:
            return compute_trust_scorecard(state, state_path=state_path)
        except Exception as exc:
            if state_path is None:
                raise
            logger.debug(f"_current_scorecard store_fallback cause={exc!r}")
            return compute_trust_scorecard(state, state_path=None)

    def _resolved_state_path(self) -> Path | None:
        """Return the host app's read-only ``state.json`` path, if any."""
        try:
            state_path = getattr(self.app, "_state_path", None)
        except RuntimeError:
            return None
        return state_path if isinstance(state_path, Path) else None


__all__ = [
    "DATA_STARVED_NOTICE",
    "NO_DATA",
    "NO_ESCAPES_NOTICE",
    "NO_SCORED_EVIDENCE",
    "TRUST_REFRESH_S",
    "EscapedCriterion",
    "OracleDeterminism",
    "TrustModeScreen",
    "build_escape_ledger",
    "compute_oracle_determinism",
    "is_data_starved",
    "render_escape_ledger",
    "render_eu_calibration",
    "render_oracle_determinism",
    "render_output_labels",
    "render_overview",
    "render_store_counts",
    "render_tier_counts",
    "render_verifier_reliability",
]
