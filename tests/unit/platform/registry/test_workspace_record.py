"""Strictness tests for :class:`eawf.platform.registry.WorkspaceRecord`.

The record is the shape every qualified URN is minted against, so it is
validated at the boundary rather than at mint time. These tests pin the
three refusals that matter - an unknown key, an empty membership, and a
home repo outside the membership - plus the backward-compatibility
clause: a version-1 registry written before workspaces existed still
loads, with an empty ``workspaces`` mapping.

Every read here targets a ``tmp_path`` file or a checked-in fixture;
nothing touches the operator's real ``~/.eawf/registry.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from eawf.platform.registry import Registry, RegistryReadError, WorkspaceRecord, read_registry

pytestmark = pytest.mark.unit


#: ``tests/fixtures/registry`` - four levels up lands on ``tests/``.
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "registry"


def _record(**overrides: object) -> WorkspaceRecord:
    """Build a valid record, overriding named fields."""
    fields: dict[str, object] = {
        "key": "MONO",
        "title": "Mono",
        "member_project_codes": frozenset({"EAWF", "DEMO"}),
        "home_project_code": "EAWF",
    }
    fields.update(overrides)
    return WorkspaceRecord(**fields)  # type: ignore[arg-type]


# ---- happy path ------------------------------------------------------------


def test_workspace_record_accepts_minimal_membership() -> None:
    record = _record(member_project_codes=frozenset({"EAWF"}))
    assert record.member_project_codes == frozenset({"EAWF"})
    assert record.home_project_code == "EAWF"
    assert record.revision == 1
    assert record.title == "Mono"


def test_workspace_record_serialises_members_sorted() -> None:
    record = _record(
        member_project_codes=frozenset({"ZED", "ABC", "MID"}),
        home_project_code="MID",
    )
    assert record.model_dump(mode="json")["member_project_codes"] == ["ABC", "MID", "ZED"]


def test_workspace_record_round_trips_through_json() -> None:
    record = _record(revision=7, updated_at=datetime(2026, 5, 1, tzinfo=UTC))
    reloaded = WorkspaceRecord.model_validate(record.model_dump(mode="json"))
    assert reloaded == record


# ---- error paths -----------------------------------------------------------


def test_workspace_record_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        WorkspaceRecord.model_validate(
            {
                "key": "MONO",
                "member_project_codes": ["EAWF"],
                "home_project_code": "EAWF",
                "owner": "someone",
            }
        )
    assert "owner" in str(excinfo.value)
    assert "Extra inputs are not permitted" in str(excinfo.value)


def test_workspace_record_rejects_empty_membership() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _record(member_project_codes=frozenset())
    assert "member_project_codes" in str(excinfo.value)


def test_workspace_record_rejects_home_outside_membership() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _record(member_project_codes=frozenset({"DEMO"}), home_project_code="EAWF")
    assert "home_project_code" in str(excinfo.value)


def test_workspace_record_rejects_non_project_code_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _record(key="lower-case")
    assert "not a project code" in str(excinfo.value)


def test_workspace_record_rejects_non_project_code_member() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _record(member_project_codes=frozenset({"EAWF", "bad code"}))
    assert "not project codes" in str(excinfo.value)


def test_workspace_record_rejects_zero_revision() -> None:
    with pytest.raises(ValidationError):
        _record(revision=0)


def test_workspace_record_rejects_missing_home() -> None:
    with pytest.raises(ValidationError) as excinfo:
        WorkspaceRecord.model_validate({"key": "MONO", "member_project_codes": ["EAWF"]})
    assert "home_project_code" in str(excinfo.value)


# ---- registry-level invariants ---------------------------------------------


def test_registry_rejects_workspace_under_mismatched_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Registry.model_validate(
            {
                "workspaces": {
                    "OTHER": {
                        "key": "MONO",
                        "member_project_codes": ["EAWF"],
                        "home_project_code": "EAWF",
                    }
                }
            }
        )
    assert "mismatched key" in str(excinfo.value)


def test_registry_defaults_workspaces_to_empty() -> None:
    assert Registry().workspaces == {}


# ---- loader compatibility --------------------------------------------------


def test_read_registry_accepts_version_1_without_workspaces(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_bytes((FIXTURES / "version1_no_workspaces.json").read_bytes())
    registry = read_registry(path=target)
    assert registry.version == "1"
    assert sorted(registry.repos) == ["DEMO", "EAWF"]
    assert registry.workspaces == {}


def test_read_registry_loads_workspace_records(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_bytes((FIXTURES / "version1_with_workspaces.json").read_bytes())
    registry = read_registry(path=target)
    assert registry.workspaces["MONO"].member_project_codes == frozenset({"DEMO", "EAWF"})
    assert registry.workspaces["MONO"].revision == 3


def test_read_registry_rejects_invalid_workspace_record(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "workspaces": {
                    "MONO": {
                        "key": "MONO",
                        "member_project_codes": [],
                        "home_project_code": "EAWF",
                    }
                },
            }
        )
    )
    with pytest.raises(RegistryReadError) as excinfo:
        read_registry(path=target)
    assert "invalid registry schema" in str(excinfo.value)
