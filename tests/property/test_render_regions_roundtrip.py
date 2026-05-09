"""Property tests for ``eawf.render.regions`` round-trip + idempotence."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.render import regions

# ids match the marker regex ``[A-Za-z0-9_.-]+`` and stay short.
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_-."
ids = st.text(
    alphabet=st.characters(whitelist_categories=(), whitelist_characters=_ID_ALPHABET),
    min_size=1,
    max_size=10,
)

# Bodies must not themselves contain a managed-region marker — Hypothesis
# shrinking can otherwise produce a body that turns the test fixture into an
# invalid (nested or duplicate) document. Bound body chars + length to keep
# the marker substring statistically improbable AND filter as a belt-and-braces.
bodies = st.text(
    alphabet=st.characters(blacklist_characters="<>", min_codepoint=32, max_codepoint=126),
    max_size=80,
)

# Surrounding text follows the same rule — no embedded markers.
surrounding = st.text(
    alphabet=st.characters(blacklist_characters="<>", min_codepoint=32, max_codepoint=126),
    max_size=40,
)


@given(text=surrounding, region_id=ids, body=bodies)
@settings(max_examples=200, deadline=None)
def test_replace_region_roundtrip(text: str, region_id: str, body: str) -> None:
    out = regions.replace_region(text, id=region_id, version="1.0", body=body)
    region = regions.extract_region(out, region_id)
    assert region is not None
    assert region.body == body
    assert region.declared_hash == regions.compute_hash(body)


@given(text=surrounding, region_id=ids, body=bodies)
@settings(max_examples=200, deadline=None)
def test_replace_idempotent(text: str, region_id: str, body: str) -> None:
    once = regions.replace_region(text, id=region_id, version="1.0", body=body)
    twice = regions.replace_region(once, id=region_id, version="1.0", body=body)
    assert once == twice


@given(region_id=ids, body=bodies)
@settings(max_examples=200, deadline=None)
def test_replace_changes_body(region_id: str, body: str) -> None:
    """Re-rendering with a new body must replace, not duplicate."""
    once = regions.replace_region("", id=region_id, version="1.0", body=body)
    twice = regions.replace_region(once, id=region_id, version="1.0", body=body + "_v2")
    region = regions.extract_region(twice, region_id)
    assert region is not None
    assert region.body == body + "_v2"
    # Exactly one BEGIN marker for region_id.
    assert twice.count(f"<!-- BEGIN EAWF:managed id={region_id} ") == 1
