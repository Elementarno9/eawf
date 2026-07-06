"""The v0.6.0 re-close staging invariants (P30-I23-W36).

W36 hands the phase off to P30-I21-W22 (the re-close wave): its criteria
are retyped with real gates, the runbook artifact drafts the exact
phase-close subject, and the repo carries fresh deterministic evidence —
the three preconditions the ship gate reads before the operator confirms
the merge.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_RUNBOOK = _REPO_ROOT / ".ea" / "artifacts" / "plans" / "2026-07-03-v0.6.0-reclose-runbook.md"

#: The extraction regex from .github/workflows/phase-release.yaml — the
#: annotation must be its own paren group for the tag job to fire.
_EXTRACTION_RE = re.compile(r"\(release=(v\d+\.\d+\.\d+(?:a\d+|b\d+|rc\d+)?)\)")

_DRAFTED_SUBJECT = "[P30] state: close iter + phase (audit=A-P30-I22-ship) (release=v0.6.0)"


def _state() -> dict:
    return json.loads(_STATE_PATH.read_text(encoding="utf-8"))


def test_reclose_wave_is_retyped_with_gates() -> None:
    """CR-01: P30-I21-W22 carries typed, gated criteria while PENDING."""
    wave = _state()["waves"]["P30-I21-W22"]
    criteria = wave.get("success_criteria") or []
    assert criteria, "the re-close wave lost its criteria"
    for criterion in criteria:
        assert criterion.get("kind") != "legacy", f"{criterion.get('id')} still legacy"
        assert criterion.get("gate_ids"), f"{criterion.get('id')} is gateless"
    gate_argvs = [
        " ".join((gate.get("args") or {}).get("argv") or []) for gate in wave.get("gates") or []
    ]
    assert any("test_commit_prefix_lint" in argv for argv in gate_argvs), (
        "the release-annotation dry-run gate (W32 suite) is missing"
    )
    assert any("test_reclose_staging" in argv for argv in gate_argvs), (
        "the CHANGELOG structural gate is missing"
    )


def test_runbook_drafts_the_exact_subject_and_descope() -> None:
    """CR-02: the runbook carries the subject (own paren group) + de-scope."""
    body = _RUNBOOK.read_text(encoding="utf-8")
    assert _DRAFTED_SUBJECT in body
    match = _EXTRACTION_RE.search(_DRAFTED_SUBJECT)
    assert match and match.group(1) == "v0.6.0"
    # The fused shape must NOT match the extraction regex — the runbook
    # exists to prevent exactly that silent miss.
    assert not _EXTRACTION_RE.search("(audit=A-P30-I22-ship, release=v0.6.0)")
    assert "D-WINDOWS-DESCOPE" in body
    assert "de-scoped" in body or "de-scope" in body


def test_repo_census_has_no_stray_pending_waves() -> None:
    """CR-02: the re-close wave stays PENDING; no stray PENDING wave outside the repair iter.

    The strict pre-I25 form asserted P30-I21-W22 was the *only* PENDING wave --
    the "staged to ship" precondition. The v0.6.0 live smoke reopened the ship
    path with the P30-I25 headless-lifecycle repair iter, so its still-open
    waves are legitimately PENDING too. The invariant that still guards the ship
    gate: no PENDING wave is *stray* (outside the re-close wave and the ACTIVE
    iter), and the re-close wave is still on record to carry the phase close.
    When I25 closes its waves leave the PENDING set and only P30-I21-W22 remains
    -- the original strict census re-emerges without any edit here.
    """
    state = _state()
    active_iter = state["current"].get("iter_id")
    waves = state["waves"]
    pending = [wave_id for wave_id, wave in waves.items() if wave.get("status") == "pending"]
    assert "P30-I21-W22" in pending, "the re-close wave must stay PENDING until the phase close"
    stray = [
        wave_id
        for wave_id in pending
        if wave_id != "P30-I21-W22" and waves[wave_id].get("iter_id") != active_iter
    ]
    assert not stray, f"stray PENDING waves outside the ACTIVE repair iter {active_iter!r}: {stray}"


def test_changelog_carries_the_release_section() -> None:
    """W22 G-02: the 0.6.0 heading, a bullet, and both migration notes."""
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## \[0\.6\.0\]", changelog, re.M)
    section = changelog.split("## [0.6.0]", 1)[1].split("\n## [", 1)[0]
    assert re.search(r"^- ", section, re.M), "0.6.0 section has no bullets"
    assert "1.13" in section and "1.14" in section, "missing schema migration notes"


def test_fresh_deterministic_evidence_precondition() -> None:
    """CR-03: >= 1 deterministic pass row dated within/after the I22 open."""
    state = _state()
    i22_open = state["iters"]["P30-I22"]["opened_at"]
    evidence = (_REPO_ROOT / ".ea" / "store" / "evidence.jsonl").read_text(encoding="utf-8")
    fresh = 0
    for line in evidence.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        payload = row.get("payload") or {}
        if (
            payload.get("evidence_kind") == "deterministic"
            and payload.get("status") == "pass"
            and (row.get("created_at") or "") >= i22_open
        ):
            fresh += 1
    assert fresh >= 1, "teeth metric dark: no fresh deterministic pass evidence"
