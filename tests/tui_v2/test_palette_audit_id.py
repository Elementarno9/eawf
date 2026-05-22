"""Unit tests for :func:`_active_audit_id` newest-for-scope resolution (P26-W15).

The ``/audit`` verb derives the overlay's audit id from the bound state
rather than operator free-text. Before W15 it returned the first iter's
``audit_id`` in dict order — the OLDEST iter — which surfaced a stale
other-phase audit (``A09-P08``) instead of the current scope's. These
tests pin the corrected contract: the newest audit (by ``created_at``)
whose ``scope_id`` matches the current phase wins, with a newest-overall
fallback and an ``"audit"`` placeholder when no state / audits exist —
all without mounting Textual (a tiny duck-typed app holds the state).
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.state.models import State
from eawf.tui_v2.palette.verbs import _active_audit_id


@dataclass
class _FakeApp:
    """A minimal stand-in exposing only the ``state`` attribute the verb reads."""

    state: State | None


def _state_with_audits(
    *,
    phase_id: str | None,
    audits: dict[str, dict[str, object]],
) -> State:
    """Build a minimal valid State carrying *audits* and a current phase.

    Args:
        phase_id: The value for ``current.phase_id`` (or ``None`` to leave
            it unset, exercising the no-scope fallback path).
        audits: Mapping of audit id -> the per-audit override fields
            (``scope_id`` + ``created_at``); the rest of each row is filled
            with valid defaults.

    Returns:
        A validated :class:`~eawf.state.models.State`.
    """
    audit_payload = {
        aid: {
            "id": aid,
            "scope_id": fields["scope_id"],
            "kind": "evaluation",
            "status": "complete",
            "created_at": fields["created_at"],
            "verdict": "pass",
        }
        for aid, fields in audits.items()
    }
    payload: dict[str, object] = {
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
            "subproject_id": None,
            "phase_id": phase_id,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "audits": audit_payload,
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


# --------------------------------------------------------------------------
# newest-for-scope wins (the headline contract)
# --------------------------------------------------------------------------


def test_active_audit_id_newest_scoped_audit_wins() -> None:
    # Two audits for the current phase: the newer ``created_at`` is chosen.
    state = _state_with_audits(
        phase_id="P26",
        audits={
            "A33-P26-old": {"scope_id": "P26", "created_at": "2026-05-21T02:40:08Z"},
            "A34-P26-new": {"scope_id": "P26", "created_at": "2026-05-22T02:28:43Z"},
        },
    )
    assert _active_audit_id(_FakeApp(state)) == "A34-P26-new"


def test_active_audit_id_does_not_return_stale_other_phase_first_entry() -> None:
    # The regression: a stale other-phase audit sits FIRST in dict order
    # (mirrors the live ``A09-P08`` that the old first-iter logic returned).
    # The newest audit scoped to the current phase must win regardless.
    state = _state_with_audits(
        phase_id="P26",
        audits={
            "A09-P08": {"scope_id": "EAWF", "created_at": "2026-05-10T23:05:28Z"},
            "A34-P26-tui-qol": {"scope_id": "P26", "created_at": "2026-05-22T02:28:43Z"},
        },
    )
    chosen = _active_audit_id(_FakeApp(state))
    assert chosen == "A34-P26-tui-qol"
    assert chosen != "A09-P08"


def test_active_audit_id_matches_descendant_scope_prefix() -> None:
    # An iter- / wave-scoped audit under the current phase (``P26-...``)
    # still counts as in-scope and wins when it is the newest such audit.
    state = _state_with_audits(
        phase_id="P26",
        audits={
            "A32-P26-I01": {"scope_id": "P26-I01", "created_at": "2026-05-20T14:06:52Z"},
            "A40-P27": {"scope_id": "P27", "created_at": "2026-05-23T00:00:00Z"},
        },
    )
    # The newer A40 is OUT of scope (P27), so the in-scope P26-I01 wins.
    assert _active_audit_id(_FakeApp(state)) == "A32-P26-I01"


# --------------------------------------------------------------------------
# fallback newest overall when nothing matches the current phase
# --------------------------------------------------------------------------


def test_active_audit_id_falls_back_to_newest_overall_when_no_scoped_match() -> None:
    # No audit matches the current phase: fall back to the newest overall.
    state = _state_with_audits(
        phase_id="P26",
        audits={
            "A20-P15": {"scope_id": "EAWF", "created_at": "2026-05-13T12:51:00Z"},
            "A24-P19": {"scope_id": "P19", "created_at": "2026-05-14T22:22:39Z"},
        },
    )
    assert _active_audit_id(_FakeApp(state)) == "A24-P19"


def test_active_audit_id_no_current_phase_falls_back_to_newest_overall() -> None:
    # current.phase_id unset (None): every audit is out-of-scope, so the
    # newest-overall fallback applies (the guard must not crash on None).
    state = _state_with_audits(
        phase_id=None,
        audits={
            "A20-P15": {"scope_id": "EAWF", "created_at": "2026-05-13T12:51:00Z"},
            "A24-P19": {"scope_id": "P19", "created_at": "2026-05-14T22:22:39Z"},
        },
    )
    assert _active_audit_id(_FakeApp(state)) == "A24-P19"


# --------------------------------------------------------------------------
# placeholder when no state / no audits
# --------------------------------------------------------------------------


def test_active_audit_id_none_state_returns_placeholder() -> None:
    assert _active_audit_id(_FakeApp(None)) == "audit"


def test_active_audit_id_no_audits_returns_placeholder() -> None:
    state = _state_with_audits(phase_id="P26", audits={})
    assert _active_audit_id(_FakeApp(state)) == "audit"


def test_active_audit_id_audits_none_returns_placeholder() -> None:
    # ``state.audits`` is ``dict | None``; the explicit-None case (no audits
    # key in the document) must also degrade to the placeholder.
    state = _state_with_audits(phase_id="P26", audits={})
    object.__setattr__(state, "audits", None)
    assert _active_audit_id(_FakeApp(state)) == "audit"
