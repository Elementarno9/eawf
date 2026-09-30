"""Unit tests for state-model derived accessors (P20-W15 / B026).

Covers :mod:`eawf.kernel.state.wave_graph` — the typed Wave DAG edge views
(``deps``, ``blocks``, ``blocked_by``, ``edges``, ``edges_for_iter``)
that consumers (the TUI wave-board in W03, the ``eawf wave graph``
CLI) read off the model in O(1) per wave from a single call site.

The DAG persistence layer's mutation behaviour (``plan_wave``,
``set_wave_deps``, ``remove_wave_plan``) is exercised separately in
:mod:`tests.unit.test_wave_dag`.

Also covers :class:`~eawf.kernel.state.models.Project.weekly_eu_target` field
contract: None default, valid float accepted, invalid
type rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Principal, Project, RuntimeLatest, State
from eawf.workflow.lifecycle.transitions import (
    open_iter,
    open_phase,
    plan_wave,
)
from tests.conftest import make_claim_criterion, make_intent


def _empty_state() -> State:
    return State.model_validate(
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


def _seed_chain() -> State:
    """Seed a linear chain W01 -> W02 -> W03 under P01-I01."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="A",
        file_scopes=["src/"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="B",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="C",
        file_scopes=["src/"],
        deps=["P01-I01-W02"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    return state


def _seed_diamond() -> State:
    """Seed a diamond W01 -> {W02, W03} -> W04 under P01-I01."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="A",
        file_scopes=["src/"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="B",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W03",
        iter_id="P01-I01",
        title="C",
        file_scopes=["src/"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W04",
        iter_id="P01-I01",
        title="D",
        file_scopes=["src/"],
        deps=["P01-I01-W02", "P01-I01-W03"],
        effort_bucket="M",
        success_criteria=[make_claim_criterion()],
        intent=make_intent(),
    )
    return state


# ---- dispatch_paused --------------------------------------------------------


def test_state_dispatch_paused_defaults_false() -> None:
    """``dispatch_paused`` defaults to ``False`` when omitted from the payload."""
    state = _empty_state()
    assert state.dispatch_paused is False


def test_state_accepts_explicit_dispatch_paused_true() -> None:
    """``State`` accepts an explicit ``dispatch_paused=True`` and round-trips it."""
    state = _empty_state()
    state.dispatch_paused = True
    reloaded = State.model_validate(state.model_dump(mode="json"))
    assert reloaded.dispatch_paused is True


def test_state_rejects_non_bool_dispatch_paused() -> None:
    """A non-bool ``dispatch_paused`` is rejected at the ingestion boundary."""
    payload = _empty_state().model_dump(mode="json")
    payload["dispatch_paused"] = "not-a-bool"
    with pytest.raises(ValidationError):
        State.model_validate(payload)


def test_wave_runtime_latest_defaults_none_and_forbids_extra_keys() -> None:
    """``Wave.runtime_latest`` is optional and strictly shaped when present."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="A",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )

    assert state.waves["P01-I01-W01"].runtime_latest is None
    with pytest.raises(ValidationError):
        RuntimeLatest.model_validate(
            {"api_duration_ms": 1, "captured_at": datetime.now(UTC).isoformat(), "extra": 1}
        )


# ---- deps -------------------------------------------------------------------


# ---- blocks -----------------------------------------------------------------


# ---- blocked_by (live runtime view) ----------------------------------------


# ---- edges (typed single-call accessor) -------------------------------------


# ---- edges_for_iter ---------------------------------------------------------


# ---- Project.weekly_eu_target ---------------------------------


def _project_kwargs() -> dict[str, object]:
    """Minimal required-only kwargs for :class:`Project` construction."""
    return {
        "code": "QR",
        "slug": "qr",
        "title": "QR",
        "domains": ["x"],
        "default_branch": "main",
        "status": ProjectStatus.ACTIVE,
        "repo_urn": "urn:eawf:v1:repo:QR",
    }


def test_project_weekly_eu_target_defaults_none() -> None:
    """weekly_eu_target defaults to None when the field is omitted.

    Constructing a Project with only the required fields leaves the new
    P20-I01-W09 field unset; consumers (the TUI footer) treat that as
    "no burn line".
    """
    project = Project(**_project_kwargs())  # type: ignore[arg-type]
    assert project.weekly_eu_target is None


def test_project_weekly_eu_target_accepts_positive_float() -> None:
    """A positive float is accepted verbatim and round-trips through JSON."""
    project = Project(**_project_kwargs(), weekly_eu_target=8.5)  # type: ignore[arg-type]
    assert project.weekly_eu_target == pytest.approx(8.5)
    # Round-trip preserves the field.
    again = Project.model_validate_json(project.model_dump_json())
    assert again.weekly_eu_target == pytest.approx(8.5)


def test_project_weekly_eu_target_accepts_zero() -> None:
    """Zero is a legal value (operator-pinned target, no burn allowed)."""
    project = Project(**_project_kwargs(), weekly_eu_target=0.0)  # type: ignore[arg-type]
    assert project.weekly_eu_target == 0.0


def test_project_weekly_eu_target_rejects_string() -> None:
    """A non-numeric value is rejected at validation time."""
    with pytest.raises(ValidationError):
        Project(**_project_kwargs(), weekly_eu_target="lots")  # type: ignore[arg-type]


def test_project_weekly_eu_target_rejects_list() -> None:
    """A list payload is rejected — the field is scalar float|None only."""
    with pytest.raises(ValidationError):
        Project(**_project_kwargs(), weekly_eu_target=[1.0, 2.0])  # type: ignore[arg-type]


def test_project_extra_field_still_forbidden() -> None:
    """ConfigDict(extra='forbid') stays intact after the new field added.

    Regression: adding a field must not accidentally relax the strict
    config — unknown keys must still be rejected.
    """
    with pytest.raises(ValidationError):
        Project(**_project_kwargs(), bogus=1.0)  # type: ignore[arg-type,call-arg]


def test_project_schema_version_pin_unchanged() -> None:
    """Adding an optional field must NOT bump State.schema_version.

    P20-I01-W09 spec: keep weekly_eu_target strictly optional (default
    None) so the schema_version literal stays at "1.0".
    """
    state = _empty_state()
    assert state.schema_version == "1.0"


# ---------------------------------------------------------------------------
# Wave.claimed_at — work-start anchor
# ---------------------------------------------------------------------------


def _wave_payload(**overrides: object) -> dict[str, object]:
    """Return a minimal valid Wave payload dict, mergeable with overrides."""
    base: dict[str, object] = {
        "id": "P01-I01-W01",
        "iter_id": "P01-I01",
        "title": "w",
        "status": WaveStatus.PENDING.value,
        "opened_at": datetime.now(UTC).isoformat(),
    }
    base.update(overrides)
    return base


def test_wave_claimed_at_defaults_to_none() -> None:
    """``claimed_at`` is optional and defaults to None at plan/creation time."""
    from eawf.kernel.state.models import Wave

    wave = Wave.model_validate(_wave_payload())
    assert wave.claimed_at is None


def test_wave_claimed_at_round_trips_when_set() -> None:
    """A set ``claimed_at`` survives a model_validate / model_dump round-trip."""
    from eawf.kernel.state.models import Wave

    stamped = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
    wave = Wave.model_validate(_wave_payload(claimed_at=stamped.isoformat()))
    assert wave.claimed_at == stamped
    reloaded = Wave.model_validate(wave.model_dump(mode="json"))
    assert reloaded.claimed_at == stamped


def test_wave_without_claimed_at_key_loads() -> None:
    """An old wave dict that predates the field loads with claimed_at None.

    The field is additive + optional, so on-disk state written before the
    v1.3 bump re-validates unchanged (no migration required to load).
    """
    from eawf.kernel.state.models import Wave

    payload = _wave_payload()
    assert "claimed_at" not in payload
    wave = Wave.model_validate(payload)
    assert wave.claimed_at is None


def test_state_accepts_schema_version_1_3() -> None:
    """The State model accepts the v1.3 ``schema_version`` literal."""
    payload = _empty_state().model_dump(mode="json")
    payload["schema_version"] = "1.3"
    state = State.model_validate(payload)
    assert state.schema_version == "1.3"


def test_state_accepts_schema_version_1_4() -> None:
    """The State model accepts the v1.4 ``schema_version`` literal (candidate_tag bump)."""
    payload = _empty_state().model_dump(mode="json")
    payload["schema_version"] = "1.4"
    state = State.model_validate(payload)
    assert state.schema_version == "1.4"


def test_state_accepts_schema_version_1_9() -> None:
    """The State model accepts the v1.9 ``schema_version`` literal."""
    payload = _empty_state().model_dump(mode="json")
    payload["schema_version"] = "1.9"
    state = State.model_validate(payload)
    assert state.schema_version == "1.9"


# ---------------------------------------------------------------------------
# Principal (C01-IMPL W02 placeholder — c01-foundations §5.3.19 + Q3 2026-05-18)
# ---------------------------------------------------------------------------


def test_principal_operator_kind_valid() -> None:
    p = Principal(id="u-abc12345", kind="operator", display_name="Alice")
    assert p.id == "u-abc12345"
    assert p.kind == "operator"
    assert p.display_name == "Alice"


def test_principal_agent_kind_valid() -> None:
    p = Principal(id="u-12345678", kind="agent", display_name="executor-bot")
    assert p.kind == "agent"


def test_principal_cli_kind_valid() -> None:
    """The 'cli' kind is the legacy CLI-dispatch sentinel per c01-foundations §5.3.19."""
    p = Principal(id="u-00000000", kind="cli", display_name="cli")
    assert p.kind == "cli"


@pytest.mark.parametrize(
    "bad_id",
    ["abc12345", "u-ABC12345", "u-abc1234", "u-abc123456", "u-xyz12345", ""],
)
def test_principal_rejects_invalid_id_pattern(bad_id: str) -> None:
    """Principal.id MUST match ``^u-[0-9a-f]{8}$`` per c01-foundations §5.3.19."""
    with pytest.raises(ValidationError):
        Principal(id=bad_id, kind="operator", display_name="x")


def test_principal_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Principal(id="u-abc12345", kind="admin", display_name="x")  # type: ignore[arg-type]


def test_principal_strict_forbids_extra_field() -> None:
    with pytest.raises(ValidationError):
        Principal(
            id="u-abc12345",
            kind="operator",
            display_name="x",
            email="x@example.com",  # type: ignore[call-arg]
        )


def test_principal_state_schema_version_unchanged() -> None:
    """Adding the Principal class must NOT bump State.schema_version.

    Principal is a placeholder model — not yet referenced from State —
    so the schema literal stays at "1.0" per c01-foundations §5.3.19.
    """
    state = _empty_state()
    assert state.schema_version == "1.0"
