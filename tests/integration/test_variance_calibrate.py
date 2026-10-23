"""CLI tests for ``eawf metrics variance`` and ``eawf metrics refit``.

The variance gauge reads the selected generation's estimate and actual
ledgers (its roll-up arithmetic is pinned in
``tests/unit/workflow/estimation/test_metrics_empty_estimates.py``); the
re-fit forwards to the daemon. Float aggregates via :func:`pytest.approx`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf.kernel.config.layered as layered
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.enums import ActualStatus, Confidence
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.models import ActualSummary, EstimateSummary
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain
from eawf.surfaces.cli.commands.metrics import _render_variance
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()

_T0 = datetime(2026, 5, 1, tzinfo=UTC)


def _estimate(*, wave_id: str, expected_eu: float) -> EstimateSummary:
    return EstimateSummary(
        id=f"EST-{wave_id}",
        scope_id=wave_id,
        expected_eu=expected_eu,
        pessimistic_eu=expected_eu * 1.5,
        expected_minutes=expected_eu * 30.0,
        pessimistic_minutes=expected_eu * 45.0,
        display=f"{expected_eu} EU",
        reference_class="core_swe",
        confidence=Confidence.MEDIUM,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


def _actual(
    *,
    wave_id: str,
    elapsed_eu: float,
    updated_at: datetime = _T0,
    calibration_excluded: bool = False,
) -> ActualSummary:
    return ActualSummary(
        id=f"ACT-{wave_id}",
        scope_id=wave_id,
        status=ActualStatus.DONE,
        elapsed_eu=elapsed_eu,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=updated_at,
        calibration_excluded=calibration_excluded,
    )


# ---- compute_weekly_burn ----------------------------------------------------


# ---- CLI dispatch smoke -----------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global config layer at an empty tmp file + clear EA_STATE."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EA_STATE", raising=False)


def _write_state(tmp_path: Path) -> Path:
    """Bear an epoch-2 tree whose ledgers hold one subject that ran 50 % over."""
    workspace = tmp_path / "ws"
    state_path = lay_epoch2_tree(workspace)
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    key = "T1"
    for collection, row in (
        (Epoch2Collection.ESTIMATE, _estimate(wave_id=key, expected_eu=1.0)),
        (Epoch2Collection.ACTUAL, _actual(wave_id=key, elapsed_eu=1.5)),
    ):
        append_ledger_record(
            ledger_path(document, collection),
            LedgerRecord(
                collection=collection,
                record_key=key,
                status="imported",
                recorded_at=_T0,
                supersedes=None,
                payload={"payload": row.model_dump(mode="json")},
            ),
        )
    return workspace


def test_cli_metrics_variance_emits_gauge(tmp_path: Path) -> None:
    """``--json metrics variance`` emits the M26 gauge from the generation's ledgers."""
    workspace = _write_state(tmp_path)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "metrics", "variance"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.stdout)
    assert payload["sample_count"] == 1
    assert payload["variance_pct"] == pytest.approx(50.0)


def test_cli_metrics_variance_not_found_exits_one(tmp_path: Path) -> None:
    """Error path: no eawf tree -> NotFound exit 1."""
    workspace = tmp_path / "no-state"
    workspace.mkdir()
    result = runner.invoke(app, ["-w", str(workspace), "metrics", "variance"])
    assert result.exit_code == 1


def test_meas_018_variance_labels_the_mapping_revisions_it_spans() -> None:
    """A roll-up over estimates of two revisions names both instead of blending silently."""
    assert "across mapping revisions 2, unrecorded" in _render_variance(0.0, 2, ["2", "unrecorded"])
    assert "across" not in _render_variance(0.0, 1, ["2"])


def _refit_answer(result: str, **fields: Any) -> dict[str, Any]:
    """Return a daemon re-fit answer: the shipped revision, not due, one exclusion."""
    return {
        "result": result,
        "revision": 2,
        "digest": "sha256:" + "0" * 64,
        "outcome": {
            "disposition": "not_due",
            "current_revision": 2,
            "current_digest": "sha256:" + "0" * 64,
            "eligible_count": 1,
            "excluded": [{"reason": "flagged_excluded", "count": 1}],
            "not_due": ["sample_below_minimum"],
        },
        **fields,
    }


def test_meas_020_023_cli_metrics_refit_forwards_to_the_daemon_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json metrics refit`` sends the actor and key, and prints what the daemon did."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        sent.append((method, params))
        return _refit_answer("not_due")

    monkeypatch.setattr(domain, "_native_answer", answer)
    result = runner.invoke(
        app,
        ["--json", "metrics", "refit", "--actor", "OP-0001", "--idempotency-key", "refit-7"],
    )

    assert result.exit_code == 0, result.output
    assert sent == [
        ("runtime.estimation.refit", {"actor": "OP-0001", "idempotency_key": "refit-7"})
    ]
    import json

    payload = json.loads(result.stdout)
    assert payload["result"] == "not_due"
    assert payload["outcome"]["excluded"] == [{"reason": "flagged_excluded", "count": 1}]


def test_cli_metrics_refit_renders_plain_text_and_keys_by_the_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text form names the result, the revision and the exclusions."""
    sent: list[dict[str, Any]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        sent.append(params)
        return _refit_answer("decision_opened", action_ref="eawf://W/P/R/pending-action/ACT-0001")

    monkeypatch.setattr(domain, "_native_answer", answer)
    result = runner.invoke(app, ["metrics", "refit", "--actor", "OP-0001"])

    assert result.exit_code == 0, result.output
    assert sent[0]["idempotency_key"] == f"refit-{datetime.now(UTC):%Y-%m-%d}"
    assert "effort mapping: decision_opened, revision 2 in force" in result.stdout
    assert "operator decision: eawf://W/P/R/pending-action/ACT-0001" in result.stdout
    assert "excluded: flagged_excluded=1" in result.stdout


def test_cli_metrics_refit_without_an_actor_exits_one() -> None:
    """Error path: a re-fit may file an operator decision, so it names who asks."""
    result = runner.invoke(app, ["metrics", "refit"])
    assert result.exit_code == 1


def test_cli_metrics_refit_maps_a_daemon_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: the daemon's refusal becomes the CLI's validation exit."""

    def refuse(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        raise DaemonRpcError(cli_errors.RPC_VALIDATION_FAILED, "validation_failed: refit_raced")

    monkeypatch.setattr(domain, "_native_answer", refuse)
    result = runner.invoke(app, ["metrics", "refit", "--actor", "OP-0001"])
    assert result.exit_code == 2
