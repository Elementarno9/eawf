"""Property tests for the typed-envelope JSON ⇄ markdown round-trip.

This is the W01-mandated typed counterpart to
``tests/property/test_render_envelope_roundtrip.py:28``. It composes
Hypothesis strategies for both the string-bodied and dict-bodied
envelope variants and asserts both contracts:

- ``from_markdown(to_markdown(env)) == env`` for every typed envelope.
- ``to_markdown(from_markdown(to_markdown(env))) == to_markdown(env)``
  for byte-stable idempotence.

Coverage: header (10 frozen skills x 5 frozen statuses x 3 instrument
statuses x URN-shaped scope/session x monotonic timestamps), footer
(six list fields plus optional ``repair_commands``), body (string OR
typed-dict body via the bodies module).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.surfaces.render.envelope import (
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

# YAML-safe alphabet: letters + digits + dash + underscore.
_URN_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"  # pragma: allowlist secret
)
_scopes = st.text(
    alphabet=_URN_ALPHABET,
    min_size=1,
    max_size=10,
).map(lambda s: f"urn:eawf:v1:state:{s}")
_sessions = st.text(
    alphabet=_URN_ALPHABET,
    min_size=1,
    max_size=10,
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


_warnings_strategy = st.lists(
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
    repair = draw(st.one_of(st.none(), _string_lists))
    return EnvelopeFooter(
        persisted_artifacts=draw(_string_lists),
        persisted_store_records=draw(_string_lists),
        state_mutations=draw(_string_lists),
        evidence_refs=draw(_string_lists),
        next_valid_actions=draw(_string_lists),
        warnings=draw(_warnings_strategy),
        repair_commands=repair,
    )


# ----- Body strategies -------------------------------------------------------

# String body. Must not contain the closing footer/body markers.
_string_body = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd", "Po", "Pc"),
        whitelist_characters=" \n\t.,!?",
        blacklist_characters="<>",
        min_codepoint=32,
        max_codepoint=126,
    ),
    max_size=80,
).filter(lambda s: "<!-- eawf:footer" not in s and "<!-- eawf:body" not in s and "-->" not in s)


_YAML_SAFE_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"  # pragma: allowlist secret
)
_yaml_safe_keys = st.text(
    alphabet=_YAML_SAFE_ALPHABET,
    min_size=1,
    max_size=12,
)
_yaml_safe_values = st.one_of(
    st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Lu", "Nd"),
            whitelist_characters=" _-.",
            min_codepoint=32,
            max_codepoint=126,
        ),
        max_size=20,
    ),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.booleans(),
)
_dict_body: st.SearchStrategy[dict[str, Any]] = st.dictionaries(
    _yaml_safe_keys, _yaml_safe_values, max_size=4
)


@st.composite
def typed_envelopes_string_body(draw: st.DrawFn) -> OutputEnvelope:
    return OutputEnvelope(
        header=draw(envelope_headers()),
        body=draw(_string_body),
        footer=draw(envelope_footers()),
    )


@st.composite
def typed_envelopes_dict_body(draw: st.DrawFn) -> OutputEnvelope:
    return OutputEnvelope(
        header=draw(envelope_headers()),
        body=draw(_dict_body),
        footer=draw(envelope_footers()),
    )


@pytest.mark.slow
@given(env=typed_envelopes_string_body())
@settings(max_examples=150, deadline=None)
def test_typed_envelope_roundtrip_eq_string_body(env: OutputEnvelope) -> None:
    """``from_markdown(to_markdown(env)) == env`` for typed string-bodied envelopes."""
    md = to_markdown(env)
    parsed = from_markdown(md)
    assert parsed.header == env.header
    assert parsed.body == env.body
    assert parsed.footer == env.footer
    assert parsed == env


@pytest.mark.slow
@given(env=typed_envelopes_string_body())
@settings(max_examples=150, deadline=None)
def test_typed_envelope_roundtrip_byte_stable_string_body(env: OutputEnvelope) -> None:
    """Two consecutive emits are byte-identical for typed string-bodied envelopes."""
    md1 = to_markdown(env)
    md2 = to_markdown(from_markdown(md1))
    assert md1 == md2


@pytest.mark.slow
@given(env=typed_envelopes_dict_body())
@settings(max_examples=150, deadline=None)
def test_typed_envelope_roundtrip_eq_dict_body(env: OutputEnvelope) -> None:
    """``from_markdown(to_markdown(env)) == env`` for typed dict-bodied envelopes."""
    md = to_markdown(env)
    parsed = from_markdown(md)
    assert parsed.header == env.header
    assert parsed.body == env.body
    assert parsed.footer == env.footer
    assert parsed == env
