"""Unit tests for :mod:`eawf.workflow.evidence.artifact`."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.evidence import artifact
from tests._contract_view import contract_view

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def test_add_artifact_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = contract_view(state_path)
    event = artifact.add_artifact(
        state,
        artifact_id="ART-001",
        kind="audit_report",
        uri="repo:.ea/artifacts/audit-001.md",
        scope_id="QR",
        sha256="a" * 64,
        size_bytes=1024,
    )
    a = state.artifacts["ART-001"]
    assert a.kind == "audit_report"
    assert a.urn == "urn:eawf:v1:artifact:QR/ART-001"
    assert a.sha256 == "a" * 64
    assert a.size_bytes == 1024
    assert event.payload["event_type"] == "artifact.add"


def test_add_artifact_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = contract_view(state_path)
    artifact.add_artifact(
        state, artifact_id="ART-001", kind="audit_report", uri="repo:foo.md", scope_id="QR"
    )
    with pytest.raises(cli_errors.UserError, match="already exists"):
        artifact.add_artifact(
            state, artifact_id="ART-001", kind="audit_report", uri="repo:bar.md", scope_id="QR"
        )


@pytest.mark.parametrize(
    "uri",
    [
        "file:///tmp/report.md",
        "/tmp/report.md",
        "C:\\Users\\name\\report.md",
    ],
)
def test_add_artifact_rejects_local_uri(tmp_path: Path, uri: str) -> None:
    state_path = _state_path(tmp_path)
    state = contract_view(state_path)
    with pytest.raises(cli_errors.UserError, match="uri must not"):
        artifact.add_artifact(
            state,
            artifact_id="ART-001",
            kind="audit_report",
            uri=uri,
            scope_id="QR",
        )


def test_artifact_model_has_no_local_path_field() -> None:
    """Field removed; persisted state shape must not carry local_path."""
    from eawf.kernel.state.models import Artifact

    assert "local_path" not in Artifact.model_fields


def test_show_artifact_unknown_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = contract_view(state_path)
    with pytest.raises(cli_errors.UserError, match="ART-999"):
        artifact.show_artifact(state, "ART-999")
