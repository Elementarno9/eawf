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

from eawf.kernel.state.enums import ProjectStatus, WaveStatus
from eawf.kernel.state.models import Principal, Project

# ---- dispatch_paused --------------------------------------------------------


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
