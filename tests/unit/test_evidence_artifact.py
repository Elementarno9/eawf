"""Unit tests for :mod:`eawf.workflow.evidence.artifact`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import state_transaction
from eawf.workflow.evidence import _io, artifact

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def test_add_artifact_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
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
    state = _io.load_state(state_path)
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
    state = _io.load_state(state_path)
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
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="ART-999"):
        artifact.show_artifact(state, "ART-999")


def test_update_artifact_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    artifact.add_artifact(
        state,
        artifact_id="ART-100",
        kind="audit_report",
        uri="repo:.ea/artifacts/audit-100.md",
        scope_id="QR",
        sha256="a" * 64,
        size_bytes=1024,
    )
    new_sha = "b" * 64
    event = artifact.update_artifact(state, artifact_id="ART-100", sha256=new_sha, size_bytes=2048)
    a = state.artifacts["ART-100"]
    assert a.sha256 == new_sha
    assert a.size_bytes == 2048
    assert a.urn == "urn:eawf:v1:artifact:QR/ART-100"
    assert event.payload["event_type"] == "artifact.update"


def test_update_artifact_uri_only(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    artifact.add_artifact(
        state,
        artifact_id="ART-101",
        kind="audit_report",
        uri="repo:.ea/artifacts/audit-101.md",
        scope_id="QR",
    )
    artifact.update_artifact(state, artifact_id="ART-101", uri="repo:.ea/artifacts/renamed.md")
    assert state.artifacts["ART-101"].uri == "repo:.ea/artifacts/renamed.md"
    assert state.artifacts["ART-101"].sha256 is None


def test_update_artifact_unknown_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="ART-999"):
        artifact.update_artifact(state, artifact_id="ART-999", sha256="c" * 64)


def test_update_artifact_no_fields_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    artifact.add_artifact(
        state,
        artifact_id="ART-102",
        kind="audit_report",
        uri="repo:.ea/artifacts/x.md",
        scope_id="QR",
    )
    with pytest.raises(cli_errors.UserError, match="at least one"):
        artifact.update_artifact(state, artifact_id="ART-102")


def test_update_artifact_rejects_absolute_uri(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    artifact.add_artifact(
        state,
        artifact_id="ART-103",
        kind="audit_report",
        uri="repo:.ea/artifacts/x.md",
        scope_id="QR",
    )
    with pytest.raises(cli_errors.UserError, match="file://"):
        artifact.update_artifact(state, artifact_id="ART-103", uri="/etc/passwd")


def test_state_transaction_persists_add_artifact(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        event = artifact.add_artifact(
            state, artifact_id="ART-001", kind="audit_report", uri="repo:foo.md", scope_id="QR"
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["artifacts"]["ART-001"]["urn"] == "urn:eawf:v1:artifact:QR/ART-001"
    event_lines = paths[StoreKind.EVENT].read_text().splitlines()
    assert len(event_lines) == 1
    payload = json.loads(event_lines[0])
    assert payload["payload"]["event_type"] == "artifact.add"
    assert payload["artifact_ids"] == ["ART-001"]
