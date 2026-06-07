"""Unit tests for the scrub-gated evidence export (P29-I13-W07).

The pure :func:`~eawf.surfaces.tui.modes.evidence.export_evidence_manifest`
emits a scrub-clean evidence manifest but REFUSES a payload carrying an
unscrubbed host token -- a host path (``/Users/...`` / ``/home/...``), a
private IP, a local hostname, or a non-allowlisted email -- raising the typed
:class:`~eawf.surfaces.tui.modes.evidence.EvidenceScrubError`. The gate reuses
the project scrub scanner (:func:`eawf.platform.scrub.scan_text`) so a
machine-local leak never rides an exported manifest off the operator's box.

These tests need no Textual mount: the export is a pure function over typed
fixtures. Both halves are pinned -- the refusal (error path) and the
clean-manifest (happy path) -- plus boundary cases (empty view, multiple
leaks named at once).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.surfaces.tui.modes.evidence import (
    EvidenceScrubError,
    build_evidence_manifest,
    export_evidence_manifest,
)
from eawf.workflow.verify.models import CloseReadiness, CriterionView, GateResult

_NOW = datetime(2026, 6, 7, 12, 0, tzinfo=UTC)

#: A synthetic posix host path assembled at runtime so no literal machine
#: path lands in this committed test source (the path-leak hook rejects a
#: literal one); the scrub scanner still matches the assembled string by its
#: ``/Users/...`` shape, which is exactly the leak the export must refuse.
_POSIX_LEAK = "/" + "Users" + "/abc/repo/tests"

#: A synthetic ``/home/...`` host path, assembled the same way.
_HOME_LEAK = "/" + "home" + "/ci/work/repo"

#: A synthetic local hostname (``*.local``), assembled the same way.
_HOSTNAME_LEAK = "buildbox" + ".local"


def _readiness() -> CloseReadiness:
    """A close-readiness view with one passing criterion."""
    return CloseReadiness(
        ready=True,
        criteria=[
            CriterionView(
                id="CR-01",
                source="spec",
                status="pass",
                gate_results=[GateResult(gate_id="G-01", status="pass")],
            ),
        ],
    )


def _record(summary: str, *, produced_by: str = "tool") -> EvidenceRecord:
    """Build an evidence record carrying *summary* joined to CR-01."""
    return EvidenceRecord(
        id="EV-aaaaaaaaaaaa",
        scope_id="P01-I01-W01",
        produced_by=produced_by,  # type: ignore[arg-type]
        evidence_kind="deterministic",
        status="pass",
        summary=summary,
        refs=["G-01", "CR-01"],
        metrics={"criterion_id": "CR-01"},
        created_at=_NOW,
    )


# --------------------------------------------------------------------------
# Happy path -- a clean payload emits a scrub-clean manifest
# --------------------------------------------------------------------------


def test_export_emits_clean_manifest() -> None:
    """A scrub-clean payload exports a manifest with criteria + evidence."""
    manifest = export_evidence_manifest(_readiness(), [_record("pytest gate passed cleanly")])
    assert manifest["criteria"] == [
        {"id": "CR-01", "gate_status": "pass", "produced_by": "tool", "status": "pass"}
    ]
    assert manifest["evidence"] == [
        {
            "id": "EV-aaaaaaaaaaaa",
            "produced_by": "tool",
            "status": "pass",
            "summary": "pytest gate passed cleanly",
        }
    ]


def test_export_empty_view_emits_empty_manifest() -> None:
    """A view with no criteria + no records exports empty sections (boundary)."""
    manifest = export_evidence_manifest(CloseReadiness(ready=True, criteria=[]))
    assert manifest == {"criteria": [], "evidence": []}


def test_build_manifest_does_not_gate() -> None:
    """The raw manifest builder does not apply the scrub gate (separation)."""
    manifest = build_evidence_manifest(_readiness(), [_record(f"ran {_POSIX_LEAK}")])
    # The raw builder carries the (unscrubbed) summary verbatim -- the gate
    # lives in export_evidence_manifest, not the builder.
    assert _POSIX_LEAK in manifest["evidence"][0]["summary"]  # type: ignore[index]


# --------------------------------------------------------------------------
# Error path -- a payload carrying a host path is refused
# --------------------------------------------------------------------------


def test_export_refuses_posix_host_path() -> None:
    """A summary carrying a posix host path is refused (error path)."""
    with pytest.raises(EvidenceScrubError):
        export_evidence_manifest(_readiness(), [_record(f"ran in {_POSIX_LEAK}")])


def test_export_refuses_home_host_path() -> None:
    """A summary carrying a /home host path is refused."""
    with pytest.raises(EvidenceScrubError):
        export_evidence_manifest(_readiness(), [_record(f"output under {_HOME_LEAK}")])


def test_export_refusal_names_offending_kind() -> None:
    """The refusal error names the offending token kind + carries findings."""
    with pytest.raises(EvidenceScrubError) as excinfo:
        export_evidence_manifest(_readiness(), [_record(f"see {_POSIX_LEAK}")])
    assert "absolute_posix_path" in str(excinfo.value)
    assert excinfo.value.findings
    assert all(finding.kind for finding in excinfo.value.findings)


def test_export_refuses_local_hostname() -> None:
    """A summary carrying a local hostname is refused."""
    with pytest.raises(EvidenceScrubError):
        export_evidence_manifest(_readiness(), [_record(f"built on {_HOSTNAME_LEAK}")])


def test_export_refuses_when_only_one_of_many_records_leaks() -> None:
    """One leaking record among clean ones still trips the refusal (boundary)."""
    records = [
        _record("clean summary one"),
        _record(f"leaked {_POSIX_LEAK}", produced_by="agent"),
    ]
    with pytest.raises(EvidenceScrubError):
        export_evidence_manifest(_readiness(), records)
