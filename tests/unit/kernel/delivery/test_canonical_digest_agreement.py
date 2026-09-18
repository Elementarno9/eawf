"""The two canonical_digest homes agree, including on payloads JSON would escape."""

from __future__ import annotations

import pytest

from eawf.kernel.delivery.receipts import canonical_digest as receipts_digest
from eawf.kernel.runtime.compiled import canonical_digest as compiled_digest

#: Payloads whose UTF-8 form differs from their ``\\uXXXX`` escaped form. An
#: implementation that escapes and one that does not agree on every ASCII payload,
#: so only these discriminate.
NON_ASCII_PAYLOADS: list[tuple[str, dict[str, object]]] = [
    ("brand", {"summary": "Eä ships"}),
    ("changed_path", {"changed_paths": ["docs/café/naïve.md"]}),
    ("nested", {"outer": {"inner": ["ß", "→"]}}),
    ("key", {"clé": "value"}),
]


@pytest.mark.parametrize(
    ("payload"),
    [pytest.param(payload, id=name) for name, payload in NON_ASCII_PAYLOADS],
)
def test_canonical_digest_agrees_across_homes_on_non_ascii(payload: dict[str, object]) -> None:
    """Both homes digest one payload to one value.

    A proof written through one home and checked through the other must compare equal.
    The two encode independently, so a payload outside ASCII is the only input that can
    separate them.
    """
    assert compiled_digest(payload) == receipts_digest(payload)


def test_canonical_digest_is_stable_across_key_order() -> None:
    """Structural equality, not authoring order, decides the digest."""
    assert compiled_digest({"b": 1, "a": 2}) == compiled_digest({"a": 2, "b": 1})
    assert receipts_digest({"b": 1, "a": 2}) == receipts_digest({"a": 2, "b": 1})


def test_a_non_ascii_payload_digests_differently_from_its_escaped_spelling() -> None:
    """The guard above has teeth: escaping really does move the digest.

    Were an implementation to escape non-ASCII, it would produce the digest of the
    escaped spelling, which this asserts is a different value. A guard that could not
    tell the two apart would pass whatever either home did.
    """
    assert compiled_digest({"s": "Eä"}) != compiled_digest({"s": "E\\u00e4"})
