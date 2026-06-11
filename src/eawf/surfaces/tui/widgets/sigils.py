"""Lifecycle + chrome sigil glyphs for the Eae TUI cosmic-terminal reskin.

This module is the SHAPE layer of the two-axis visual vocabulary the
reskin panes share. Its sibling
:mod:`~eawf.surfaces.tui.widgets.status_tint` is the COLOUR layer: it maps
a lifecycle-status string to a Wong deuteranopia-safe ``#rrggbb`` hex via
:data:`~eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS` /
:func:`~eawf.surfaces.tui.widgets.status_tint.status_colour`. Shape comes
from here; colour comes from there. No pane invents a glyph or a hex of
its own -- they call :func:`glyph` / :func:`chrome` for the mark and
:func:`tint` for the hue so a retune of either axis lands in one home.

The module is PURE: it imports no Textual primitive and holds no state,
so every consumer (the roadmap tree, the status pane, the dispatch /
gate / attention chrome) can resolve a glyph string without mounting a
widget, and the unit tests cover the whole surface lock-free.

Two glyph columns ship per mark -- a unicode column and an ASCII
fallback. The active column is chosen by the App's resolved render mode
string (see :func:`glyph` / :func:`chrome`): ``"ascii"`` selects the
ASCII column, any other label (``"unicode"`` and the legacy ``"braille"``
alias a sibling wave renames) selects the unicode column, so the helper
stays decoupled from the not-yet-landed render-mode rename.

The ASCII lifecycle alphabet is deliberately DECONFLICTED off the EU /
burn bar glyphs (the bar fills with ``#`` and pads with ``-``; see
:data:`~eawf.surfaces.tui.widgets.eu_bar.GLYPH_FULL` /
:data:`~eawf.surfaces.tui.widgets.eu_bar.GLYPH_EMPTY`). That is why the
closed sigil is ``@`` rather than the bar's ``#`` and the pending sigil
is ``o`` rather than the bar's ``-``: a row that renders a sigil beside
an inline bar would otherwise read ambiguously in ASCII mode. The
regression test pins the empty intersection of the two alphabets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionStatus,
    AuditVerdict,
    BacklogStatus,
    ClaimStatus,
    IterStatus,
    OpenQuestionStatus,
    OutcomeStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX, status_colour

#: The render-mode label that selects the ASCII glyph column. Any other
#: label selects the unicode column (see :func:`glyph` / :func:`chrome`),
#: so the two not-yet-unified unicode labels (``"unicode"`` and the legacy
#: ``"braille"`` alias) both resolve to the unicode glyphs without this
#: module knowing which name is current.
ASCII_MODE: str = "ascii"


class Sigil(Enum):
    """Lifecycle-state marks the reskin panes render.

    The enum value of each member is the canonical lifecycle-status
    string the SHAPE and COLOUR layers share, EXCEPT :attr:`RUNNING`,
    whose value is the human ``"running"`` while its tint resolves
    against the ``"in_progress"`` status key (see :func:`tint`). Phase
    and iter rows draw the four-state subset (they never enter the
    CLAIMED state, which is wave-only); a consumer renders that subset
    simply by never passing :attr:`CLAIMED` -- no separate API exists for
    it.

    :attr:`ABANDONED` is the withheld / terminal-not-closed mark: a wave,
    iter, or phase that ended without reaching the clean CLOSED terminal
    (the WaveStatus / IterStatus ABANDONED + PhaseStatus ARCHIVED cascade,
    a BLOCKED agent verdict, a WAIVED outcome, a DEFERRED backlog item).
    It is shape-distinct from :attr:`PENDING` -- a withheld state must not
    read as a not-yet-run one -- and wears the muted ``abandoned`` grey
    tint so it recedes rather than alarms.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    CLOSED = "closed"
    FAILED = "failed"
    ABANDONED = "abandoned"


#: Lifecycle shapes: :class:`Sigil` -> ``(unicode, ascii)``. The unicode
#: column is written with ``\uXXXX`` escapes so the source stays
#: ASCII-clean; the rendered marks are pending=hollow-dotted-circle,
#: claimed=half-filled-circle, running=filled-diamond,
#: closed=filled-circle, failed=multiplication-x,
#: abandoned=circle-with-vertical-bar (the withheld / circled-division
#: slash). The ascii column is deconflicted off the bar glyphs (see the
#: module docstring): closed is ``@`` not ``#`` and pending is ``o`` not
#: ``-``. The abandoned ascii ``%`` is chosen to collide with NEITHER the
#: other lifecycle sigils (``o ( * @ x``) NOR the bar glyphs (``# -``) NOR
#: the chrome ascii (``> [] ! ~ = $``), so a withheld row never reads
#: ambiguously beside an inline bar or a chrome mark.
_LIFECYCLE: dict[Sigil, tuple[str, str]] = {
    Sigil.PENDING: ("\u25cc", "o"),  # hollow dotted circle
    Sigil.CLAIMED: ("\u25d0", "("),  # half-filled circle (left)
    Sigil.RUNNING: ("\u25c6", "*"),  # filled diamond
    Sigil.CLOSED: ("\u25cf", "@"),  # filled circle
    Sigil.FAILED: ("\u2715", "x"),  # multiplication x
    Sigil.ABANDONED: ("\u2298", "%"),  # circled division slash (withheld)
}

#: Chrome / action shapes: role string -> ``(unicode, ascii)``. The
#: unicode column uses ``\uXXXX`` escapes to keep the source ASCII-clean;
#: the rendered marks are dispatch=heavy-right-angle-quote,
#: gate=square-with-rounded-corners-lozenge, attention=up-triangle,
#: harmony=almost-equal, overview=identical-to (triple bar),
#: runtime=dollar, check_on=square-with-fill, check_off=hollow-square,
#: brand=fisheye (the leading brand mark before the ``E\u00e4`` wordmark, the
#: terminal-renderable stand-in for the Seal SVG that Textual cannot draw;
#: ascii fallback ``*`` per ``brand-and-sigils.md`` Decision A).
_CHROME: dict[str, tuple[str, str]] = {
    "dispatch": ("\u276f", ">"),  # heavy right-pointing angle quote
    "gate": ("\u2394", "[]"),  # software-function / lozenge
    "attention": ("\u25b3", "!"),  # white up-pointing triangle
    "harmony": ("\u2248", "~"),  # almost equal to
    "overview": ("\u2261", "="),  # identical to (triple bar)
    "runtime": ("$", "$"),  # dollar (same in both columns)
    "check_on": ("\u25a3", "[x]"),  # square with fill
    "check_off": ("\u25a2", "[ ]"),  # hollow square
    "brand": ("\u25c9", "*"),  # fisheye -- the leading brand mark
}

#: :class:`Sigil` -> the :data:`~eawf.surfaces.tui.widgets.status_tint.STATUS_COLOURS`
#: key its tint resolves against. Every member maps to its own value
#: EXCEPT :attr:`Sigil.RUNNING`, whose lifecycle-status string is
#: ``"in_progress"`` while its human-facing enum value is ``"running"``.
#: Kept private here so the COLOUR layer's public API stays unchanged.
_TINT_KEY: dict[Sigil, str] = {
    Sigil.PENDING: "pending",
    Sigil.CLAIMED: "claimed",
    Sigil.RUNNING: "in_progress",
    Sigil.CLOSED: "closed",
    Sigil.FAILED: "failed",
    Sigil.ABANDONED: "abandoned",
}


def _column(unicode_glyph: str, ascii_glyph: str, *, mode: str) -> str:
    """Return the ASCII or unicode glyph from a ``(unicode, ascii)`` pair.

    Selection is binary: ``mode == "ascii"`` returns the ASCII column;
    any other label returns the unicode column. There is no third state,
    so the helper stays robust to both unicode labels (``"unicode"`` and
    the legacy ``"braille"`` alias) without coupling to whichever name is
    current.

    Args:
        unicode_glyph: The unicode column glyph.
        ascii_glyph: The ASCII column glyph.
        mode: The App's resolved render-mode label.

    Returns:
        The ASCII glyph when *mode* is ``"ascii"``, else the unicode glyph.
    """
    return ascii_glyph if mode == ASCII_MODE else unicode_glyph


def glyph(sigil: Sigil, *, mode: str) -> str:
    """Return the lifecycle *sigil*'s glyph in the active render *mode*.

    Args:
        sigil: The lifecycle-state mark to render.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects
            the ASCII column; any other value (``"unicode"`` or the legacy
            ``"braille"`` alias) selects the unicode column.

    Returns:
        The single-cell glyph string for *sigil* in the resolved column.

    Raises:
        KeyError: If *sigil* is not a member of :class:`Sigil` (an enum
            that drifted past the :data:`_LIFECYCLE` table).
    """
    unicode_glyph, ascii_glyph = _LIFECYCLE[sigil]
    return _column(unicode_glyph, ascii_glyph, mode=mode)


def chrome(role: str, *, mode: str) -> str:
    """Return the chrome *role*'s glyph in the active render *mode*.

    Args:
        role: The chrome / action role -- one of ``"dispatch"`` /
            ``"gate"`` / ``"attention"`` / ``"harmony"`` / ``"overview"``
            / ``"runtime"`` / ``"check_on"`` / ``"check_off"``.
        mode: The App's resolved render-mode label -- ``"ascii"`` selects
            the ASCII column; any other value (``"unicode"`` or the legacy
            ``"braille"`` alias) selects the unicode column.

    Returns:
        The glyph string for *role* in the resolved column.

    Raises:
        KeyError: If *role* is not a known chrome role.
    """
    unicode_glyph, ascii_glyph = _CHROME[role]
    return _column(unicode_glyph, ascii_glyph, mode=mode)


def tint(sigil: Sigil) -> str | None:
    """Return the Wong status tint for *sigil*, or ``None`` when unmapped.

    Delegates to
    :func:`~eawf.surfaces.tui.widgets.status_tint.status_colour` so colour
    stays single-homed in the COLOUR layer. The :class:`Sigil` member is
    first mapped to its lifecycle-status string (via :data:`_TINT_KEY`,
    which resolves :attr:`Sigil.RUNNING` to the ``"in_progress"`` status
    key the COLOUR layer carries) and the tint is read off that string,
    so :func:`tint(Sigil.CLOSED) <tint>` returns the Wong closed green
    ``#009e73``.

    Args:
        sigil: The lifecycle-state mark whose tint to resolve.

    Returns:
        A concrete ``#rrggbb`` hex string, or ``None`` when the mapped
        status has no tint (the COLOUR layer drifted past its map).

    Raises:
        KeyError: If *sigil* is not a member of :class:`Sigil`.
    """
    status_key = _TINT_KEY[sigil]
    return status_colour(_StatusValue(status_key))


class _StatusValue:
    """A minimal ``.value``-bearing shim for :func:`status_colour`.

    :func:`~eawf.surfaces.tui.widgets.status_tint.status_colour` reads its
    argument's ``.value`` attribute (it expects a lifecycle-status enum
    member). Wrapping the bare status key in this shim lets :func:`tint`
    delegate to the COLOUR layer without changing its public API to accept
    a raw string.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


#: The warn-triangle glyph pair (the COLOUR-agnostic attention mark) the
#: extended resolver renders for a degraded-but-not-failed status (an audit
#: MINOR finding, a STALE agent session). It is the chrome ``attention``
#: mark reused as a STATE shape so a degraded status reads as
#: ATTENTION-shaped, not as the PENDING ring -- the correctness rule the
#: extended map enforces (warn -> triangle, never the not-yet-run ring).
_WARN_TRIANGLE: tuple[str, str] = _CHROME["attention"]

#: The follow-up badge a ``pass-with-followups`` verdict trails after its
#: CLOSED filled circle, so a clean pass and a pass-with-a-tail are
#: distinguishable at a glance without a second shape. The unicode column
#: is a superscript plus; the ASCII column is a bare ``+`` (which collides
#: with no lifecycle / bar / chrome glyph).
FOLLOWUP_BADGE: tuple[str, str] = ("\u207a", "+")  # U+207A superscript plus, ascii plus


@dataclass(frozen=True, slots=True)
class ResolvedSigil:
    """A status resolved to its ratified glyph, tint, and optional badge.

    The extended resolver (:func:`status_sigil`) returns this for every value
    of every TUI-render status enum so a consuming pane renders a real glyph
    -- never a bare ``.value`` word and never a ``?`` fallthrough. The two
    glyph columns mirror the :data:`_LIFECYCLE` / :data:`_CHROME` pairs: a
    consumer picks the column with :meth:`render` against the App's resolved
    render mode.

    Attributes:
        glyph_unicode: The unicode-column mark.
        glyph_ascii: The ASCII-column fallback mark (deconflicted off the
            bar glyphs).
        tint_hex: The concrete Wong ``#rrggbb`` tint, or ``None`` when the
            status carries no tint.
        badge: An optional trailing ``(unicode, ascii)`` badge pair (the
            follow-up badge a ``pass-with-followups`` verdict trails), or
            ``None`` for a bare single-glyph status.
    """

    glyph_unicode: str
    glyph_ascii: str
    tint_hex: str | None
    badge: tuple[str, str] | None = None

    def render(self, *, mode: str) -> str:
        """Return the resolved glyph (plus any badge) in the active *mode*.

        Args:
            mode: The App's resolved render-mode label -- ``"ascii"`` selects
                the ASCII column; any other value selects the unicode column.

        Returns:
            The single status glyph, with the follow-up badge appended when
            one is set, in the resolved column.
        """
        mark = _column(self.glyph_unicode, self.glyph_ascii, mode=mode)
        if self.badge is None:
            return mark
        return mark + _column(self.badge[0], self.badge[1], mode=mode)


def _from_sigil(sigil: Sigil, *, badge: tuple[str, str] | None = None) -> ResolvedSigil:
    """Build a :class:`ResolvedSigil` off a lifecycle :class:`Sigil` member.

    Args:
        sigil: The lifecycle shape whose glyph columns + tint back the result.
        badge: An optional trailing badge pair.

    Returns:
        A :class:`ResolvedSigil` carrying *sigil*'s two glyph columns, its
        Wong tint, and *badge*.
    """
    unicode_glyph, ascii_glyph = _LIFECYCLE[sigil]
    return ResolvedSigil(
        glyph_unicode=unicode_glyph,
        glyph_ascii=ascii_glyph,
        tint_hex=tint(sigil),
        badge=badge,
    )


def _warn() -> ResolvedSigil:
    """Build the warn-triangle :class:`ResolvedSigil` for a degraded status.

    Returns:
        The attention-triangle glyph pair tinted the Wong ``warn`` band hex
        -- shape-distinct from the PENDING ring so a degraded status never
        reads as a not-yet-run one.
    """
    return ResolvedSigil(
        glyph_unicode=_WARN_TRIANGLE[0],
        glyph_ascii=_WARN_TRIANGLE[1],
        tint_hex=BAND_HEX["warn"],
    )


#: A withheld :class:`ResolvedSigil` factory: the circled-slash ABANDONED
#: glyph carrying a caller-chosen tint. Used by the few withheld states whose
#: tint is the warn band (a BLOCKED verdict, a WAIVED outcome) rather than the
#: muted ``abandoned`` grey the lifecycle :attr:`Sigil.ABANDONED` carries.
def _withheld(*, tint_hex: str | None) -> ResolvedSigil:
    """Build a withheld :class:`ResolvedSigil` (the circled slash) with *tint_hex*.

    Args:
        tint_hex: The concrete Wong ``#rrggbb`` tint the withheld mark wears.

    Returns:
        A :class:`ResolvedSigil` carrying the ABANDONED circled-slash glyph
        columns and *tint_hex*.
    """
    unicode_glyph, ascii_glyph = _LIFECYCLE[Sigil.ABANDONED]
    return ResolvedSigil(glyph_unicode=unicode_glyph, glyph_ascii=ascii_glyph, tint_hex=tint_hex)


#: The sandbox-enforcement severity alphabet: a severity string ->
#: :class:`ResolvedSigil`. The spawn floor records an enforcement decision at
#: one of three severities (``block`` / ``warn`` / ``info``, the
#: ``EnforcementSeverity`` literal). A hard ``block`` (the floor REFUSED the
#: action -- an argv head, an egress host) wears the FAILED cross so a denial
#: reads as a hard stop; a ``warn`` / ``info`` (the floor degraded but let the
#: spawn continue -- a cwd-guard fallback, an env-scrub note) wears the warn
#: triangle, shape-distinct from the cross so a soft note never reads as a hard
#: deny. Both shapes are reused from the existing lifecycle / chrome alphabets,
#: so no new glyph is invented and the chrome-role count + ascii deconfliction
#: invariants stay intact.
_ENFORCEMENT_SEVERITY: dict[str, ResolvedSigil] = {
    "block": _from_sigil(Sigil.FAILED),
    "warn": _warn(),
    "info": _warn(),
}

#: The :class:`ResolvedSigil` a severity string outside the known alphabet
#: resolves to: the hard-deny cross. An enforcement row whose severity spelling
#: is novel still reads as a real glyph (never a bare word / fallthrough), and
#: an unrecognised severity defaults to the hard-deny mark so a denial is never
#: under-stated.
_ENFORCEMENT_FALLBACK: ResolvedSigil = _from_sigil(Sigil.FAILED)


def enforcement_sigil(severity: str) -> ResolvedSigil:
    """Return the :class:`ResolvedSigil` for a sandbox-enforcement *severity*.

    The single resolver the sandbox-events timeline pane calls to turn a
    persisted enforcement severity (``block`` / ``warn`` / ``info``, the
    ``EnforcementSeverity`` literal the floor writes) into its severity sigil:
    a hard ``block`` deny wears the FAILED cross (a refusal reads as a hard
    stop), a ``warn`` / ``info`` degraded-but-continued note wears the warn
    triangle (shape-distinct so a soft note never reads as a hard deny). An
    unrecognised severity defaults to the hard-deny cross
    (:data:`_ENFORCEMENT_FALLBACK`) so a row always renders a real glyph and a
    denial is never under-stated -- the resolver is total, never raising.

    Args:
        severity: The enforcement severity string read off the persisted
            event row.

    Returns:
        The :class:`ResolvedSigil` for *severity* (the hard-deny cross for an
        unknown / ``block`` severity, the warn triangle for ``warn`` /
        ``info``).
    """
    return _ENFORCEMENT_SEVERITY.get(severity, _ENFORCEMENT_FALLBACK)


#: The ratified extended-status map, keyed by enum CLASS then by member, so the
#: cross-class StrEnum value collisions (e.g. ``AgentReportVerdict.BLOCKED`` and
#: ``OpenQuestionStatus.BLOCKED`` both ``== "blocked"``) stay distinct: a flat
#: ``dict[StrEnum, ...]`` would silently merge them because two members with the
#: same value hash + compare equal. Built once at import so the resolver is a
#: constant-time lookup and the sigil-totality gate can assert each inner map
#: covers ``list(EnumCls)``. The correctness rule the table enforces: a degraded
#: state (audit MINOR, agent STALE, a blocking question) maps to the warn
#: TRIANGLE, NEVER the PENDING ring -- a degraded state must not be
#: shape-identical to a not-yet-run one. Withheld / terminal-not-closed states
#: (WaveStatus / IterStatus ABANDONED, PhaseStatus ARCHIVED, a BLOCKED verdict,
#: a WAIVED outcome, a DEFERRED backlog item, a SUPERSEDED / DROPPED row) map to
#: the ABANDONED circled-slash; hard failures (AuditVerdict MAJOR, a FAILED
#: session, a REFUTED claim) map to the FAILED cross.
_EXTENDED: dict[type, dict[object, ResolvedSigil]] = {
    # Wave lifecycle -- the five live states reuse their lifecycle shape; the
    # withheld ABANDONED terminal wears the muted circled slash.
    WaveStatus: {
        WaveStatus.PENDING: _from_sigil(Sigil.PENDING),
        WaveStatus.CLAIMED: _from_sigil(Sigil.CLAIMED),
        WaveStatus.IN_PROGRESS: _from_sigil(Sigil.RUNNING),
        WaveStatus.CLOSED: _from_sigil(Sigil.CLOSED),
        WaveStatus.FAILED: _from_sigil(Sigil.FAILED),
        WaveStatus.ABANDONED: _from_sigil(Sigil.ABANDONED),
    },
    # Iter lifecycle.
    IterStatus: {
        IterStatus.PLANNED: _from_sigil(Sigil.PENDING),
        IterStatus.ACTIVE: _from_sigil(Sigil.RUNNING),
        IterStatus.CLOSED: _from_sigil(Sigil.CLOSED),
        IterStatus.ABANDONED: _from_sigil(Sigil.ABANDONED),
    },
    # Phase lifecycle -- ARCHIVED is the phase-level withheld terminal.
    PhaseStatus: {
        PhaseStatus.PLANNED: _from_sigil(Sigil.PENDING),
        PhaseStatus.ACTIVE: _from_sigil(Sigil.RUNNING),
        PhaseStatus.CLOSED: _from_sigil(Sigil.CLOSED),
        PhaseStatus.ARCHIVED: _from_sigil(Sigil.ABANDONED),
    },
    # Agent-report verdict -- pass-with-followups wears the CLOSED circle plus a
    # follow-up badge so it is distinguishable from a clean pass; BLOCKED is a
    # warn-tinted withheld circled slash (withheld, not a clean pass / hard
    # fail).
    AgentReportVerdict: {
        AgentReportVerdict.PASS: _from_sigil(Sigil.CLOSED),
        AgentReportVerdict.PASS_WITH_FOLLOWUPS: _from_sigil(Sigil.CLOSED, badge=FOLLOWUP_BADGE),
        AgentReportVerdict.FAIL: _from_sigil(Sigil.FAILED),
        AgentReportVerdict.BLOCKED: _withheld(tint_hex=BAND_HEX["warn"]),
    },
    # Agent-session status -- STALE is degraded (warn triangle), CHECKPOINTED a
    # paused half-circle, FAILED the cross, CLOSED the filled circle.
    AgentSessionStatus: {
        AgentSessionStatus.ACTIVE: _from_sigil(Sigil.RUNNING),
        AgentSessionStatus.CHECKPOINTED: _from_sigil(Sigil.CLAIMED),
        AgentSessionStatus.CLOSED: _from_sigil(Sigil.CLOSED),
        AgentSessionStatus.STALE: _warn(),
        AgentSessionStatus.FAILED: _from_sigil(Sigil.FAILED),
    },
    # Audit verdict -- PASS clears, MINOR is the warn triangle, MAJOR fails.
    AuditVerdict: {
        AuditVerdict.PASS: _from_sigil(Sigil.CLOSED),
        AuditVerdict.MINOR: _warn(),
        AuditVerdict.MAJOR: _from_sigil(Sigil.FAILED),
    },
    # Outcome status -- WAIVED is a warn-tinted withheld terminal (circled slash).
    OutcomeStatus: {
        OutcomeStatus.PENDING: _from_sigil(Sigil.PENDING),
        OutcomeStatus.MET: _from_sigil(Sigil.CLOSED),
        OutcomeStatus.MISSED: _from_sigil(Sigil.FAILED),
        OutcomeStatus.WAIVED: _withheld(tint_hex=BAND_HEX["warn"]),
    },
    # Backlog status -- OPEN ring, IN_PROGRESS diamond, DEFERRED withheld,
    # CLOSED filled circle.
    BacklogStatus: {
        BacklogStatus.OPEN: _from_sigil(Sigil.PENDING),
        BacklogStatus.IN_PROGRESS: _from_sigil(Sigil.RUNNING),
        BacklogStatus.DEFERRED: _from_sigil(Sigil.ABANDONED),
        BacklogStatus.CLOSED: _from_sigil(Sigil.CLOSED),
    },
    # Claim status -- OPEN ring, SUPPORTED filled circle, REFUTED cross,
    # SUPERSEDED the muted withheld slash (pruned-but-kept-for-traceability).
    ClaimStatus: {
        ClaimStatus.OPEN: _from_sigil(Sigil.PENDING),
        ClaimStatus.SUPPORTED: _from_sigil(Sigil.CLOSED),
        ClaimStatus.REFUTED: _from_sigil(Sigil.FAILED),
        ClaimStatus.SUPERSEDED: _from_sigil(Sigil.ABANDONED),
    },
    # Open-question status -- OPEN ring, ANSWERED filled circle, BLOCKED the
    # warn triangle (it gates work), DROPPED the muted withheld slash.
    OpenQuestionStatus: {
        OpenQuestionStatus.OPEN: _from_sigil(Sigil.PENDING),
        OpenQuestionStatus.ANSWERED: _from_sigil(Sigil.CLOSED),
        OpenQuestionStatus.BLOCKED: _warn(),
        OpenQuestionStatus.DROPPED: _from_sigil(Sigil.ABANDONED),
    },
}


def status_sigil(status: object) -> ResolvedSigil:
    """Return the ratified :class:`ResolvedSigil` for an extended *status*.

    The single resolver every reskin pane calls to turn a status enum member
    (a wave / iter / phase status, an agent-report verdict, an agent-session
    status, an audit verdict, an outcome status, a backlog status, a claim
    status, an open-question status) into its glyph + tint + optional badge,
    so no pane ever prints a bare ``.value`` word or a ``?`` fallthrough. The
    member is looked up by its enum CLASS first, so two members of different
    enums that share a string value (e.g. the two ``BLOCKED`` members) resolve
    to their own ratified glyph rather than colliding.

    Args:
        status: A member of one of the TUI-render status enums the
            :data:`_EXTENDED` table covers.

    Returns:
        The :class:`ResolvedSigil` mapped for *status*.

    Raises:
        KeyError: If *status*'s class is not a covered enum, or its class is
            covered but the member drifted past the inner map. Surfacing the
            miss is deliberate -- the sigil-totality gate asserts each inner
            map covers ``list(EnumCls)`` so a drift fails CI rather than
            silently rendering a fallthrough.
    """
    return _EXTENDED[type(status)][status]


__all__ = [
    "ASCII_MODE",
    "FOLLOWUP_BADGE",
    "ResolvedSigil",
    "Sigil",
    "chrome",
    "enforcement_sigil",
    "glyph",
    "status_sigil",
    "tint",
]
