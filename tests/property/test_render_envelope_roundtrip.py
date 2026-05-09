"""Property tests for the JSON ⇄ markdown envelope round-trip.

The contract is two-fold:

- ``from_markdown(to_markdown(env)) == env`` — value equality.
- ``to_markdown(env) == to_markdown(from_markdown(to_markdown(env)))``
  — byte-stable idempotence (round-trip never drifts after the first
  emit).

Hypothesis generates header/footer dicts of primitive YAML-safe values
and a printable body string. Body characters are restricted to the
printable-ASCII + common-whitespace range so ``yaml.safe_dump`` never
escapes the body in surprising ways and the closing-fence detection
inside :func:`from_markdown` stays unambiguous.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.render.envelope import OutputEnvelope, from_markdown, to_markdown

# YAML-safe key alphabet: letters, digits, underscores, dashes. Avoids the
# special chars (``:``, ``#``, ``-`` leading, etc.) that yaml would quote.
_KEY_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"  # pragma: allowlist secret
)
keys = st.text(alphabet=_KEY_ALPHABET, min_size=1, max_size=12)

# Value strategies kept primitive — Phase 4 W01 will introduce a typed
# header/footer model and the property test will tighten with it.
primitive_values = st.one_of(
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

dict_values: st.SearchStrategy[dict[str, Any]] = st.dictionaries(keys, primitive_values, max_size=6)

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


envelopes = st.builds(
    OutputEnvelope,
    header=dict_values,
    body=body_text,
    footer=dict_values,
)


@given(env=envelopes)
@settings(max_examples=150, deadline=None)
def test_envelope_roundtrip_eq(env: OutputEnvelope) -> None:
    """``from_markdown(to_markdown(env)) == env`` for any valid envelope."""
    md = to_markdown(env)
    parsed = from_markdown(md)
    assert parsed.header == env.header
    assert parsed.body == env.body
    assert parsed.footer == env.footer
    assert parsed == env


@given(env=envelopes)
@settings(max_examples=150, deadline=None)
def test_envelope_roundtrip_byte_stable(env: OutputEnvelope) -> None:
    """Two consecutive emits are byte-identical (no drift)."""
    md1 = to_markdown(env)
    md2 = to_markdown(from_markdown(md1))
    assert md1 == md2
