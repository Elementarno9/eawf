"""Unit tests for the pure state-derived standup digest.

Covers the typed projection (ordering, caps, optional pointers), the
markdown renderer's determinism, and the doc-clarity HARD gate: the rendered
``--md`` digest must pass the composed prose lints (no manual wrap, reference
hygiene) and every lifecycle id it emits must be glossed on first use.
"""

from __future__ import annotations

import re

import pytest

from eawf.kernel.state.models import State
from eawf.platform.lint.validate_prose import validate_prose
from eawf.platform.memory.digest import (
    DigestEntry,
    build_digest,
    render_digest_md,
)
from eawf.platform.profiles.clarity import internal_codes_in

# A ref token is a lifecycle/decision id optionally carrying an iter/wave tail,
# e.g. ``P29``, ``P29-I08``, ``D01``. The gloss check asserts each such token
# is immediately followed by `` (`` so the id is defined inline.
_REF_TOKEN_RE = re.compile(r"\b(?:[PIW]\d{2,}(?:-[IW]\d{2,})*|D\d{2,})")


def _base_state() -> dict[str, object]:
    """Return a minimal valid state document (no phase/iter/decisions)."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _mid_flight_state() -> dict[str, object]:
    """Return a state with an active phase+iter, two closed iters, two decisions."""
    state = _base_state()
    state["current"]["phase_id"] = "P29"
    state["current"]["iter_id"] = "P29-I08"
    state["phases"] = {
        "P29": {
            "id": "P29",
            "scope_id": "QR",
            "title": "Ship the v0.5.0 mega-phase",
            "status": "active",
            "iter_ids": ["P29-I06", "P29-I07", "P29-I08"],
            "opened_at": "2026-05-31T00:00:00Z",
        }
    }
    state["iters"] = {
        "P29-I08": {
            "id": "P29-I08",
            "phase_id": "P29",
            "title": "Refactor the interface and arm the QC gate",
            "status": "active",
            "wave_ids": [],
            "opened_at": "2026-06-03T00:00:00Z",
        },
        "P29-I07": {
            "id": "P29-I07",
            "phase_id": "P29",
            "title": "Land the doc-clarity and math-explainer layer",
            "status": "closed",
            "wave_ids": [],
            "opened_at": "2026-06-01T00:00:00Z",
            "closed_at": "2026-06-02T00:00:00Z",
            "audit_id": "A07",
        },
        "P29-I06": {
            "id": "P29-I06",
            "phase_id": "P29",
            "title": "Build the trust and release scaffolding",
            "status": "closed",
            "wave_ids": [],
            "opened_at": "2026-05-31T00:00:00Z",
            "closed_at": "2026-06-01T00:00:00Z",
        },
    }
    state["audits"] = {
        "A07": {
            "id": "A07",
            "scope_id": "QR",
            "kind": "evaluation",
            "status": "complete",
            "created_at": "2026-06-02T00:00:00Z",
            "verdict": "pass",
        }
    }
    state["decisions"] = {
        "D01": {
            "id": "D01",
            "scope_id": "QR",
            "title": "Pick portalocker for cross-platform file locks",
            "rationale": "portalocker is the only maintained cross-platform lock.",
            "status": "active",
            "created_at": "2026-05-08T00:00:00Z",
        },
        "D02": {
            "id": "D02",
            "scope_id": "QR",
            "title": "Adopt Pydantic v2 strict models at every boundary",
            "rationale": "strict validation at ingestion keeps downstream code typed.",
            "status": "active",
            "created_at": "2026-05-09T00:00:00Z",
        },
    }
    return state


def test_build_digest_empty_state_has_no_focus() -> None:
    """A state with no phase/iter/decisions yields an empty-ish digest."""
    digest = build_digest(State.model_validate(_base_state()))
    assert digest.phase is None
    assert digest.iter is None
    assert digest.recently_closed == []
    assert digest.recent_decisions == []


def test_build_digest_orders_and_glosses() -> None:
    """Mid-flight state projects current focus, closes newest-first, decisions."""
    digest = build_digest(State.model_validate(_mid_flight_state()))
    assert digest.phase == DigestEntry("P29", "Ship the v0.5.0 mega-phase", "active")
    assert digest.iter is not None and digest.iter.ref_id == "P29-I08"
    # Closed iters newest first by closed_at; I07 (closed 06-02) before I06 (06-01).
    assert [e.ref_id for e in digest.recently_closed] == ["P29-I07", "P29-I06"]
    # The linked audit verdict rides the closed-iter row.
    assert digest.recently_closed[0].detail == "audit pass"
    assert digest.recently_closed[1].detail == ""  # I06 has no audit
    # Decisions newest first by created_at; D02 (05-09) before D01 (05-08).
    assert [e.ref_id for e in digest.recent_decisions] == ["D02", "D01"]


def test_build_digest_rejects_negative_limits() -> None:
    """Negative caps fail fast at the boundary."""
    state = State.model_validate(_base_state())
    with pytest.raises(ValueError, match="closed_limit must be >= 0"):
        build_digest(state, closed_limit=-1)
    with pytest.raises(ValueError, match="decision_limit must be >= 0"):
        build_digest(state, decision_limit=-1)


def test_build_digest_caps_lists() -> None:
    """The closed/decision caps bound the projected list lengths."""
    digest = build_digest(
        State.model_validate(_mid_flight_state()),
        closed_limit=1,
        decision_limit=1,
    )
    assert len(digest.recently_closed) == 1
    assert digest.recently_closed[0].ref_id == "P29-I07"
    assert len(digest.recent_decisions) == 1
    assert digest.recent_decisions[0].ref_id == "D02"


def test_render_digest_md_is_deterministic() -> None:
    """Two renders of the same digest produce byte-identical markdown."""
    digest = build_digest(State.model_validate(_mid_flight_state()))
    assert render_digest_md(digest) == render_digest_md(digest)


def test_render_digest_md_empty_state_reads_clean() -> None:
    """The empty-state digest still renders honest headings and passes the gate."""
    digest = build_digest(State.model_validate(_base_state()))
    md = render_digest_md(digest)
    assert "No phase or iter is active right now." in md
    assert "No iter has closed yet." in md
    assert "No decision has been recorded yet." in md
    assert validate_prose(md, strict=True).ok


@pytest.mark.parametrize(
    "factory",
    [_base_state, _mid_flight_state],
    ids=["empty", "mid_flight"],
)
def test_render_digest_md_passes_prose_gate(factory) -> None:  # type: ignore[no-untyped-def]
    """HARD gate: the rendered digest passes the composed deterministic prose lints.

    ``validate_prose(strict=True)`` runs EAWF014 (no-manual-wrap) plus the
    bracket-position and inline-reference checks; a non-``ok`` report means the
    digest drifted from one line per paragraph or grew inline path/link soup.
    """
    digest = build_digest(State.model_validate(factory()))
    md = render_digest_md(digest)
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
    digest = build_digest(State.model_validate(_mid_flight_state()))
    md = render_digest_md(digest)
    # The clarity scanner must find at least one code (else the gate is vacuous).
    assert internal_codes_in(md), "expected lifecycle ids in a mid-flight digest"
    for match in _REF_TOKEN_RE.finditer(md):
        tail = md[match.end() : match.end() + 2]
        assert tail == " (", (
            f"un-glossed lifecycle id {match.group(0)!r} at offset {match.start()} "
            f"(followed by {tail!r}, expected ' (')"
        )
