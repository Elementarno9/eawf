"""Property tests for the JSON ⇄ markdown envelope round-trip.

The contract is two-fold:

- ``from_markdown(to_markdown(env)) == env`` — value equality.
- ``to_markdown(env) == to_markdown(from_markdown(to_markdown(env)))``
  — byte-stable idempotence (round-trip never drifts after the first
  emit).

Phase 4 W01 narrowed the header/footer to typed Pydantic models, so
this property test composes Hypothesis strategies for the typed shape:
:class:`EnvelopeHeader` (frozen ``skill``/``status`` literals, URN-ish
``scope`` and ``session`` strings, datetime pair) and
:class:`EnvelopeFooter` (six list fields plus the optional
``repair_commands``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.render.envelope import (
    EnvelopeFooter,
    EnvelopeHeader,
    EnvelopeStatus,
    EnvelopeWarning,
    InstrumentStatus,
    OutputEnvelope,
    SkillName,
    from_markdown,
    to_markdown,
)

_skill_names: tuple[SkillName, ...] = (
    "/research",
    "/prep",
    "/audit",
    "/ship",
    "/review",
    "/polish",
    "/init",
    "/roadmap",
    "/differentiate",
    "/flow",
)
_statuses: tuple[EnvelopeStatus, ...] = ("ok", "needs_user", "blocked", "failed", "partial")
_instrument_statuses: tuple[InstrumentStatus, ...] = ("ok", "missing", "degraded")

# URN-shaped scope/session keep the strings recognisable but bounded.
# YAML-safe alphabet: letters + digits + dash + underscore.
_URN_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"  # pragma: allowlist secret
)
_scopes = st.text(
    alphabet=_URN_ALPHABET,
    min_size=1,
    max_size=12,
).map(lambda s: f"urn:eawf:v1:state:{s}")
_sessions = st.text(
    alphabet=_URN_ALPHABET,
    min_size=1,
    max_size=12,
).map(lambda s: f"urn:eawf:v1:store:QR/sessions/SES-{s}")
_started_at = st.datetimes(
    min_value=datetime(2026, 1, 1).replace(tzinfo=None),
    max_value=datetime(2027, 1, 1).replace(tzinfo=None),
).map(lambda dt: dt.replace(tzinfo=UTC))
_durations = st.integers(min_value=0, max_value=86_400).map(lambda s: timedelta(seconds=s))

_INSTRUMENT_KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyz_"  # pragma: allowlist secret
_instrument_keys = st.text(
    alphabet=_INSTRUMENT_KEY_ALPHABET,
    min_size=1,
    max_size=8,
)
_instrument_probe = st.dictionaries(
    keys=_instrument_keys,
    values=st.sampled_from(_instrument_statuses),
    max_size=4,
)


@st.composite
def envelope_headers(draw: st.DrawFn) -> EnvelopeHeader:
    """Build a typed :class:`EnvelopeHeader` with monotonic timestamps."""
    started = draw(_started_at)
    finished = started + draw(_durations)
    return EnvelopeHeader(
        skill=draw(st.sampled_from(_skill_names)),
        scope_id=draw(_scopes),
        session=draw(_sessions),
        started_at=started,
        finished_at=finished,
        status=draw(st.sampled_from(_statuses)),
        instrument_probe=draw(_instrument_probe),
    )


_string_lists = st.lists(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:",
        min_size=1,
        max_size=10,
    ),
    max_size=4,
)


_warnings = st.lists(
    st.builds(
        EnvelopeWarning,
        code=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12),
        detail=st.text(
            alphabet=st.characters(
                whitelist_categories=("Ll", "Lu", "Nd"),
                whitelist_characters=" _-.",
                min_codepoint=32,
                max_codepoint=126,
            ),
            max_size=20,
        ),
    ),
    max_size=3,
)


@st.composite
def envelope_footers(draw: st.DrawFn) -> EnvelopeFooter:
    """Build a typed :class:`EnvelopeFooter` with bounded list sizes."""
    repair = draw(st.one_of(st.none(), _string_lists))
    return EnvelopeFooter(
        persisted_artifacts=draw(_string_lists),
        persisted_store_records=draw(_string_lists),
        state_mutations=draw(_string_lists),
        evidence_refs=draw(_string_lists),
        next_valid_actions=draw(_string_lists),
        warnings=draw(_warnings),
        repair_commands=repair,
    )


# The body must not embed the closing footer marker; otherwise a
# Hypothesis-generated body could shadow the real footer comment.
# Restricting to printable-ASCII keeps yaml.safe_dump deterministic and
# blacklist filtering keeps the parser unambiguous.
body_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd", "Po", "Pc"),
        whitelist_characters=" \n\t.,!?",
        blacklist_characters="<>",
        min_codepoint=32,
        max_codepoint=126,
    ),
    max_size=80,
).filter(lambda s: "<!-- eawf:footer" not in s and "-->" not in s)


@st.composite
def envelopes(draw: st.DrawFn) -> OutputEnvelope:
    """Build a typed :class:`OutputEnvelope` with a string body."""
    return OutputEnvelope(
        header=draw(envelope_headers()),
        body=draw(body_text),
        footer=draw(envelope_footers()),
    )


@given(env=envelopes())
@settings(max_examples=150, deadline=None)
def test_envelope_roundtrip_eq(env: OutputEnvelope) -> None:
    """``from_markdown(to_markdown(env)) == env`` for any valid typed envelope."""
    md = to_markdown(env)
    parsed = from_markdown(md)
    assert parsed.header == env.header
    assert parsed.body == env.body
    assert parsed.footer == env.footer
    assert parsed == env


@given(env=envelopes())
@settings(max_examples=150, deadline=None)
def test_envelope_roundtrip_byte_stable(env: OutputEnvelope) -> None:
    """Two consecutive emits are byte-identical (no drift)."""
    md1 = to_markdown(env)
    md2 = to_markdown(from_markdown(md1))
    assert md1 == md2
