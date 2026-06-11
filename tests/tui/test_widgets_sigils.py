"""Unit tests for the SHAPE-layer ``widgets/sigils.py`` helper.

Covers the two glyph columns (unicode vs ascii) across all five
lifecycle sigils and every chrome role, the COLOUR-layer delegation
(``tint`` resolves the Wong status hex via ``status_colour``, including
the ``RUNNING -> in_progress`` key remap), the binary mode selection (any
non-``"ascii"`` label resolves the unicode column, decoupling the helper
from the not-yet-landed render-mode rename), and the deconfliction
regression invariant (the ascii sigil alphabet shares no character with
the EU / burn bar glyphs).

The chrome-role count + coverage pin asserts the ratified fixtures below
against the SOURCE ``_CHROME`` table (imported from the module under test),
not a hand-kept count literal: an additive drift -- a new chrome role landed
in source without ratifying its glyphs here -- fails the pin until the
fixtures gain the role, so the role inventory cannot grow invisibly.

The module is PURE -- no Textual primitive, no daemon -- so these tests
mount nothing and need no lock.
"""

from __future__ import annotations

import pytest

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
from eawf.surfaces.tui.app import resolve_render_mode
from eawf.surfaces.tui.theme import WONG_VARIABLES
from eawf.surfaces.tui.widgets.eu_bar import GLYPH_EMPTY, GLYPH_FULL
from eawf.surfaces.tui.widgets.sigils import (
    _CHROME,
    FOLLOWUP_BADGE,
    Sigil,
    chrome,
    glyph,
    status_sigil,
    tint,
)
from eawf.surfaces.tui.widgets.status_tint import BAND_HEX

# The expected rendered glyphs, written as the actual code points so the
# test pins the real marks (the source uses \uXXXX escapes to stay ASCII).
_LIFECYCLE_UNICODE: dict[Sigil, str] = {
    Sigil.PENDING: "\u25cc",  # hollow dotted circle
    Sigil.CLAIMED: "\u25d0",  # half-filled circle
    Sigil.RUNNING: "\u25c6",  # filled diamond
    Sigil.CLOSED: "\u25cf",  # filled circle
    Sigil.FAILED: "\u2715",  # multiplication x
    Sigil.ABANDONED: "\u2298",  # circled division slash (withheld)
}
_LIFECYCLE_ASCII: dict[Sigil, str] = {
    Sigil.PENDING: "o",
    Sigil.CLAIMED: "(",
    Sigil.RUNNING: "*",
    Sigil.CLOSED: "@",
    Sigil.FAILED: "x",
    Sigil.ABANDONED: "%",
}

# The ratified chrome-role glyphs. ``brand`` is the leading fisheye wordmark
# stand-in (the terminal-renderable Seal SVG fallback); it is a real shipped
# chrome role, so it is pinned here alongside the eight action roles. The
# count + coverage pin (``test_chrome_keys_match_source``) asserts these keys
# equal the SOURCE ``_CHROME`` table, so a future role lands red until ratified.
_CHROME_UNICODE: dict[str, str] = {
    "dispatch": "\u276f",
    "gate": "\u2394",
    "attention": "\u25b3",
    "harmony": "\u2248",
    "overview": "\u2261",
    "runtime": "$",
    "check_on": "\u25a3",
    "check_off": "\u25a2",
    "brand": "\u25c9",
    "criteria": "\u25b8",
    "cost": "\u00a4",
    "history": "\u21ba",
}
_CHROME_ASCII: dict[str, str] = {
    "dispatch": ">",
    "gate": "[]",
    "attention": "!",
    "harmony": "~",
    "overview": "=",
    "runtime": "$",
    "check_on": "[x]",
    "check_off": "[ ]",
    "brand": "*",
    "criteria": ">",
    "cost": "$",
    "history": "<",
}


# --------------------------------------------------------------------------
# Criterion 1 -- lifecycle glyph correctness across all five states + modes
# --------------------------------------------------------------------------


def test_glyph_closed_unicode_is_filled_circle() -> None:
    assert glyph(Sigil.CLOSED, mode="unicode") == "\u25cf"


def test_glyph_closed_ascii_is_at_sign() -> None:
    # Deconflicted off the bar full glyph '#': closed sigil is '@'.
    assert glyph(Sigil.CLOSED, mode="ascii") == "@"


@pytest.mark.parametrize("sigil", list(Sigil))
def test_glyph_unicode_column_for_every_state(sigil: Sigil) -> None:
    assert glyph(sigil, mode="unicode") == _LIFECYCLE_UNICODE[sigil]


@pytest.mark.parametrize("sigil", list(Sigil))
def test_glyph_ascii_column_for_every_state(sigil: Sigil) -> None:
    assert glyph(sigil, mode="ascii") == _LIFECYCLE_ASCII[sigil]


def test_glyph_pending_ascii_is_o_not_dash() -> None:
    # Deconflicted off the bar empty glyph '-': pending sigil is 'o'.
    assert glyph(Sigil.PENDING, mode="ascii") == "o"
    assert glyph(Sigil.PENDING, mode="ascii") != GLYPH_EMPTY


# --------------------------------------------------------------------------
# Criterion 2 -- chrome glyph correctness for every role + modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(_CHROME_UNICODE))
def test_chrome_unicode_column_for_every_role(role: str) -> None:
    assert chrome(role, mode="unicode") == _CHROME_UNICODE[role]


@pytest.mark.parametrize("role", list(_CHROME_ASCII))
def test_chrome_ascii_column_for_every_role(role: str) -> None:
    assert chrome(role, mode="ascii") == _CHROME_ASCII[role]


def test_chrome_keys_match_source() -> None:
    # Pin the role inventory against the SOURCE ``_CHROME`` table, not a count
    # literal: a chrome role added to source without ratifying its glyphs in
    # the fixtures above (additive drift) fails this assertion until the
    # fixtures gain the role, so the inventory cannot grow invisibly. The two
    # ratified fixtures must also stay in lockstep with each other.
    source_roles = set(_CHROME)
    assert set(_CHROME_UNICODE) == source_roles
    assert set(_CHROME_ASCII) == source_roles


def test_chrome_resolves_the_detail_tab_marker_roles() -> None:
    """The criteria / cost / history tab markers resolve through chrome roles.

    These three markers were folded out of the detail overlay into the single
    chrome home, so the detail tab chassis resolves every tab glyph through one
    vocabulary (criterion: tab markers resolve through sigils.py chrome roles).
    """
    assert chrome("criteria", mode="unicode") == "▸"
    assert chrome("criteria", mode="ascii") == ">"
    # The runtime role already owns the ``$`` mark, so the cost tab carries the
    # generic currency sign in unicode and a plain dollar in ascii.
    assert chrome("cost", mode="unicode") == "¤"
    assert chrome("cost", mode="ascii") == "$"
    assert chrome("history", mode="unicode") == "↺"
    assert chrome("history", mode="ascii") == "<"


def test_chrome_unknown_role_raises_key_error() -> None:
    with pytest.raises(KeyError):
        chrome("no-such-role", mode="unicode")


# --------------------------------------------------------------------------
# Criterion 3 -- tint delegates to the COLOUR layer (single-homed hue)
# --------------------------------------------------------------------------


def test_tint_closed_is_wong_closed_green() -> None:
    assert tint(Sigil.CLOSED) == "#009e73"


def test_tint_running_resolves_in_progress_key() -> None:
    # Sigil.RUNNING.value is "running" but the lifecycle status string is
    # "in_progress"; tint must remap so it does not fall through to None.
    assert tint(Sigil.RUNNING) == WONG_VARIABLES["status-in-progress"]


@pytest.mark.parametrize(
    ("sigil", "expected"),
    [
        (Sigil.PENDING, WONG_VARIABLES["status-pending"]),
        (Sigil.CLAIMED, WONG_VARIABLES["status-claimed"]),
        (Sigil.RUNNING, WONG_VARIABLES["status-in-progress"]),
        (Sigil.CLOSED, WONG_VARIABLES["status-closed"]),
        (Sigil.FAILED, WONG_VARIABLES["status-failed"]),
    ],
)
def test_tint_resolves_for_every_state(sigil: Sigil, expected: str) -> None:
    assert tint(sigil) == expected


def test_tint_never_none_for_any_sigil() -> None:
    # Every lifecycle sigil resolves a concrete tint (no row goes untinted).
    for sigil in Sigil:
        assert tint(sigil) is not None


# --------------------------------------------------------------------------
# Criterion 4 -- mode binding: any non-ascii label selects the unicode
# column; a non-TTY harness resolves the ascii column.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("unicode_label", ["unicode", "braille"])
def test_glyph_any_non_ascii_label_selects_unicode(unicode_label: str) -> None:
    # The legacy "braille" alias and the canonical "unicode" both resolve
    # the unicode column -- the helper is decoupled from the rename.
    assert glyph(Sigil.CLOSED, mode=unicode_label) == "\u25cf"
    assert chrome("dispatch", mode=unicode_label) == "\u276f"


def test_glyph_unrecognised_label_falls_to_unicode() -> None:
    # There is no third state: any label that is not exactly "ascii" maps to
    # the unicode column (binary selection).
    assert glyph(Sigil.CLOSED, mode="garbage") == "\u25cf"


def test_non_tty_harness_resolves_ascii_column() -> None:
    # When the app resolves "ascii" (the non-TTY / CI / Braille-less path:
    # resolve_render_mode("ascii", ...) or a failed coverage probe), the
    # helper gives the ascii column.
    mode = resolve_render_mode("ascii", braille_ok=False)
    assert mode == "ascii"
    assert glyph(Sigil.CLOSED, mode=mode) == "@"
    assert chrome("gate", mode=mode) == "[]"

    # The auto policy with a failed coverage probe (a non-TTY / Braille-less
    # terminal) also resolves ascii, and the helper honours it.
    auto_mode = resolve_render_mode("auto", braille_ok=False)
    assert auto_mode == "ascii"
    assert glyph(Sigil.PENDING, mode=auto_mode) == "o"


# --------------------------------------------------------------------------
# Criterion 5 -- deconfliction regression: ascii sigil alphabet shares no
# character with the EU / burn bar glyphs.
# --------------------------------------------------------------------------


def test_ascii_sigil_alphabet_disjoint_from_bar_glyphs() -> None:
    # The whole ascii sigil alphabet -- lifecycle 'o ( * @ x' plus chrome
    # '> [] ! ~ = $ [x] [ ]' -- must not contain the bar's '#' or '-' so a
    # row that renders a sigil beside an inline bar reads unambiguously in
    # ascii mode.
    ascii_chars: set[str] = set()
    for sigil in Sigil:
        ascii_chars.update(glyph(sigil, mode="ascii"))
    for role in _CHROME_ASCII:
        ascii_chars.update(chrome(role, mode="ascii"))

    bar_chars = set(GLYPH_FULL) | set(GLYPH_EMPTY)
    assert ascii_chars & bar_chars == set()


def test_lifecycle_ascii_chars_exclude_bar_full_and_empty() -> None:
    # Pin the two specific deconflictions the contract calls out: closed is
    # not the bar full glyph, pending is not the bar empty glyph.
    lifecycle_ascii = {glyph(sigil, mode="ascii") for sigil in Sigil}
    assert GLYPH_FULL not in lifecycle_ascii
    assert GLYPH_EMPTY not in lifecycle_ascii


# --------------------------------------------------------------------------
# Criterion 6 -- the 6th ABANDONED sigil + its deconflicted ascii char
# --------------------------------------------------------------------------


def test_abandoned_is_sixth_lifecycle_sigil() -> None:
    # The extended alphabet adds exactly one lifecycle member: ABANDONED.
    assert Sigil.ABANDONED in Sigil
    assert len(list(Sigil)) == 6


def test_abandoned_unicode_is_circled_division_slash() -> None:
    assert glyph(Sigil.ABANDONED, mode="unicode") == "⊘"


def test_abandoned_ascii_is_percent() -> None:
    # '%' is chosen because it collides with NEITHER the other lifecycle
    # sigils ('o ( * @ x') NOR the bar glyphs ('# -') NOR the chrome ascii.
    assert glyph(Sigil.ABANDONED, mode="ascii") == "%"


def test_abandoned_ascii_deconflicts_from_every_other_alphabet() -> None:
    # The new ascii char must not collide with any other lifecycle sigil, the
    # bar glyphs, or the chrome ascii -- a withheld row beside a bar or a
    # chrome mark must read unambiguously.
    abandoned = glyph(Sigil.ABANDONED, mode="ascii")
    other_lifecycle = {glyph(s, mode="ascii") for s in Sigil if s is not Sigil.ABANDONED}
    chrome_ascii = {chrome(role, mode="ascii") for role in _CHROME_ASCII}
    bar_chars = {GLYPH_FULL, GLYPH_EMPTY}
    assert abandoned not in other_lifecycle
    assert abandoned not in chrome_ascii
    assert abandoned not in bar_chars


def test_abandoned_tint_is_muted_grey() -> None:
    # ABANDONED wears the muted 'abandoned' grey (it recedes, not alarms).
    assert tint(Sigil.ABANDONED) == WONG_VARIABLES["status-pending"]


def test_tint_still_never_none_for_any_sigil() -> None:
    # The extended alphabet must keep tint() total over every member.
    for sigil in Sigil:
        assert tint(sigil) is not None


# --------------------------------------------------------------------------
# Criterion 7 -- the extended-status resolver: every status -> ratified glyph
# --------------------------------------------------------------------------

#: The ratified base glyph (unicode) every extended status must resolve to.
#: Verbatim from the W28 contract -- the column the totality gate also pins.
#: A LIST of pairs (not a dict) because the cross-class StrEnum value collisions
#: (AgentReportVerdict.BLOCKED and OpenQuestionStatus.BLOCKED both == "blocked")
#: would silently merge into one dict key -- exactly the hazard the class-keyed
#: resolver fixes, so the test must not re-introduce it.
_EXTENDED_EXPECTED: list[tuple[object, str]] = [
    (WaveStatus.PENDING, "◌"),
    (WaveStatus.CLAIMED, "◐"),
    (WaveStatus.IN_PROGRESS, "◆"),
    (WaveStatus.CLOSED, "●"),
    (WaveStatus.FAILED, "✕"),
    (WaveStatus.ABANDONED, "⊘"),
    (IterStatus.PLANNED, "◌"),
    (IterStatus.ACTIVE, "◆"),
    (IterStatus.CLOSED, "●"),
    (IterStatus.ABANDONED, "⊘"),
    (PhaseStatus.PLANNED, "◌"),
    (PhaseStatus.ACTIVE, "◆"),
    (PhaseStatus.CLOSED, "●"),
    (PhaseStatus.ARCHIVED, "⊘"),
    (AgentReportVerdict.PASS, "●"),
    (AgentReportVerdict.PASS_WITH_FOLLOWUPS, "●"),
    (AgentReportVerdict.FAIL, "✕"),
    (AgentReportVerdict.BLOCKED, "⊘"),
    (AgentSessionStatus.ACTIVE, "◆"),
    (AgentSessionStatus.CHECKPOINTED, "◐"),
    (AgentSessionStatus.CLOSED, "●"),
    (AgentSessionStatus.STALE, "△"),
    (AgentSessionStatus.FAILED, "✕"),
    (AuditVerdict.PASS, "●"),
    (AuditVerdict.MINOR, "△"),
    (AuditVerdict.MAJOR, "✕"),
    (OutcomeStatus.PENDING, "◌"),
    (OutcomeStatus.MET, "●"),
    (OutcomeStatus.MISSED, "✕"),
    (OutcomeStatus.WAIVED, "⊘"),
    (BacklogStatus.OPEN, "◌"),
    (BacklogStatus.IN_PROGRESS, "◆"),
    (BacklogStatus.DEFERRED, "⊘"),
    (BacklogStatus.CLOSED, "●"),
    (ClaimStatus.OPEN, "◌"),
    (ClaimStatus.SUPPORTED, "●"),
    (ClaimStatus.REFUTED, "✕"),
    (ClaimStatus.SUPERSEDED, "⊘"),
    (OpenQuestionStatus.OPEN, "◌"),
    (OpenQuestionStatus.ANSWERED, "●"),
    (OpenQuestionStatus.BLOCKED, "△"),
    (OpenQuestionStatus.DROPPED, "⊘"),
]


@pytest.mark.parametrize(("status", "expected"), _EXTENDED_EXPECTED)
def test_status_sigil_resolves_to_ratified_glyph(status: object, expected: str) -> None:
    # Every extended status resolves to its ratified unicode glyph -- never the
    # bare .value word, never a '?' fallthrough.
    resolved = status_sigil(status)
    assert resolved.glyph_unicode == expected
    assert resolved.glyph_unicode != status.value  # type: ignore[attr-defined]
    assert resolved.glyph_unicode != "?"


def test_blocked_verdict_and_blocked_question_do_not_collide() -> None:
    # AgentReportVerdict.BLOCKED and OpenQuestionStatus.BLOCKED both == "blocked"
    # as StrEnum values; the class-keyed resolver must give them distinct glyphs
    # (withheld circled-slash vs warn triangle), not merge them.
    verdict = status_sigil(AgentReportVerdict.BLOCKED)
    question = status_sigil(OpenQuestionStatus.BLOCKED)
    assert verdict.glyph_unicode == "⊘"
    assert question.glyph_unicode == "△"
    assert verdict.glyph_unicode != question.glyph_unicode


def test_pass_with_followups_trails_a_followup_badge() -> None:
    # A pass-with-a-tail is the CLOSED filled circle plus the follow-up badge,
    # so it is distinguishable from a clean pass without a second shape.
    clean = status_sigil(AgentReportVerdict.PASS)
    tailed = status_sigil(AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    assert clean.badge is None
    assert tailed.badge == FOLLOWUP_BADGE
    assert tailed.render(mode="unicode") == "●" + FOLLOWUP_BADGE[0]
    assert tailed.render(mode="ascii") == "@" + FOLLOWUP_BADGE[1]
    # The follow-up badge must not collide with the bar glyphs.
    assert FOLLOWUP_BADGE[1] not in {GLYPH_FULL, GLYPH_EMPTY}


@pytest.mark.parametrize(
    "warn_status",
    [AuditVerdict.MINOR, AgentSessionStatus.STALE, OpenQuestionStatus.BLOCKED],
)
def test_warn_status_is_the_triangle_never_the_pending_ring(warn_status: object) -> None:
    # The correctness rule: a degraded state maps to the warn TRIANGLE, never
    # the PENDING ring -- a degraded state must not be shape-identical to a
    # not-yet-run one.
    resolved = status_sigil(warn_status)
    assert resolved.glyph_unicode == "△"
    assert resolved.glyph_unicode != status_sigil(WaveStatus.PENDING).glyph_unicode
    assert resolved.tint_hex == BAND_HEX["warn"]


def test_minor_is_warn_triangle_major_is_failed_cross() -> None:
    # Pin the audit-verdict split the contract calls out explicitly.
    assert status_sigil(AuditVerdict.MINOR).glyph_unicode == "△"
    assert status_sigil(AuditVerdict.MAJOR).glyph_unicode == "✕"


def test_abandoned_and_archived_map_to_the_sixth_sigil() -> None:
    # ABANDONED (wave/iter) + ARCHIVED (phase) all map to the withheld glyph.
    assert status_sigil(WaveStatus.ABANDONED).glyph_unicode == "⊘"
    assert status_sigil(IterStatus.ABANDONED).glyph_unicode == "⊘"
    assert status_sigil(PhaseStatus.ARCHIVED).glyph_unicode == "⊘"


def test_resolved_sigil_render_picks_the_active_column() -> None:
    # render() is binary on mode, mirroring glyph()/chrome().
    resolved = status_sigil(WaveStatus.ABANDONED)
    assert resolved.render(mode="unicode") == "⊘"
    assert resolved.render(mode="ascii") == "%"
    assert resolved.render(mode="braille") == "⊘"  # any non-ascii label -> unicode


def test_status_sigil_unmapped_member_raises_key_error() -> None:
    # A status enum the resolver does not cover surfaces a KeyError rather than
    # silently rendering a fallthrough (no idle contract).
    with pytest.raises(KeyError):
        status_sigil(WaveStatus)  # the class itself is not a mapped member
