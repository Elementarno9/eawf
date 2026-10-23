"""The v1 ``State`` document model: its root keys, schema versions and datetimes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.migration.v1_state import State
from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    BacklogItem,
    CurrentPointers,
    Iter,
    Phase,
    Project,
    RuntimeLatest,
    Wave,
)
from tests.conftest import make_intent
from tests.fixtures.migration.epoch1_seed import seed_iter, seed_phase, seed_wave


def test_state_root_with_no_optional_keys() -> None:
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-05-08T00:00:00Z",
            "project": {
                "code": "QR",
                "slug": "quant-research",
                "title": "Quant Research",
                "description": "",
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
    )
    assert state.scope_kind == "repo"
    assert state.tracks is None or state.tracks == {}


def test_state_schema_version_literal() -> None:
    with pytest.raises(ValidationError):
        State.model_validate(
            {
                "schema_version": "2.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:QR",
                "updated_at": "2026-05-08T00:00:00Z",
                "project": None,
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
        )


def test_decisions_default_factory_dict() -> None:
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-05-08T00:00:00Z",
            "project": None,
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
    )
    assert state.decisions == {}


def _minimal_state_payload(updated_at: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": updated_at,
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
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


def test_naive_datetime_rejected_in_state_root() -> None:
    payload = _minimal_state_payload("2026-05-08T00:00:00")  # no tz
    with pytest.raises(ValidationError) as excinfo:
        State.model_validate(payload)
    assert "timezone-aware" in str(excinfo.value)


def test_offset_datetime_normalised_to_utc() -> None:
    payload = _minimal_state_payload("2026-01-01T00:00:00+05:00")
    state = State.model_validate(payload)
    assert state.updated_at.tzinfo is UTC
    expected = datetime(2025, 12, 31, 19, 0, 0, tzinfo=UTC)
    assert state.updated_at == expected


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
    seed_phase(state, phase_id="P01", title="Bootstrap")
    seed_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    seed_wave(
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


def test_project_schema_version_pin_unchanged() -> None:
    """Adding an optional field must NOT bump State.schema_version.

    P20-I01-W09 spec: keep weekly_eu_target strictly optional (default
    None) so the schema_version literal stays at "1.0".
    """
    state = _empty_state()
    assert state.schema_version == "1.0"


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


def test_principal_state_schema_version_unchanged() -> None:
    """Adding the Principal class must NOT bump State.schema_version.

    Principal is a placeholder model — not yet referenced from State —
    so the schema literal stays at "1.0" per c01-foundations §5.3.19.
    """
    state = _empty_state()
    assert state.schema_version == "1.0"


def _now() -> datetime:
    return datetime.now(UTC)


def test_state_validates_without_intent_field_on_entities() -> None:
    """A payload mirroring the pre-W11 wire format (no `intent` keys) loads."""
    payload = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
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
        "phases": {
            "P01": Phase(
                id="P01",
                scope_id="QR",
                title="t",
                status=PhaseStatus.ACTIVE,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "iters": {
            "P01-I01": Iter(
                id="P01-I01",
                phase_id="P01",
                title="t",
                status=IterStatus.ACTIVE,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "waves": {
            "P01-I01-W01": Wave(
                id="P01-I01-W01",
                iter_id="P01-I01",
                title="t",
                status=WaveStatus.PENDING,
                opened_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "backlog": {
            "B001": BacklogItem(
                id="B001",
                scope_id="QR",
                title="t",
                priority=BacklogPriority.P2,
                status=BacklogStatus.OPEN,
                created_at=_now(),
            ).model_dump(mode="json", exclude={"intent"}),
        },
    }
    state = State.model_validate(payload)
    assert state.waves["P01-I01-W01"].intent is None
    assert state.iters["P01-I01"].intent is None
    assert state.phases["P01"].intent is None
    assert state.backlog is not None
    assert state.backlog["B001"].intent is None


def test_state_schema_version_accepts_supported_set() -> None:
    """The accepted schema-version set is the migrate-chain supported range."""
    from typing import get_args

    from eawf.kernel.migration.v1_state import State as StateModel

    field = StateModel.model_fields["schema_version"]
    versions = set(get_args(field.annotation))
    assert versions == {
        "1.0",
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "1.10",
        "1.11",
        "1.12",
        "1.13",
        "1.14",
        "1.15",
        "1.16",
        "1.17",
        "1.18",
        "1.19",
        "1.20",
    }
