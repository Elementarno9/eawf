"""Runtime-tuple health lines, and the run each one is repeating.

Doctor measures nothing about a runtime tuple itself, so every check in
this slice has to name the run behind it: the producer verb, the stage it
reached, and the artifact that stage filed its evidence under. The typed
model carries that obligation rather than a convention -- a check named
under the runtime-tuple prefix with no provenance fails to construct.

The grading is read off the newest stage the store holds. A quarantine is
a failed rollback row and grades ``fail`` under the quarantine verb; the
passed rollback that walks the tuple back grades ``ok`` under the
rollback verb; a refusal grades ``warn``, because a stage that refused
proves nothing either way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import CertificationFailureCode, ConformanceStageRecord
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.observability.doctor import checks
from eawf.observability.doctor.models import (
    RUNTIME_TUPLE_CHECK_PREFIX,
    CheckResult,
    HealthProvenance,
)
from eawf.observability.doctor.runtime_health import run_runtime_tuple_health_checks
from eawf.runtime.daemon.methods.conformance import StoreStageJournal

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
EVIDENCE_REF = "artifact://conformance/codex-2026-09-17"
TUPLE_A = f"sha256:{'a' * 64}"
TUPLE_B = f"sha256:{'b' * 64}"


def record(stage: str, **overrides: Any) -> ConformanceStageRecord:
    """Return one passed stage record of *stage*."""
    document: dict[str, Any] = {
        "stage": stage,
        "outcome": "passed",
        "evidence_ref": EVIDENCE_REF,
        "started_at": NOW,
        "completed_at": NOW,
    }
    document.update(overrides)
    return ConformanceStageRecord.model_validate(document)


def write(workspace: Path, rows: list[tuple[str, ConformanceStageRecord]]) -> None:
    """Append *rows* to the tree's conformance journal, in order."""
    journal = StoreStageJournal(workspace / ".ea" / "state.json")
    for tuple_digest, row in rows:
        journal.append(tuple_digest=tuple_digest, record=row)


def only(workspace: Path) -> CheckResult:
    """Return the single runtime-tuple check of *workspace*."""
    results = run_runtime_tuple_health_checks(workspace=workspace)
    assert len(results) == 1
    return results[0]


# ---- every check names its producer, stage and evidence ---------------------


def test_check_carries_the_stage_and_evidence_of_the_newest_record(tmp_path: Path) -> None:
    """The provenance repeats the newest stage the store holds for the tuple."""
    write(tmp_path, [(TUPLE_A, record("probe")), (TUPLE_A, record("canary"))])

    result = only(tmp_path)

    assert result.provenance is not None
    assert result.provenance.stage == "canary"
    assert result.provenance.evidence_ref == EVIDENCE_REF
    assert result.name == f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'a' * 12}"


@pytest.mark.parametrize(
    ("stage", "producer"),
    [
        ("probe", "conformance.probe"),
        ("canary", "conformance.canary"),
        ("certify", "conformance.certify"),
        ("rollback", "conformance.rollback"),
    ],
)
def test_each_passed_stage_names_the_verb_that_wrote_it(
    tmp_path: Path, stage: str, producer: str
) -> None:
    """A passed stage is attributed to the verb that runs that stage."""
    write(tmp_path, [(TUPLE_A, record(stage))])

    result = only(tmp_path)

    assert result.provenance is not None
    assert result.provenance.producer == producer
    assert result.status == "ok"


def test_a_failed_rollback_is_attributed_to_the_quarantine_verb(tmp_path: Path) -> None:
    """A quarantine is the failed rollback row, so the producer is quarantine."""
    write(
        tmp_path,
        [
            (
                TUPLE_A,
                record(
                    "rollback",
                    outcome="failed",
                    reason_code=CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
                ),
            )
        ],
    )

    result = only(tmp_path)

    assert result.provenance is not None
    assert result.provenance.producer == "conformance.quarantine"
    assert result.status == "fail"
    assert "quarantined" in (result.detail or "")
    assert "containment_probe_escape" in (result.detail or "")


def test_a_rollback_after_a_quarantine_grades_the_tuple_back_to_ok(tmp_path: Path) -> None:
    """The passed rollback row is the tuple returning to service."""
    write(
        tmp_path,
        [
            (
                TUPLE_A,
                record(
                    "rollback",
                    outcome="failed",
                    reason_code=CertificationFailureCode.AUTH_KIND_MISMATCH,
                ),
            ),
            (TUPLE_A, record("certify")),
        ],
    )

    result = only(tmp_path)

    assert result.status == "ok"
    assert result.provenance is not None
    assert result.provenance.producer == "conformance.certify"


def test_a_refused_stage_grades_as_a_warning(tmp_path: Path) -> None:
    """A refusal proves nothing about the tuple, so it warns rather than fails."""
    write(
        tmp_path,
        [
            (
                TUPLE_A,
                record(
                    "canary",
                    outcome="refused",
                    reason_code=CertificationFailureCode.SCHEMA_MISMATCH,
                ),
            )
        ],
    )

    result = only(tmp_path)

    assert result.status == "warn"
    assert "schema_mismatch" in (result.detail or "")


def test_one_check_per_tuple_ordered_by_digest(tmp_path: Path) -> None:
    """Two tuples yield two checks, ordered so the table never reshuffles."""
    write(tmp_path, [(TUPLE_B, record("probe")), (TUPLE_A, record("certify"))])

    results = run_runtime_tuple_health_checks(workspace=tmp_path)

    assert [result.name for result in results] == [
        f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'a' * 12}",
        f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'b' * 12}",
    ]


# ---- boundaries: nothing to report is not a healthy tuple -------------------


def test_no_anchor_yields_no_checks() -> None:
    """Boundary: doctor without an anchor reports on no tuple."""
    assert run_runtime_tuple_health_checks(workspace=None) == []


def test_a_tree_with_no_conformance_store_yields_no_checks(tmp_path: Path) -> None:
    """Boundary: an unprobed tree has no tuple to grade."""
    assert run_runtime_tuple_health_checks(workspace=tmp_path) == []


def test_an_empty_conformance_store_yields_no_checks(tmp_path: Path) -> None:
    """Boundary: an empty store is absent evidence, never an ok row."""
    path = store_path(tmp_path / ".ea" / "state.json", StoreKind.CONFORMANCE_STAGE)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")

    assert run_runtime_tuple_health_checks(workspace=tmp_path) == []


def test_a_malformed_line_is_skipped_rather_than_raised_on(tmp_path: Path) -> None:
    """One broken row must not take the whole doctor surface down."""
    write(tmp_path, [(TUPLE_A, record("certify"))])
    path = store_path(tmp_path / ".ea" / "state.json", StoreKind.CONFORMANCE_STAGE)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")

    assert len(run_runtime_tuple_health_checks(workspace=tmp_path)) == 1


def test_an_envelope_of_another_kind_is_skipped(tmp_path: Path) -> None:
    """A row filed under another kind is not a stage record of any tuple."""
    write(tmp_path, [(TUPLE_A, record("certify"))])
    path = store_path(tmp_path / ".ea" / "state.json", StoreKind.CONFORMANCE_STAGE)
    foreign = Envelope(
        id="EV-1",
        kind=StoreKind.EVENT,
        scope_id=TUPLE_B,
        created_at=NOW,
        summary="not a stage",
        payload={"stage": "probe"},
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{foreign.model_dump_json()}\n")

    results = run_runtime_tuple_health_checks(workspace=tmp_path)

    assert [result.name for result in results] == [f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'a' * 12}"]


# ---- the model refuses a provenance-free tuple check ------------------------


def test_a_runtime_tuple_check_without_provenance_is_refused() -> None:
    """Error path: the obligation is typed, not a convention."""
    with pytest.raises(ValidationError, match="requires provenance"):
        CheckResult(name=f"{RUNTIME_TUPLE_CHECK_PREFIX}_abc", status="ok")


def test_a_check_outside_the_prefix_needs_no_provenance() -> None:
    """A check doctor measured itself carries no conformance provenance."""
    result = CheckResult(name="runtime_dir_size", status="ok")

    assert result.provenance is None


def test_provenance_refuses_a_producer_outside_the_closed_verb_set() -> None:
    """Error path: a verdict no conformance verb wrote has no producer."""
    with pytest.raises(ValidationError):
        HealthProvenance(producer="conformance.guess", stage="probe", evidence_ref=EVIDENCE_REF)  # type: ignore[arg-type]


def test_provenance_refuses_an_evidence_reference_that_is_not_an_artifact() -> None:
    """Error path: evidence is an artifact reference, never free text."""
    with pytest.raises(ValidationError):
        HealthProvenance(producer="conformance.probe", stage="probe", evidence_ref="somewhere")


def test_provenance_refuses_an_unknown_stage() -> None:
    """Error path: the stage set is the closed conformance sequence."""
    with pytest.raises(ValidationError):
        HealthProvenance(
            producer="conformance.probe",
            stage="warmup",  # type: ignore[arg-type]
            evidence_ref=EVIDENCE_REF,
        )


# ---- the checks reach the canonical doctor surface --------------------------


def test_run_all_carries_the_runtime_tuple_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice is wired into the canonical check set, not merely importable."""
    from eawf.platform.install.instrument_probe import ProbeReport, ProbeResult
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    monkeypatch.setattr(
        "eawf.observability.doctor.checks.probe",
        lambda profile_ids, *, cache_path, reprobe=False: ProbeReport(
            probe_version=1,
            profile_ids=profile_ids,
            results=[ProbeResult(name="git", kind="hard", status="ok", path="/x/git")],
        ),
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent",
        lambda *_a, **_k: SupervisedAgentReport(
            supervisor="none",
            label="",
            installed=False,
            loaded=False,
            program=None,
            drift=False,
            rival_pid=None,
        ),
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "eawfd"))
    monkeypatch.chdir(tmp_path)
    write(tmp_path, [(TUPLE_A, record("certify"))])

    results = checks.run_all(workspace=tmp_path)

    tuple_checks = [row for row in results if row.name.startswith(RUNTIME_TUPLE_CHECK_PREFIX)]
    assert [row.name for row in tuple_checks] == [f"{RUNTIME_TUPLE_CHECK_PREFIX}_{'a' * 12}"]
    assert tuple_checks[0].provenance is not None
    assert tuple_checks[0].provenance.producer == "conformance.certify"
