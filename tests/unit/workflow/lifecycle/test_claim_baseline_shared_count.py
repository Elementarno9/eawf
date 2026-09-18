"""A claim baseline records the concurrency the claim was taken under.

One vendor runtime session's counters are cumulative for the session, not
for any wave in it, so every wave claimed into that session differences the
same numbers. Without the sharer count each of them banks the whole
session; the divisor is the only thing that turns one session's runtime
into N shares of it.

The count is stamped by the real ``claim_wave`` transition against a real
counter source: the claiming session discloses a vendor runtime session id
and a statusline counter sidecar stands on disk for it, so nothing here
monkeypatches the capture it is asserting on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import AgentSession, CurrentPointers, Project, State
from eawf.runtime.runtime_counter_sidecar import (
    RuntimeCounterSidecar,
    sidecar_path_for_statusline_cache,
)
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.runtime.runtimes.claude.statusline import cache_path_for
from eawf.workflow.lifecycle._claim_session import (
    capture_claim_baseline,
    count_runtime_session_sharers,
)
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from eawf.workflow.lifecycle.wave import claim_wave
from tests.conftest import make_claim_criterion, make_intent

_VENDOR_SESSION = "vendor-session-shared-count"
_OTHER_VENDOR_SESSION = "vendor-session-alone"


@pytest.fixture(autouse=True)
def isolated_counter_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both counter sources at empty tmp roots, then chdir into one.

    An ambient transcript or sidecar from the developer's own machine would
    make the assertions depend on whatever session happened to be open.
    """
    projects = tmp_path / "projects"
    cache = tmp_path / "statusline-cache"
    projects.mkdir()
    cache.mkdir()
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(cache))
    monkeypatch.chdir(tmp_path)


def write_sidecar(vendor_session_id: str) -> None:
    """Write a real statusline counter sidecar for *vendor_session_id*."""
    sidecar = RuntimeCounterSidecar(
        sidecar_path_for_statusline_cache(cache_path_for(vendor_session_id))
    )
    sidecar.write(
        RuntimeCounters(
            api_duration_ms=7_000,
            total_duration_ms=9_000,
            cost_usd=None,
            input_tokens=11,
            output_tokens=22,
            harness="claude-code",
            model="claude-sonnet-5",
        )
    )


def seeded_state(*, wave_ids: tuple[str, ...]) -> State:
    """Return a state whose phase is open and whose waves are claimable."""
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    open_phase(state, phase_id="P01", title="Stamp the sharer count on a claim baseline")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Claim into a shared session")
    for index, wave_id in enumerate(wave_ids, start=1):
        plan_wave(
            state,
            wave_id=wave_id,
            iter_id="P01-I01",
            title=f"Claim wave {index} into a shared runtime session",
            file_scopes=["x"],
            effort_bucket="M",
            intent=make_intent(),
            success_criteria=[make_claim_criterion(f"CR-{index}")],
        )
    return state


def seed_session(
    state: State, *, session_id: str, vendor_session_id: str | None, scope_id: str = "QR"
) -> AgentSession:
    """Insert one ACTIVE session disclosing *vendor_session_id* (or nothing)."""
    session = AgentSession(
        id=session_id,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude",
        scope_id=scope_id,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
        runtime_session_id=vendor_session_id,
    )
    state.agent_sessions[session_id] = session
    state.current.active_session_ids.append(session_id)
    return session


# ---------------------------------------------------------------------------
# B110: the claim baseline carries the concurrency the claim saw
# ---------------------------------------------------------------------------


def test_a_sole_claim_stamps_one_sharer() -> None:
    """A wave with the vendor session to itself divides by one, not by zero."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    wave = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")

    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.shared_wave_count == 1
    assert wave.runtime_baseline.session_id == _VENDOR_SESSION


def test_a_second_claim_into_the_same_runtime_session_stamps_two() -> None:
    """The sharer joining the session is counted, so neither banks it whole."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01", "P01-I01-W02"))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)
    seed_session(state, session_id="SES-02", vendor_session_id=_VENDOR_SESSION)

    first = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")
    second = claim_wave(state, wave_id="P01-I01-W02", session_id="SES-02")

    assert first.runtime_baseline is not None
    assert first.runtime_baseline.shared_wave_count == 1
    assert second.runtime_baseline is not None
    assert second.runtime_baseline.shared_wave_count == 2


def test_two_claims_on_one_session_row_stamp_the_same_sharer_count() -> None:
    """One EAWF session claiming twice is still one vendor session, shared."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01", "P01-I01-W02"))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")
    second = claim_wave(state, wave_id="P01-I01-W02", session_id="SES-01")

    assert second.runtime_baseline is not None
    assert second.runtime_baseline.shared_wave_count == 2


def test_a_claim_into_a_different_runtime_session_is_not_a_sharer() -> None:
    """Concurrency is per vendor session, not per repository."""
    write_sidecar(_VENDOR_SESSION)
    write_sidecar(_OTHER_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01", "P01-I01-W02"))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)
    seed_session(state, session_id="SES-02", vendor_session_id=_OTHER_VENDOR_SESSION)

    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")
    second = claim_wave(state, wave_id="P01-I01-W02", session_id="SES-02")

    assert second.runtime_baseline is not None
    assert second.runtime_baseline.shared_wave_count == 1


def test_a_session_disclosing_no_vendor_session_captures_no_baseline() -> None:
    """An EAWF session id is not a vendor one, so nothing is resolved by it."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=None)

    wave = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")

    assert wave.runtime_baseline is None


def test_a_vendor_session_with_no_counters_captures_no_baseline() -> None:
    """Absence is honest: no phantom zero origin for the close-time delta."""
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    wave = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")

    assert wave.runtime_baseline is None


def test_an_idempotent_reclaim_leaves_the_stamped_count_alone() -> None:
    """Re-entering a claim must not re-base the origin it already recorded."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)
    first = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")
    captured = first.runtime_baseline

    again = claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")

    assert again.runtime_baseline is captured


# ---------------------------------------------------------------------------
# The counter itself: boundaries and the empty case
# ---------------------------------------------------------------------------


def test_counting_sharers_of_a_session_nobody_claimed_is_zero() -> None:
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    assert count_runtime_session_sharers(state, runtime_session_id=_VENDOR_SESSION) == 0


def test_counting_sharers_of_an_unknown_session_is_zero() -> None:
    state = seeded_state(wave_ids=("P01-I01-W01",))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    assert count_runtime_session_sharers(state, runtime_session_id="nobody-here") == 0


def test_a_closed_wave_stops_sharing_the_session_it_claimed_in() -> None:
    """The divisor is live concurrency, so a finished wave stops counting."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01", "P01-I01-W02"))
    seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)
    claim_wave(state, wave_id="P01-I01-W01", session_id="SES-01")
    state.waves["P01-I01-W01"].status = WaveStatus.CLOSED

    second = claim_wave(state, wave_id="P01-I01-W02", session_id="SES-01")

    assert second.runtime_baseline is not None
    assert second.runtime_baseline.shared_wave_count == 1


def test_capturing_against_a_session_row_with_no_vendor_id_returns_none() -> None:
    """The unit refuses the same thing the transition does, for the same reason."""
    state = seeded_state(wave_ids=("P01-I01-W01",))
    session = seed_session(state, session_id="SES-01", vendor_session_id=None)

    assert capture_claim_baseline(state, session) is None


def test_capturing_before_any_wave_is_bound_floors_the_divisor_at_one() -> None:
    """A zero divisor is not a thing, and the capture's own session is in flight."""
    write_sidecar(_VENDOR_SESSION)
    state = seeded_state(wave_ids=("P01-I01-W01",))
    session = seed_session(state, session_id="SES-01", vendor_session_id=_VENDOR_SESSION)

    baseline = capture_claim_baseline(state, session)

    assert count_runtime_session_sharers(state, runtime_session_id=_VENDOR_SESSION) == 0
    assert baseline is not None
    assert baseline.shared_wave_count == 1
