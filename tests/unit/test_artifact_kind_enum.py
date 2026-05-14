"""Unit tests for the ``ArtifactKind`` enum (P14-W11 / B059)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.state.enums import ArtifactKind
from eawf.state.models import Artifact


def _make_artifact(kind: str | ArtifactKind) -> Artifact:
    return Artifact(
        id="ART-test",
        kind=kind,  # type: ignore[arg-type]
        uri="repo:artifacts/x.md",
        urn="urn:eawf:v1:artifact:DEMO/ART-test",
        sha256=None,
        size_bytes=None,
        created_at=datetime.now(UTC),
        metadata={},
    )


def test_enum_accepts_canonical_string_value() -> None:
    art = _make_artifact("audit_report")
    assert art.kind == ArtifactKind.AUDIT_REPORT


def test_enum_accepts_member_directly() -> None:
    art = _make_artifact(ArtifactKind.NOTEBOOK)
    assert art.kind == ArtifactKind.NOTEBOOK


def test_enum_construction_rejects_unknown_value() -> None:
    """The ``ArtifactKind`` enum itself is closed; constructing from an
    unknown literal raises ``ValueError`` even though the Artifact model
    still accepts free-form ``str`` kinds in v0.3 (strict-enum
    tightening lands in v0.4 once internal callers migrate).
    """
    with pytest.raises(ValueError):
        ArtifactKind("not-a-real-kind")


def test_artifact_model_still_accepts_free_string() -> None:
    """Non-canonical strings remain accepted in v0.3 for back-compat."""
    art = _make_artifact("review_findings")
    assert art.kind == "review_findings"


def test_enum_covers_b059_vocabulary() -> None:
    expected = {
        "audit_report",
        "notebook",
        "dataset",
        "model",
        "backtest",
        "strategy",
        "binary",
        "scene",
        "playtest_session",
        "cve_ref",
        "research_brief",
        "plan_spec",
        "agent_report",
    }
    assert {k.value for k in ArtifactKind} == expected


def test_enum_serialises_to_string_value() -> None:
    art = _make_artifact(ArtifactKind.DATASET)
    payload = art.model_dump(mode="json")
    assert payload["kind"] == "dataset"
