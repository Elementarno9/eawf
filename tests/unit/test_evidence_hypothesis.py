"""Unit tests for :mod:`eawf.workflow.evidence.hypothesis`.

Covers define / set_verdict happy + boundary + audit-evidence guard paths plus
the read-only ``list_hypotheses`` filter behaviour.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditKind,
    AuditVerdict,
    HypothesisStatus,
    HypothesisVerdict,
    StoreKind,
)
from eawf.kernel.state.models import Artifact, State
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import state_transaction
from eawf.workflow.evidence import _io, audit, hypothesis

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def _seed_artifact(state: State, artifact_id: str = "ART-001", scope: str = "QR") -> None:
    """Insert a minimum-valid :class:`Artifact` into ``state.artifacts``.

    Required because :func:`audit.add_audit` rejects a ``report_artifact_id``
    that is absent from ``state.artifacts``.
    """
    artifacts = dict(state.artifacts)
    artifacts[artifact_id] = Artifact(
        id=artifact_id,
        kind="audit_report",
        uri=f"repo:.ea/artifacts/{artifact_id}.md",
        urn=f"urn:eawf:v1:artifact:{scope}/{artifact_id}",
        created_at=datetime.now(UTC),
    )
    state.artifacts = artifacts


def test_define_hypothesis_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = hypothesis.define_hypothesis(
        state,
        hypothesis_id="H03-12",
        scope_id="QR",
        text="Latency below 100ms improves UX.",
        metric="p99_ms",
        confirm="< 100",
        reject=">= 200",
    )
    assert state.hypotheses is not None
    h = state.hypotheses["H03-12"]
    assert h.status == HypothesisStatus.PENDING
    assert h.verdict is None
    assert event.payload["event_type"] == "hypothesis.define"


def test_define_hypothesis_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    hypothesis.define_hypothesis(
        state,
        hypothesis_id="H03-12",
        scope_id="QR",
        text="t",
        metric="m",
        confirm="c",
        reject="r",
    )
    with pytest.raises(cli_errors.UserError, match="already exists"):
        hypothesis.define_hypothesis(
            state,
            hypothesis_id="H03-12",
            scope_id="QR",
            text="t2",
            metric="m2",
            confirm="c2",
            reject="r2",
        )


def test_set_verdict_unknown_hypothesis_raises_not_found(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="H99-99"):
        hypothesis.set_verdict(
            state,
            hypothesis_id="H99-99",
            verdict=HypothesisVerdict.CONFIRMED,
            audit_id="AUD-001",
        )


def test_set_verdict_missing_audit_raises_validation(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    hypothesis.define_hypothesis(
        state,
        hypothesis_id="H03-12",
        scope_id="QR",
        text="t",
        metric="m",
        confirm="c",
        reject="r",
    )
    with pytest.raises(cli_errors.ValidationError, match="UNKNOWN"):
        hypothesis.set_verdict(
            state,
            hypothesis_id="H03-12",
            verdict=HypothesisVerdict.CONFIRMED,
            audit_id="AUD-DOES-NOT-EXIST",
        )


def test_set_verdict_status_follows_verdict(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    hypothesis.define_hypothesis(
        state,
        hypothesis_id="H03-12",
        scope_id="QR",
        text="t",
        metric="m",
        confirm="c",
        reject="r",
    )
    _seed_artifact(state)
    audit.add_audit(
        state,
        audit_id="AUD-001",
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )

    for verdict, expected_status in (
        (HypothesisVerdict.CONFIRMED, HypothesisStatus.CONFIRMED),
        (HypothesisVerdict.REJECTED, HypothesisStatus.REJECTED),
        (HypothesisVerdict.INCONCLUSIVE, HypothesisStatus.INCONCLUSIVE),
    ):
        hypothesis.set_verdict(
            state,
            hypothesis_id="H03-12",
            verdict=verdict,
            audit_id="AUD-001",
        )
        h = state.hypotheses["H03-12"]
        assert h.verdict == verdict
        assert h.status == expected_status


def test_list_hypotheses_filters_by_scope_and_status(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    for i, scope in enumerate(["QR", "QR", "OTHER"]):
        hypothesis.define_hypothesis(
            state,
            hypothesis_id=f"H0{i + 1}-01",
            scope_id=scope,
            text=f"text {i}",
            metric="m",
            confirm="c",
            reject="r",
        )
    qr_only = hypothesis.list_hypotheses(state, scope_id="QR")
    assert {h.id for h in qr_only} == {"H01-01", "H02-01"}

    pending = hypothesis.list_hypotheses(state, status=HypothesisStatus.PENDING)
    assert len(pending) == 3


def test_state_transaction_persists_set_verdict(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)

    with state_transaction(state_path) as state:
        event = hypothesis.define_hypothesis(
            state,
            hypothesis_id="H03-12",
            scope_id="QR",
            text="t",
            metric="m",
            confirm="c",
            reject="r",
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        _seed_artifact(state)
        record, event = audit.add_audit(
            state,
            audit_id="AUD-001",
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            report_artifact_id="ART-001",
            verdict=AuditVerdict.PASS,
        )
        _io.append_jsonl(paths[StoreKind.AUDIT], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    with state_transaction(state_path) as state:
        event = hypothesis.set_verdict(
            state,
            hypothesis_id="H03-12",
            verdict=HypothesisVerdict.CONFIRMED,
            audit_id="AUD-001",
        )
        _io.append_jsonl(paths[StoreKind.EVENT], event)

    body = json.loads(state_path.read_text())
    assert body["hypotheses"]["H03-12"]["verdict"] == "confirmed"
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert len(events) == 3
