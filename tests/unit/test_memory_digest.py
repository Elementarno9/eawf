"""Unit tests for the standup digest's markdown renderer.

Covers the renderer's determinism and the doc-clarity HARD gate: the rendered
``--md`` digest must pass the composed prose lints (no manual wrap, reference
hygiene) and every lifecycle id it emits must be glossed on first use.
"""

from __future__ import annotations

import re

import pytest

from eawf.platform.lint.validate_prose import validate_prose
from eawf.platform.memory.digest import (
    DigestEntry,
    MemoryDigest,
    build_native_digest,
    render_digest_md,
)
from eawf.platform.profiles.clarity import internal_codes_in

# A ref token is a lifecycle/decision id optionally carrying an iter/wave tail,
# e.g. ``P29``, ``P29-I08``, ``D01``. The gloss check asserts each such token
# is immediately followed by `` (`` so the id is defined inline.
_REF_TOKEN_RE = re.compile(r"\b(?:[PIW]\d{2,}(?:-[IW]\d{2,})*|D\d{2,})")


def _empty_digest() -> MemoryDigest:
    """The digest of a born tree that holds no decision yet."""
    return build_native_digest([])


def _mid_flight_digest() -> MemoryDigest:
    """A digest carrying every section: focus, closed iters and decisions."""
    return MemoryDigest(
        phase=DigestEntry("P29", "Ship the v0.5.0 mega-phase", "active"),
        iter=DigestEntry("P29-I08", "Refactor the interface and arm the QC gate", "active"),
        recently_closed=[
            DigestEntry("P29-I07", "Land the doc-clarity and math-explainer layer", "audit pass"),
            DigestEntry("P29-I06", "Build the trust and release scaffolding", ""),
        ],
        recent_decisions=[
            DigestEntry("D02", "Adopt Pydantic v2 strict models at every boundary", "active"),
            DigestEntry("D01", "Pick portalocker for cross-platform file locks", "active"),
        ],
    )


def test_render_digest_md_is_deterministic() -> None:
    """Two renders of the same digest produce byte-identical markdown."""
    digest = _mid_flight_digest()
    assert render_digest_md(digest) == render_digest_md(digest)


def test_render_digest_md_empty_state_reads_clean() -> None:
    """The empty digest still renders honest headings and passes the gate."""
    md = render_digest_md(_empty_digest())
    assert "No phase or iter is active right now." in md
    assert "No iter has closed yet." in md
    assert "No decision has been recorded yet." in md
    assert validate_prose(md, strict=True).ok


@pytest.mark.parametrize(
    "factory",
    [_empty_digest, _mid_flight_digest],
    ids=["empty", "mid_flight"],
)
def test_render_digest_md_passes_prose_gate(factory) -> None:  # type: ignore[no-untyped-def]
    """HARD gate: the rendered digest passes the composed deterministic prose lints.

    ``validate_prose(strict=True)`` runs EAWF014 (no-manual-wrap) plus the
    bracket-position and inline-reference checks; a non-``ok`` report means the
    digest drifted from one line per paragraph or grew inline path/link soup.
    """
    md = render_digest_md(factory())
    report = validate_prose(md, strict=True)
    assert report.ok, "\n".join(f.render() for f in report.findings)


def test_render_digest_md_glosses_every_lifecycle_id() -> None:
    """HARD gate: every internal code in the digest is glossed on first use.

    The clarity standard (:mod:`eawf.platform.profiles.clarity`) forbids a bare
    lifecycle / decision id in newcomer prose unless it is glossed. The digest
    glosses each id as ``id (title)``; this asserts every ref token the
    clarity scanner can see is immediately followed by the gloss open-paren so
    a newcomer can read the standup without opening state.json.
    """
    md = render_digest_md(_mid_flight_digest())
    # The clarity scanner must find at least one code (else the gate is vacuous).
    assert internal_codes_in(md), "expected lifecycle ids in a mid-flight digest"
    for match in _REF_TOKEN_RE.finditer(md):
        tail = md[match.end() : match.end() + 2]
        assert tail == " (", (
            f"un-glossed lifecycle id {match.group(0)!r} at offset {match.start()} "
            f"(followed by {tail!r}, expected ' (')"
        )
