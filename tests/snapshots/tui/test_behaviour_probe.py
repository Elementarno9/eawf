"""Unit tests for the behaviour-probe transcript evidence channel.

Covers :mod:`eawf.surfaces.tui.snapshot.behaviour_probe`: that a real
action records as observable (with the signals that moved), that a
resolved-but-inert action records as a no-op, that an unresolved action
records as unresolved, that the transcript carries its source-commit
provenance stamp, that :func:`render_transcript_evidence` surfaces the
outcomes + the commit, and that re-running the same probes yields the
same transcript (worker-drained determinism).

The no-op + unresolved cases are the load-bearing ones: they are the two
dead-click shapes a golden snapshot cannot see (a resolved markup link
that fires nothing, and a stale action string that never resolves), and
the transcript MUST mark them distinctly from a live action.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.snapshot.behaviour_probe import (
    BehaviourTranscript,
    ProbeOutcome,
    ProbeStatus,
    record_behaviour_transcript,
    render_transcript_evidence,
)

_REPO_STATE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)
_SIZE = (120, 40)
_COMMIT = "abc1234"

# A real action that changes observable state (home -> doctor; doctor is a
# legal mode at every scope so the switch is always accepted).
_REAL_ACTION = "app.switch_mode('doctor')"
# A resolved-but-inert action: switching to doctor a SECOND time resolves
# (run_action returns truthy) but changes nothing observable -- the
# dead-click signature a golden snapshot cannot distinguish from a live one.
_NOOP_ACTION = "app.switch_mode('doctor')"
# An action string that never resolves to a handler (stale / mis-named) --
# run_action returns falsy, the pre-fix dead-click shape.
_UNRESOLVED_ACTION = "app.totally_bogus_action()"


def _record(probes: list[str], *, commit: str = _COMMIT) -> BehaviourTranscript:
    """Run *probes* against a fresh repo-scope app and return the transcript."""

    async def body() -> BehaviourTranscript:
        app = EaApp(scope="repo", state_path=_REPO_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            return await record_behaviour_transcript(pilot, probes, source_commit=commit)

    return asyncio.run(body())


# --------------------------------------------------------------------------
# record_behaviour_transcript — outcome classification
# --------------------------------------------------------------------------


def test_real_action_records_as_observable() -> None:
    transcript = _record([_REAL_ACTION])
    assert len(transcript.outcomes) == 1
    outcome = transcript.outcomes[0]
    assert outcome.status is ProbeStatus.OBSERVABLE
    assert outcome.action == _REAL_ACTION
    # The signals name what moved -- the mode flip is the observable change.
    joined = "; ".join(outcome.signals)
    assert "current_mode" in joined
    assert "'home' -> 'doctor'" in joined


def test_resolved_but_inert_action_records_as_no_op() -> None:
    # First switch lands doctor (observable); the second resolves but changes
    # nothing -- the dead-click the channel exists to catch.
    transcript = _record([_REAL_ACTION, _NOOP_ACTION])
    first, second = transcript.outcomes
    assert first.status is ProbeStatus.OBSERVABLE
    assert second.status is ProbeStatus.NO_OP
    # A no-op carries no signals (nothing moved).
    assert second.signals == ()


def test_unresolved_action_records_as_unresolved() -> None:
    transcript = _record([_UNRESOLVED_ACTION])
    outcome = transcript.outcomes[0]
    assert outcome.status is ProbeStatus.UNRESOLVED
    assert outcome.signals == ()


def test_dead_click_is_distinguishable_from_live_action() -> None:
    # The load-bearing assertion: the SAME action string records as
    # observable when it does something and as a no-op when it does not, so a
    # jury can tell a live click from a dead one in the transcript.
    transcript = _record([_REAL_ACTION, _NOOP_ACTION])
    statuses = [o.status for o in transcript.outcomes]
    assert statuses == [ProbeStatus.OBSERVABLE, ProbeStatus.NO_OP]
    assert transcript.outcomes[0].status is not transcript.outcomes[1].status


def test_probes_preserve_order() -> None:
    transcript = _record([_REAL_ACTION, _UNRESOLVED_ACTION, _NOOP_ACTION])
    assert [o.action for o in transcript.outcomes] == [
        _REAL_ACTION,
        _UNRESOLVED_ACTION,
        _NOOP_ACTION,
    ]


def test_empty_probe_list_yields_empty_transcript() -> None:
    transcript = _record([])
    assert transcript.outcomes == ()
    # Provenance is still stamped even with no probes.
    assert transcript.source_commit == _COMMIT


# --------------------------------------------------------------------------
# provenance stamp
# --------------------------------------------------------------------------


def test_transcript_carries_source_commit() -> None:
    transcript = _record([_REAL_ACTION], commit="deadbeef")
    assert transcript.source_commit == "deadbeef"


# --------------------------------------------------------------------------
# render_transcript_evidence
# --------------------------------------------------------------------------


def test_render_evidence_includes_commit_and_outcomes() -> None:
    transcript = _record([_REAL_ACTION, _NOOP_ACTION, _UNRESOLVED_ACTION])
    rendered = render_transcript_evidence(transcript)
    # Provenance pin is present.
    assert "source_commit: " + _COMMIT in rendered
    # Every probed action appears.
    assert _REAL_ACTION in rendered
    assert _UNRESOLVED_ACTION in rendered
    # The observable probe surfaces its signal delta.
    assert "observable" in rendered
    assert "'home' -> 'doctor'" in rendered


def test_render_evidence_marks_dead_clicks_distinctly() -> None:
    transcript = _record([_REAL_ACTION, _NOOP_ACTION, _UNRESOLVED_ACTION])
    rendered = render_transcript_evidence(transcript)
    # Both dead-click shapes carry an explicit marker a jury can read.
    assert "NO-OP" in rendered
    assert "UNRESOLVED" in rendered
    assert rendered.count("dead click") == 2
    # The live action is NOT flagged as a dead click on its own line.
    observable_line = next(line for line in rendered.splitlines() if "observable" in line)
    assert "dead click" not in observable_line


# --------------------------------------------------------------------------
# determinism — re-running yields the same transcript (workers drained)
# --------------------------------------------------------------------------


def test_transcript_is_deterministic_across_runs() -> None:
    probes = [_REAL_ACTION, _NOOP_ACTION, _UNRESOLVED_ACTION]
    first = _record(probes)
    second = _record(probes)
    # The provenance differs only if the commit differs; here both stamp the
    # same commit, so the whole typed transcript must compare equal.
    assert first == second


# --------------------------------------------------------------------------
# typed-model strictness (rule 2: extra="forbid")
# --------------------------------------------------------------------------


def test_probe_outcome_forbids_extra_keys() -> None:
    with pytest.raises(ValueError):
        ProbeOutcome(  # type: ignore[call-arg]
            action="app.x()",
            status=ProbeStatus.NO_OP,
            bogus=1,
        )


def test_behaviour_transcript_forbids_extra_keys() -> None:
    with pytest.raises(ValueError):
        BehaviourTranscript(  # type: ignore[call-arg]
            source_commit="abc",
            outcomes=(),
            bogus=1,
        )
