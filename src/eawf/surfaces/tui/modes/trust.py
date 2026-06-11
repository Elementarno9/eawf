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

The jury-authority section is the same honesty discipline applied to the
cross-vendor jury: the jury is held ADVISORY (its veto is logged, the close
still proceeds) until the I07 validation pass earns it block authority, so
the pane renders that literal advisory state -- a number-based scorecard of
dashes plus sample-count / cohort notes, never a fabricated trust number --
with the validation metrics pinned as ``[needs I07]`` placeholders the next
roadmap owns.

The render half is a set of pure, content-markup-returning helpers (one
per scorecard section) so the composition is unit-testable without
mounting Textual; the screen is a thin :class:`ScopeScreen` body over
them. The pane reads the host app's read-only ``state`` + ``_state_path``
and computes the scorecard the same way the ``eawf trust`` CLI does --
state plus the append-only stores under the resolved ``state.json``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.modals.calibration_drill import (
    CalibrationSet,
    calibration_set_from_report,
)
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph
from eawf.workflow.estimation.buckets import FIT_N_MIN, resolve_wave_actual
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
    render_hint_label("K", "calibration"),
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

#: Authority verdict the cross-vendor jury currently carries: it is held
#: ADVISORY (its veto is logged, the close still proceeds) until the I07
#: validation pass earns it block authority. The pane renders this literal,
#: never a fabricated "trusted / blocking" verdict, so the operator reads the
#: jury's real, un-validated state. The amber attention sigil leads the line.
JURY_AUTHORITY_ADVISORY: str = "advisory"

#: The placeholder marker pinned beside every jury-validation metric whose
#: real value the I07 validation reducer (Fleiss kappa, Brier / ECE,
#: known-bad catch) owns. Rendered LITERALLY rather than as a fabricated
#: number so a not-yet-built metric reads as "another roadmap owns this", not
#: as a measured zero. Honest-negative is sacred: no fake green trust number.
NEEDS_I07_PLACEHOLDER: str = "[needs I07]"

#: The honest-negative copy pinned on each jury-validation metric row whose
#: backing data does not exist yet -- a dash plus the literal reason the value
#: is absent. None of these is a fabricated trust number; each is a dash and a
#: sample-count / cohort note so the row reads "no signal yet", never
#: "trusted". The golden pins these literals verbatim.
JURY_ECE_STARVED: str = "-- starved"
JURY_VARIANCE_STARVED: str = "-- starved"
JURY_FLEISS_NEED_COHORT: str = "-- need cohort"
JURY_KNOWN_BAD_NEED_LABELS: str = "-- need labels"

#: The Wilson lower-bound row renders the current score against the bar it must
#: clear before the jury earns block authority. Both sides are number-based
#: (no fabricated trust verdict): a measured ``0.00`` against the ``0.75``
#: floor the I07 staged authority gate enforces.
JURY_WILSON_FLOOR: float = 0.75

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

#: Minimum count of closed waves with captured elapsed EU before the
#: bucket re-fit (B069) can run. Mirrors :data:`eawf.workflow.estimation.buckets.FIT_N_MIN`
#: so the readiness tile and the calibration fit agree on the same floor.
_CALIBRATION_READY_THRESHOLD: int = FIT_N_MIN


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


@dataclass(frozen=True)
class CalibrationReadiness:
    """Whether enough captured elapsed EU backs a bucket re-fit (B069).

    The bucket-drift re-fit reads each closed wave's measured
    ``ActualSummary.elapsed_eu`` (the close path now records it from the
    session runtime); the re-fit is only trustworthy once a floor of waves
    carries that signal. This tile counts the captured waves against the
    floor so the operator reads "how close is the calibration to having
    enough data to act on".

    Attributes:
        captured_waves: Count of closed waves whose actual records a
            positive ``elapsed_eu`` (the captured-runtime signal).
        threshold: The minimum captured-wave count the re-fit needs.
    """

    captured_waves: int
    threshold: int

    @property
    def ready(self) -> bool:
        """Return whether the captured-wave count meets the floor."""
        return self.captured_waves >= self.threshold


def compute_calibration_readiness(
    state: State,
    *,
    threshold: int = _CALIBRATION_READY_THRESHOLD,
) -> CalibrationReadiness:
    """Count closed waves with captured elapsed EU for the re-fit floor.

    A wave contributes when it is CLOSED and its resolved
    :class:`~eawf.kernel.state.models.ActualSummary` records a positive
    ``elapsed_eu`` -- the measured-runtime signal the close path now
    captures. This is a pure read over the typed state (no store read, no
    subprocess), so the render seam can compute it without the live
    readiness ``compute`` path.

    Args:
        state: Loaded typed :class:`State` snapshot (read-only).
        threshold: The minimum captured-wave count the re-fit needs;
            defaults to :data:`_CALIBRATION_READY_THRESHOLD`.

    Returns:
        The :class:`CalibrationReadiness` tally.
    """
    captured = 0
    for wave in state.waves.values():
        if wave.status != WaveStatus.CLOSED:
            continue
        actual = resolve_wave_actual(state, wave.id)
        if actual is not None and actual.elapsed_eu > 0.0:
            captured += 1
    return CalibrationReadiness(captured_waves=captured, threshold=threshold)


def render_calibration_readiness(readiness: CalibrationReadiness) -> str:
    """Render the calibration-readiness tile body.

    Shows the captured-wave count against the floor plus a ready /
    not-ready verdict so the operator reads whether the bucket re-fit has
    enough captured elapsed EU to act on. The verdict colour reflects the
    state: ready is the green target, not-ready is the muted "collecting"
    state (not a warning -- it is the expected early state).

    Args:
        readiness: The computed readiness tally.

    Returns:
        A content-markup tile body.
    """
    verdict = "[$ok]ready[/]" if readiness.ready else "[$muted]not-ready[/]"
    return "\n".join(
        [
            f"captured waves {readiness.captured_waves} / {readiness.threshold}",
            f"calibration {verdict}",
        ]
    )


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


def render_jury_authority(*, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the cross-vendor jury's advisory-to-block authority scorecard.

    Surfaces the jury's CURRENT authority state honestly: the jury is held
    ADVISORY (its veto is logged, the close still proceeds) until the I07
    validation pass earns it block authority. The section renders as a
    number-based scorecard whose validation metrics (Fleiss kappa, ECE,
    variance, known-bad catch, Wilson lower-bound) are dashes plus their
    sample-count / cohort notes -- never a fabricated trust number, because
    the I07 validation reducer owns the real values. A :data:`NEEDS_I07_PLACEHOLDER`
    marker pins each metric the next roadmap owns.

    The leading banner wears the attention sigil (``glyph`` via the
    :func:`~eawf.surfaces.tui.widgets.sigils.chrome` helper) so the advisory
    state reads as "needs attention", never as a pending ring. The OVERRIDDEN
    marker (the half-filled :data:`~eawf.surfaces.tui.widgets.sigils.Sigil.CLAIMED`
    sigil) lands ONLY on the authority row, marking that the jury's verdict is
    currently held / overridden to advisory.

    Args:
        mode: The App's resolved render-mode label, threaded so the sigils
            resolve their ASCII / unicode column; defaults to
            :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE`.

    Returns:
        A content-markup section body: the advisory banner sigil row, the
        number-based validation-metric rows (dashes + sample counts), and
        the overridden-marked authority row.
    """
    attention = chrome("attention", mode=mode)
    overridden = glyph(Sigil.CLAIMED, mode=mode)
    return "\n".join(
        [
            f"[$warn]{attention} jury held {JURY_AUTHORITY_ADVISORY} until validated[/]",
            f"[$muted]Fleiss kappa[/] {JURY_FLEISS_NEED_COHORT}",
            f"[$muted]ECE[/] {JURY_ECE_STARVED}",
            f"[$muted]variance[/] {JURY_VARIANCE_STARVED}",
            f"[$muted]known-bad catch[/] {JURY_KNOWN_BAD_NEED_LABELS}",
            f"[$muted]Wilson-LB[/] 0.00 / {JURY_WILSON_FLOOR:.2f}",
            f"[$warn]authority {JURY_AUTHORITY_ADVISORY} {overridden}[/]"
            f" [$muted]overridden -- veto logged, close proceeds[/]",
            # The placeholder's leading ``[`` is escaped so Textual content
            # markup renders the literal ``[needs I07]`` rather than parsing it
            # as a markup tag and swallowing the text.
            f"[$muted]validation reducer {escape(NEEDS_I07_PLACEHOLDER)}[/]",
        ]
    )


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
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }
    TrustModeScreen #trust-section-overview.-starved {
        border: round $warn;
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
        # ``K`` opens the jury-calibration drill (Brier + ECE). Likewise
        # declared at the SCREEN level so the advertised ``K calibration``
        # footer key resolves even with no calibration set bound (the common
        # v0.5 path -- the jury is idle), which the affordance gate probes.
        Binding("K", "calibration_drill", "calibration", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _TRUST_HINTS

    #: The section column body specs: ``(widget id, heading)`` in render
    #: order. :meth:`_section_body` dispatches each id to its render helper.
    SECTIONS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("trust-section-overview", "TRUST"),
        ("trust-section-authority", "JURY AUTHORITY"),
        ("trust-section-tiers", "TIERS"),
        ("trust-section-determinism", "ORACLE DETERMINISM"),
        ("trust-section-escapes", "ESCAPE LEDGER"),
        ("trust-section-stores", "SAMPLE SIZES"),
        ("trust-section-calibration", "EU CALIBRATION"),
        ("trust-section-calibration-readiness", "CALIBRATION READINESS"),
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
        #: Externally pushed override for the jury calibration set. ``None``
        #: means "compute the calibration from the host state on demand" (the
        #: live path -- :meth:`_current_calibration` scores the jury-validation
        #: cohort and binds its real Brier + ECE); a non-``None`` value (pushed
        #: via :meth:`set_calibration`) drives the drill from a fixture without a
        #: live state. When the live compute finds a starved cohort it yields
        #: ``None`` and the drill renders honest-empty.
        self._calibration: CalibrationSet | None = None
        #: Externally supplied calibration-readiness tally for the CALIBRATION
        #: READINESS tile. ``None`` means "compute it from the host state on
        #: refresh" (the live path); a non-``None`` value (pushed via
        #: :meth:`set_calibration_readiness`) overrides the compute so a test /
        #: fixture drives the tile. The compute is a pure read over closed-wave
        #: actuals -- the render seam never calls the live readiness compute.
        self._calibration_readiness: CalibrationReadiness | None = None

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

    #: Dispatch from a scorecard-backed section id to its render helper. The
    #: evidence-backed (determinism / escape) and state-backed (calibration
    #: readiness) sections are handled ahead of this map because they read
    #: their own data source rather than the scorecard.
    _SCORECARD_RENDERERS: ClassVar[dict[str, Callable[[TrustScorecard], str]]] = {
        "trust-section-overview": render_overview,
        "trust-section-tiers": render_tier_counts,
        "trust-section-stores": render_store_counts,
        "trust-section-calibration": render_eu_calibration,
        "trust-section-verifier": render_verifier_reliability,
        "trust-section-labels": render_output_labels,
    }

    def _section_body(self, section_id: str, scorecard: TrustScorecard | None) -> str:
        """Render *section_id*'s body from *scorecard*.

        Args:
            section_id: The section widget id (one of :attr:`SECTIONS`).
            scorecard: The computed scorecard, or ``None`` before state
                loads (renders an awaiting placeholder).

        Returns:
            The section's content-markup body.
        """
        if section_id == "trust-section-authority":
            return render_jury_authority(mode=self._render_mode())
        if section_id == "trust-section-determinism":
            return render_oracle_determinism(
                compute_oracle_determinism(self._current_evidence_records())
            )
        if section_id == "trust-section-escapes":
            return render_escape_ledger(build_escape_ledger(self._current_evidence_records()))
        if section_id == "trust-section-calibration-readiness":
            readiness = self._current_calibration_readiness()
            if readiness is None:
                return f"[$muted]{NO_DATA}[/]"
            return render_calibration_readiness(readiness)
        renderer = self._SCORECARD_RENDERERS.get(section_id)
        if scorecard is None or renderer is None:
            return f"[$muted]{NO_DATA}[/]"
        return renderer(scorecard)

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

    def set_calibration_readiness(self, readiness: CalibrationReadiness | None) -> None:
        """Bind the calibration-readiness tally for the CALIBRATION READINESS tile.

        ``None`` clears the override so the tile computes its tally from the
        host state on refresh (the live path); a non-``None`` value drives the
        tile from a fixture without a live state. Repaints the affected section
        when the pane is mounted.

        Args:
            readiness: The readiness tally to bind, or ``None`` to fall back to
                the state-derived compute.
        """
        self._calibration_readiness = readiness
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

    def set_calibration(self, calibration: CalibrationSet | None) -> None:
        """Pin an explicit jury calibration set, overriding the live compute.

        ``None`` clears the override so the drill computes its calibration from
        the host state on demand (the live path -- :meth:`_current_calibration`
        scores the jury-validation cohort); a non-``None`` value drives the drill
        from a fixture without a live state. The common v0.5 path leaves the
        override ``None``: the live compute then finds a starved cohort and the
        drill renders honest-empty.

        Args:
            calibration: The jury calibration set to pin, or ``None`` to fall
                back to the state-derived live compute.
        """
        self._calibration = calibration

    def _current_calibration(self) -> CalibrationSet | None:
        """Return the calibration set backing the drill (override or live).

        Prefers the externally pinned override (:meth:`set_calibration`); when
        none is bound, scores the jury-validation cohort from the host state via
        :func:`~eawf.observability.eval.jury_validation.build_jury_validation_cohort`
        + :func:`~eawf.observability.eval.jury_validation.validate_jury` and binds
        its real Brier + ECE through
        :func:`~eawf.surfaces.tui.modals.calibration_drill.calibration_set_from_report`.
        A starved cohort (insufficient signal) -- the common v0.5 path, since the
        per-juror ballot store is not yet persisted -- yields ``None`` so the
        drill renders honest-empty rather than a fabricated score. A failed /
        absent store read degrades to ``None`` likewise.

        Returns:
            The pinned or computed calibration set, or ``None`` when none is
            pinned and the cohort refused to score.
        """
        if self._calibration is not None:
            return self._calibration
        state = self._current_state()
        state_path = self._resolved_state_path()
        if state is None or state_path is None:
            return None
        from eawf.observability.eval.jury_validation import (
            build_jury_validation_cohort,
            validate_jury,
        )

        try:
            cohort = build_jury_validation_cohort(state, state_path)
            # The per-juror ballot store is not yet persisted (the live jury is
            # idle), so the cohort reduces honest-empty today and the report
            # refuses to score. The moment a ballot store lands, the cohort's
            # labelled waves resolve their ballots here and the drill surfaces a
            # real Brier + ECE.
            report = validate_jury(cohort, ballots_by_wave={})
        except (OSError, ValueError, TypeError) as exc:
            logger.debug(f"_current_calibration cohort_read_failed cause={exc!r}")
            return None
        return calibration_set_from_report(report)

    def action_calibration_drill(self) -> None:
        """Open the jury-calibration drill modal (Brier + ECE).

        Builds the drill from the live-computed (or pinned) calibration set
        (:meth:`_current_calibration`) and pushes the modal through the App's
        cap-aware ``push_modal`` (falling back to ``push_screen`` under a bare
        harness). The binding always resolves -- even with a starved cohort (the
        common v0.5 path, the ballot store is not yet persisted) -- so the
        advertised ``K calibration`` affordance is never dead; the modal then
        renders its own honest-empty notice.
        """
        from eawf.surfaces.tui.modals.calibration_drill import CalibrationDrillModal

        modal = CalibrationDrillModal(self._current_calibration())
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

    def _current_calibration_readiness(self) -> CalibrationReadiness | None:
        """Return the calibration-readiness tally for the readiness tile.

        Prefers the externally pushed override
        (:meth:`set_calibration_readiness`); when none is bound, computes the
        tally from the host state's closed-wave actuals via
        :func:`compute_calibration_readiness` (a pure read -- no store, no
        subprocess). Returns ``None`` before state loads so the tile renders
        the awaiting placeholder rather than a fabricated zero.

        Returns:
            The readiness tally, or ``None`` before state loads.
        """
        if self._calibration_readiness is not None:
            return self._calibration_readiness
        state = self._current_state()
        if state is None:
            return None
        return compute_calibration_readiness(state)

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

    def _render_mode(self) -> RenderMode:
        """Return the host app's live render mode, or the safe default.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the sigil
        helpers so an ASCII / unicode flip rerenders the jury-authority sigil
        rows. Falls back to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a
        bare harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The active ``"unicode"`` / ``"ascii"`` mode.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)


__all__ = [
    "DATA_STARVED_NOTICE",
    "JURY_AUTHORITY_ADVISORY",
    "JURY_ECE_STARVED",
    "JURY_FLEISS_NEED_COHORT",
    "JURY_KNOWN_BAD_NEED_LABELS",
    "JURY_VARIANCE_STARVED",
    "JURY_WILSON_FLOOR",
    "NEEDS_I07_PLACEHOLDER",
    "NO_DATA",
    "NO_ESCAPES_NOTICE",
    "NO_SCORED_EVIDENCE",
    "TRUST_REFRESH_S",
    "CalibrationReadiness",
    "EscapedCriterion",
    "OracleDeterminism",
    "TrustModeScreen",
    "build_escape_ledger",
    "compute_calibration_readiness",
    "compute_oracle_determinism",
    "is_data_starved",
    "render_calibration_readiness",
    "render_escape_ledger",
    "render_eu_calibration",
    "render_jury_authority",
    "render_oracle_determinism",
    "render_output_labels",
    "render_overview",
    "render_store_counts",
    "render_tier_counts",
    "render_verifier_reliability",
]
