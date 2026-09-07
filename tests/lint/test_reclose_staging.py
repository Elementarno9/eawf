"""The v0.6.0 re-close staging invariants.

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


#: The phase-close vehicle after the I26 reconcile. The original re-close wave
#: (P30-I21-W22) was claimed + closed in the I26 Step-1 reconcile, and the
#: phase-close ceremony moved onto P30-I26-W23.
_RECLOSE_WAVE = "P30-I26-W23"
_SUPERSEDED_RECLOSE_WAVE = "P30-I21-W22"


def _stray_pending_waves(state: dict) -> list[str]:
    """The PENDING waves that no live plan accounts for.

    A PENDING wave is accounted for when it is the re-close vehicle, when it
    sits in the ACTIVE iter, or when its phase is still PLANNED. The phase is
    read off the ``P<NN>`` prefix that a wave id and its iter id share, and a
    wave whose phase has no row at all stays stray so the census fails closed.
    """
    active_iter = state["current"].get("iter_id")
    phases = state["phases"]
    stray: list[str] = []
    for wave_id, wave in state["waves"].items():
        if wave.get("status") != "pending":
            continue
        if wave_id == _RECLOSE_WAVE or wave.get("iter_id") == active_iter:
            continue
        phase_id = (wave.get("iter_id") or wave_id).split("-", 1)[0]
        if (phases.get(phase_id) or {}).get("status") == "planned":
            continue
        stray.append(wave_id)
    return stray


def test_repo_census_has_no_stray_pending_waves() -> None:
    """CR-02 (I26 reconcile): the re-close wave carries the close; no stray PENDING wave.

    The I26 reconcile closed the original re-close vehicle in its
    Step 1 and moved the phase-close ceremony onto P30-I26-W23. That wave now
    waits while the phase is ACTIVE and closes in the same commit that closes the
    phase. Asserting it is PENDING outright would redden the moment that commit
    lands -- and that commit is the PR head CI runs against -- so the invariant is
    tied to the phase's status instead. While P30 is ACTIVE the wave is still on
    record to carry the close (PENDING, or CLAIMED in the close-commit window);
    once the phase is no longer ACTIVE the wave must have actually closed, which
    catches a phase closed out from under its own re-close vehicle. The superseded
    P30-I21-W22 must already be CLOSED.

    The second half is unchanged in spirit: no PENDING wave is *stray*. What
    counts as stray is narrower than "outside the ACTIVE iter", because a staged
    roadmap IS a PLANNED phase full of PENDING waves -- ``roadmap propose`` plus
    ``roadmap apply`` produce exactly that shape, so a bare iter comparison
    reddens the census for following the documented procedure. A PENDING wave
    under a PLANNED phase is therefore legitimate; one under a phase that is
    neither PLANNED nor the ACTIVE iter is an orphan and still reddens. Do not
    re-tighten this back to a bare iter comparison.
    """
    state = _state()
    active_iter = state["current"].get("iter_id")
    waves = state["waves"]
    assert waves[_SUPERSEDED_RECLOSE_WAVE]["status"] == "closed", (
        "the I26 reconcile must have closed the superseded re-close wave"
    )
    reclose_status = waves[_RECLOSE_WAVE]["status"]
    if state["phases"]["P30"]["status"] == "active":
        assert reclose_status in {"pending", "claimed"}, (
            "the re-close wave must still be on record to carry the phase close"
        )
    else:
        assert reclose_status == "closed", "the phase closed without closing its re-close wave"
    stray = _stray_pending_waves(state)
    assert not stray, (
        f"stray PENDING waves outside the ACTIVE iter {active_iter!r} "
        f"and outside every PLANNED phase: {stray}"
    )


def test_stray_pending_waves_flags_an_orphan_under_a_settled_phase() -> None:
    """The narrowed invariant still reds on the defect the census was written for.

    Three PENDING waves are accounted for -- the ACTIVE iter's own, a staged
    PLANNED phase's, and the re-close vehicle -- while the fourth hangs off a
    CLOSED phase with nothing to run it.
    """
    fixture = {
        "current": {"iter_id": "P31-I01"},
        "phases": {
            "P30": {"status": "closed"},
            "P31": {"status": "active"},
            "P32": {"status": "planned"},
        },
        "waves": {
            "P31-I01-W51": {"iter_id": "P31-I01", "status": "pending"},
            "P32-I01-W01": {"iter_id": "P32-I01", "status": "pending"},
            _RECLOSE_WAVE: {"iter_id": "P30-I26", "status": "pending"},
            "P30-I26-W91": {"iter_id": "P30-I26", "status": "pending"},
            "P30-I26-W92": {"iter_id": "P30-I26", "status": "closed"},
        },
    }
    assert _stray_pending_waves(fixture) == ["P30-I26-W91"]


@pytest.mark.parametrize(
    ("waves", "expected"),
    [
        pytest.param({}, [], id="empty"),
        pytest.param(
            {"P32-I01-W01": {"iter_id": "P32-I01", "status": "pending"}},
            [],
            id="single-planned-phase",
        ),
        pytest.param(
            {"P99-I01-W01": {"iter_id": "P99-I01", "status": "pending"}},
            ["P99-I01-W01"],
            id="phase-row-missing-fails-closed",
        ),
        pytest.param(
            {"P30-I26-W91": {"status": "pending"}},
            ["P30-I26-W91"],
            id="iter-id-absent-falls-back-to-wave-prefix",
        ),
    ],
)
def test_stray_pending_waves_edges(waves: dict, expected: list[str]) -> None:
    """Empty, single, an unknown phase, and a wave carrying no ``iter_id``."""
    state = {
        "current": {"iter_id": "P31-I01"},
        "phases": {"P30": {"status": "closed"}, "P32": {"status": "planned"}},
        "waves": waves,
    }
    assert _stray_pending_waves(state) == expected


def test_stray_pending_waves_rejects_a_state_without_phases() -> None:
    """A mis-shaped state raises instead of reporting a reassuring empty list."""
    with pytest.raises(KeyError, match="phases"):
        _stray_pending_waves({"current": {"iter_id": "P31-I01"}, "waves": {}})


def test_changelog_carries_the_release_section() -> None:
    """W22 G-02 + W36: the 0.6.0 heading, a bullet, and a note per schema edge.

    The release pre-flight requires a migration note for every ``schema_version``
    edge the release ships. The edge list is DERIVED from the persisted schema
    version rather than hardcoded, so the next bump that lands without a note
    reddens here instead of surfacing to an upgrading repo -- which is exactly
    how five edges (1.15 through 1.19) reached the ship gate undocumented.
    """
    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## \[0\.6\.0\]", changelog, re.M)
    section = changelog.split("## [0.6.0]", 1)[1].split("\n## [", 1)[0]
    assert re.search(r"^- ", section, re.M), "0.6.0 section has no bullets"
    # 1.8 is the schema v0.5.x shipped on; every edge from there to the
    # persisted version is part of this release.
    major, minor = (int(part) for part in _state()["schema_version"].split("."))
    for target in range(9, minor + 1):
        edge = f"{major}.{target - 1} -> {major}.{target}"
        assert edge in section, f"0.6.0 ships schema edge {edge} with no migration note"
    # The I25 entry described four lifecycle bugs long after the iter grew into
    # the runtime-measurement repair; pin the two strands it actually shipped.
    assert "calibration_excluded" in section, "the 0.6.0 notes omit the calibration exclusion"
    assert "measure_version" in section, "the 0.6.0 notes omit the measure versioning"


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
