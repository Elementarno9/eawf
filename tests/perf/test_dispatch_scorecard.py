"""Gates for the dispatch scorecard: one Run's efficiency as numbers a release can check.

The scorecard reads a Run's per-call receipts and usage readings. A tier
compares it against fixed limits. The two committed extracts are scrubbed
copies of real Runs. RUN-00000505 is a verify-only Run, and RUN-00000507 is
a Run that spent an hour re-reading the repository. Together they show the
check telling a usable Run from a wasteful one without a live dispatch.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import RunEventKind, RunEventRecord
from eawf.kernel.runtime.semantic import SemanticToolErrorCode, SemanticToolId, error_for
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.bench.dispatch_scorecard import (
    TIER_THRESHOLDS,
    DispatchExtract,
    DispatchMetric,
    DispatchScorecard,
    DispatchTier,
    ExtractedCall,
    check_scorecard,
    load_extract,
    read_dispatch_extract,
    score_dispatch,
    write_extract,
)
from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks
from eawf.runtime.daemon.semantic_gateway import SemanticCallReceipt
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import (
    AT,
    RUN_PREFIX,
    event,
    native_tree,
    run_row,
)

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "dispatch"
VERIFY_ONLY = FIXTURES / "RUN-00000505.json"
REREADER = FIXTURES / "RUN-00000507.json"


# --- builders ----------------------------------------------------------------


def _read(
    seconds: int, key: str = "c0001", *, byte_count: int = 100, truncated: bool = False
) -> ExtractedCall:
    return ExtractedCall(
        tool_id=SemanticToolId.REPO_READ,
        status="succeeded",
        completed_at=AT + timedelta(seconds=seconds),
        truncated=truncated,
        content_key=key,
        byte_count=byte_count,
    )


def _patch(seconds: int, *, status: str = "succeeded") -> ExtractedCall:
    return ExtractedCall.model_validate(
        {
            "tool_id": "workspace_apply_patch",
            "status": status,
            "completed_at": AT + timedelta(seconds=seconds),
        }
    )


def _usage(input_tokens: int, output_tokens: int, cache_tokens: int) -> UsagePayload:
    return UsagePayload(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        usage_source="provider_transcript",
        is_cumulative=True,
        measurement_quality="measured",
    )


def _extract(**overrides: Any) -> DispatchExtract:
    fields: dict[str, Any] = {
        "run_key": "RUN-00000001",
        "status": RunStatus.COMPLETED,
        "started_at": AT,
        "ended_at": AT + timedelta(seconds=120),
        "usage": (),
        "calls": (),
    }
    return DispatchExtract.model_validate(fields | overrides)


def _scorecard(**overrides: Any) -> DispatchScorecard:
    fields: dict[str, Any] = {
        "run_key": "RUN-00000001",
        "status": RunStatus.COMPLETED,
        "turns": 1,
        "read_truncation_rate": Decimal(0),
        "identical_reread_rate": Decimal(0),
        "billable_tokens": 1,
        "cache_read_tokens": 0,
        "first_patch_seconds": None,
        "wall_seconds": Decimal(1),
    }
    return DispatchScorecard.model_validate(fields | overrides)


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _receipt(
    key: str, index: int, output: dict[str, Any] | None, *, status: str = "succeeded"
) -> LedgerRecord:
    """Return the receipt ledger line of call *index* of Run *key*."""
    call_id = f"call-{index:016x}"
    receipt_id = f"receipt-{index:016x}"
    at = AT + timedelta(seconds=10 * index)
    tool_id = output["tool_id"] if output is not None else "workspace_apply_patch"
    result: dict[str, Any] = {
        "call_id": call_id,
        "run_ref": f"{RUN_PREFIX}/{key}",
        "receipt_id": receipt_id,
        "status": status,
        "result_schema_version": "1.0.0",
        "bounded_output": output,
        "completed_at": at.isoformat(),
    }
    if output is None:
        result["error"] = error_for(
            SemanticToolErrorCode.INTERNAL_TRANSIENT, message="patch failed"
        ).model_dump(mode="json")
    receipt = SemanticCallReceipt.model_validate(
        {
            "receipt_id": receipt_id,
            "call_id": call_id,
            "run_ref": f"{RUN_PREFIX}/{key}",
            "tool_id": tool_id,
            "idempotency_key": call_id,
            "payload_digest": _digest(call_id),
            "result": result,
            "recorded_at": at.isoformat(),
        }
    )
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=call_id,
        status=status,
        recorded_at=at,
        payload=receipt.model_dump(mode="json"),
    )


def _read_output(text: str, *, truncated: bool) -> dict[str, Any]:
    digest = _digest(text)
    return {
        "tool_id": "repo_read",
        "content_ref": f"artifact://content/run/{digest.removeprefix('sha256:')}",
        "content_digest": digest,
        "byte_count": len(text),
        "truncated": truncated,
        "text": text,
    }


def _patch_output() -> dict[str, Any]:
    return {
        "tool_id": "workspace_apply_patch",
        "changed_paths": ["src/a.py"],
        "resulting_tree_digest": _digest("tree"),
        "workspace_generation": 1,
    }


def _usage_event(key: str, sequence: int, reading: UsagePayload) -> RunEventRecord:
    return event(key, sequence, event_kind=RunEventKind.USAGE_OBSERVED, payload=reading)


def _ledger_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a tree holding RUN-00000001: three reads, a failed and a good patch."""
    monkeypatch.delenv("EA_STATE", raising=False)
    key = "RUN-00000001"
    tree = native_tree(
        tmp_path,
        [run_row(key, hours=0) | {"ended_at": (AT + timedelta(seconds=200)).isoformat()}],
        [
            _usage_event(key, 1, _usage(10, 20, 1_000)),
            _usage_event(key, 2, _usage(30, 50, 4_000)),
        ],
    )
    authority = resolve_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    receipts = ledger_path(document, Epoch2Collection.RECEIPT)
    for line in (
        _receipt(key, 1, _read_output("alpha\n", truncated=True)),
        _receipt(key, 2, _read_output("beta\n", truncated=False)),
        _receipt(key, 3, _read_output("alpha\n", truncated=True)),
        _receipt(key, 4, None, status="failed"),
        _receipt(key, 5, _patch_output()),
        _receipt("RUN-00000002", 6, _read_output("other\n", truncated=True)),
    ):
        append_ledger_record(receipts, line)
    return tree


# --- CR-0505-1: the scorecard's fields ------------------------------------------


def test_scorecard_fields_of_a_run_with_reads_patches_and_usage() -> None:
    card = score_dispatch(
        _extract(
            usage=(_usage(1, 2, 300), _usage(5, 10, 900)),
            calls=(
                _read(10, "c0001", truncated=True),
                _read(20, "c0002"),
                _read(30, "c0001", truncated=True),
                _read(40, "c0001", byte_count=50),
                _patch(50, status="failed"),
                _patch(90),
                _patch(100),
            ),
        )
    )
    assert card.status is RunStatus.COMPLETED
    assert card.turns == 2
    assert card.read_truncation_rate == Decimal(2) / Decimal(4)
    assert card.identical_reread_rate == Decimal(1) / Decimal(4)
    assert card.billable_tokens == 5 + 10 + 900
    assert card.cache_read_tokens == 900
    assert card.first_patch_seconds == Decimal(90)
    assert card.wall_seconds == Decimal(120)


def test_scorecard_fields_of_an_empty_run() -> None:
    card = score_dispatch(_extract())
    assert card.turns == 0
    assert card.read_truncation_rate == Decimal(0)
    assert card.identical_reread_rate == Decimal(0)
    assert card.billable_tokens is None
    assert card.cache_read_tokens is None
    assert card.first_patch_seconds is None


def test_scorecard_fields_of_a_single_read() -> None:
    card = score_dispatch(_extract(calls=(_read(1, truncated=True),)))
    assert card.read_truncation_rate == Decimal(1)
    assert card.identical_reread_rate == Decimal(0)


def test_scorecard_fields_of_a_queued_run_carry_no_durations() -> None:
    card = score_dispatch(
        _extract(status=RunStatus.QUEUED, started_at=None, ended_at=None, calls=(_patch(5),))
    )
    assert card.first_patch_seconds is None
    assert card.wall_seconds is None


def test_scorecard_fields_keep_sub_second_durations_exact() -> None:
    card = score_dispatch(_extract(ended_at=AT + timedelta(seconds=1, microseconds=5)))
    assert card.wall_seconds == Decimal("1.000005")


def test_scorecard_fields_from_a_ledger_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extract = read_dispatch_extract(_ledger_tree(tmp_path, monkeypatch), "RUN-00000001")
    card = score_dispatch(extract)
    assert [call.tool_id for call in extract.calls] == [
        "repo_read",
        "repo_read",
        "repo_read",
        "workspace_apply_patch",
        "workspace_apply_patch",
    ]
    assert [call.content_key for call in extract.calls[:3]] == ["c0001", "c0002", "c0001"]
    assert card.turns == 2
    assert card.read_truncation_rate == Decimal(2) / Decimal(3)
    assert card.identical_reread_rate == Decimal(1) / Decimal(3)
    assert card.billable_tokens == 30 + 50 + 4_000
    assert card.cache_read_tokens == 4_000
    assert card.first_patch_seconds == Decimal(50)
    assert card.wall_seconds == Decimal(200)


def test_scorecard_fields_extract_carries_no_content_or_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extract = read_dispatch_extract(_ledger_tree(tmp_path, monkeypatch), "RUN-00000001")
    raw = extract.model_dump_json()
    assert "alpha" not in raw
    assert "sha256:" not in raw
    assert "src/a.py" not in raw


def test_scorecard_fields_refuse_an_unknown_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LookupError, match="RUN-00000009"):
        read_dispatch_extract(_ledger_tree(tmp_path, monkeypatch), "RUN-00000009")


def test_scorecard_fields_refuse_a_malformed_extract(tmp_path: Path) -> None:
    path = tmp_path / "extract.json"
    path.write_bytes(orjson.dumps(_extract().model_dump(mode="json") | {"extra": 1}))
    with pytest.raises(ValidationError):
        load_extract(path)
    path.write_bytes(b"{not json")
    with pytest.raises(ValueError, match="malformed"):
        load_extract(path)
    with pytest.raises(FileNotFoundError):
        load_extract(tmp_path / "missing.json")


def test_scorecard_fields_refuse_a_read_without_its_window() -> None:
    with pytest.raises(ValidationError):
        ExtractedCall(
            tool_id=SemanticToolId.REPO_READ, status="succeeded", completed_at=AT, truncated=True
        )
    with pytest.raises(ValidationError):
        ExtractedCall.model_validate(
            {"tool_id": "repo_read", "status": "succeeded", "completed_at": AT, "byte_count": -1}
        )


def test_scorecard_fields_round_trip_through_a_written_extract(tmp_path: Path) -> None:
    extract = _extract(usage=(_usage(1, 2, 3),), calls=(_read(1), _patch(2)))
    path = tmp_path / "nested" / "extract.json"
    write_extract(extract, path)
    assert load_extract(path) == extract


@pytest.mark.parametrize("run_key", ["", "RUN-1", "TASK-00000001", "RUN-000000001"])
def test_scorecard_fields_refuse_a_malformed_run_key(run_key: str) -> None:
    with pytest.raises(ValidationError):
        _extract(run_key=run_key)


# --- CR-0505-2: the tier thresholds ---------------------------------------------


def test_tier_thresholds_are_the_briefs() -> None:
    fixture = TIER_THRESHOLDS[DispatchTier.FIXTURE]
    real = TIER_THRESHOLDS[DispatchTier.REAL]
    assert (
        fixture.max_turns,
        fixture.max_read_truncation_rate,
        fixture.max_identical_reread_rate,
        fixture.max_billable_tokens,
        fixture.max_first_patch_seconds,
        fixture.max_wall_seconds,
    ) == (25, Decimal("0.20"), Decimal("0.05"), 500_000, Decimal(300), Decimal(600))
    assert (
        real.max_turns,
        real.max_read_truncation_rate,
        real.max_identical_reread_rate,
        real.max_billable_tokens,
        real.max_first_patch_seconds,
        real.max_wall_seconds,
    ) == (60, Decimal("0.30"), Decimal("0.10"), 2_000_000, Decimal(900), None)


@pytest.mark.parametrize("tier", list(DispatchTier))
def test_tier_thresholds_pass_a_run_exactly_at_every_limit(tier: DispatchTier) -> None:
    limits = TIER_THRESHOLDS[tier]
    card = _scorecard(
        turns=limits.max_turns,
        read_truncation_rate=limits.max_read_truncation_rate,
        identical_reread_rate=limits.max_identical_reread_rate,
        billable_tokens=limits.max_billable_tokens,
        first_patch_seconds=limits.max_first_patch_seconds,
        wall_seconds=limits.max_wall_seconds,
    )
    check = check_scorecard(card, tier)
    assert check.breaches == ()
    assert check.passed


@pytest.mark.parametrize(
    ("tier", "field", "over", "metric"),
    [
        (DispatchTier.FIXTURE, "turns", 26, DispatchMetric.TURNS),
        (
            DispatchTier.FIXTURE,
            "read_truncation_rate",
            Decimal("0.2000001"),
            DispatchMetric.READ_TRUNCATION_RATE,
        ),
        (
            DispatchTier.FIXTURE,
            "identical_reread_rate",
            Decimal("0.0500001"),
            DispatchMetric.IDENTICAL_REREAD_RATE,
        ),
        (DispatchTier.FIXTURE, "billable_tokens", 500_001, DispatchMetric.BILLABLE_TOKENS),
        (
            DispatchTier.FIXTURE,
            "first_patch_seconds",
            Decimal("300.000001"),
            DispatchMetric.FIRST_PATCH_SECONDS,
        ),
        (DispatchTier.FIXTURE, "wall_seconds", Decimal("600.000001"), DispatchMetric.WALL_SECONDS),
        (DispatchTier.REAL, "turns", 61, DispatchMetric.TURNS),
        (
            DispatchTier.REAL,
            "read_truncation_rate",
            Decimal("0.31"),
            DispatchMetric.READ_TRUNCATION_RATE,
        ),
        (
            DispatchTier.REAL,
            "identical_reread_rate",
            Decimal("0.11"),
            DispatchMetric.IDENTICAL_REREAD_RATE,
        ),
        (DispatchTier.REAL, "billable_tokens", 2_000_001, DispatchMetric.BILLABLE_TOKENS),
        (
            DispatchTier.REAL,
            "first_patch_seconds",
            Decimal(901),
            DispatchMetric.FIRST_PATCH_SECONDS,
        ),
        (DispatchTier.FIXTURE, "status", RunStatus.FAILED, DispatchMetric.STATUS),
        (DispatchTier.REAL, "status", RunStatus.RUNNING, DispatchMetric.STATUS),
    ],
)
def test_tier_thresholds_breach_one_past_a_limit(
    tier: DispatchTier, field: str, over: object, metric: DispatchMetric
) -> None:
    check = check_scorecard(_scorecard(**{field: over}), tier)
    assert check.breaches == (metric,)
    assert not check.passed


def test_tier_thresholds_let_real_runs_take_any_wall_clock() -> None:
    check = check_scorecard(_scorecard(wall_seconds=Decimal(86_400)), DispatchTier.REAL)
    wall = next(row for row in check.checks if row.metric is DispatchMetric.WALL_SECONDS)
    assert wall.limit is None
    assert not wall.breached


@pytest.mark.parametrize("tier", list(DispatchTier))
def test_tier_thresholds_let_unmeasured_values_breach_nothing(tier: DispatchTier) -> None:
    card = _scorecard(first_patch_seconds=None, wall_seconds=None, billable_tokens=None)
    assert check_scorecard(card, tier).breaches == ()


def test_tier_thresholds_report_every_metric_beside_its_limit() -> None:
    check = check_scorecard(_scorecard(), DispatchTier.FIXTURE)
    assert [row.metric for row in check.checks] == list(DispatchMetric)
    status = check.checks[0]
    assert (status.observed, status.limit) == ("COMPLETED", "COMPLETED")


def test_tier_thresholds_refuse_an_unknown_tier() -> None:
    with pytest.raises(ValueError, match="nightly"):
        DispatchTier("nightly")


# --- CR-0505-3: the two scrubbed extracts ---------------------------------------


@pytest.mark.parametrize("path", [VERIFY_ONLY, REREADER])
def test_extracts_carry_no_leak_shape(path: Path) -> None:
    assert (
        scan_state_leaks(path.read_text(encoding="utf-8"), allowed_emails=default_allowed_emails())
        == []
    )
    assert "sha256:" not in path.read_text(encoding="utf-8")


def test_extracts_score_the_verify_only_run() -> None:
    card = score_dispatch(load_extract(VERIFY_ONLY))
    assert card.run_key == "RUN-00000505"
    assert card.status is RunStatus.COMPLETED
    assert card.turns == 27
    assert card.billable_tokens == 1_483_560
    assert card.cache_read_tokens == 1_463_317
    assert card.identical_reread_rate == Decimal(0)
    assert card.read_truncation_rate == Decimal(20) / Decimal(26)
    assert card.first_patch_seconds is None


def test_extracts_verify_only_run_breaches_only_read_truncation_under_real() -> None:
    # Twenty of the Run's 26 reads were cut by the 8 KB window it ran under,
    # so the honest reading breaches the truncation limit and nothing else.
    check = check_scorecard(score_dispatch(load_extract(VERIFY_ONLY)), DispatchTier.REAL)
    assert check.breaches == (DispatchMetric.READ_TRUNCATION_RATE,)


def test_extracts_rereading_run_breaches_turns_truncation_and_rereads_under_real() -> None:
    card = score_dispatch(load_extract(REREADER))
    assert card.run_key == "RUN-00000507"
    assert card.turns == 140
    assert card.read_truncation_rate == Decimal(119) / Decimal(154)
    assert card.identical_reread_rate == Decimal(33) / Decimal(154)
    assert card.first_patch_seconds is not None
    assert Decimal(2_600) < card.first_patch_seconds < Decimal(2_700)
    breaches = set(check_scorecard(card, DispatchTier.REAL).breaches)
    assert len(breaches) >= 3
    assert {
        DispatchMetric.TURNS,
        DispatchMetric.READ_TRUNCATION_RATE,
        DispatchMetric.IDENTICAL_REREAD_RATE,
    } <= breaches


# --- CR-0505-4: the bench dispatch verb -----------------------------------------


def test_cli_check_exits_2_on_the_rereading_extract() -> None:
    result = runner.invoke(
        app,
        [
            "bench",
            "dispatch",
            "RUN-00000507",
            "--extract",
            str(REREADER),
            "--check",
            "--tier",
            "real",
        ],
    )
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    assert "!! turns: 140 (limit 60)" in result.output
    assert "!! identical_reread_rate:" in result.output
    assert "tier=real" in result.output


def test_cli_check_defaults_to_the_fixture_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _ledger_tree(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["--json", "-w", str(tree.parent), "bench", "dispatch", "RUN-00000001", "--check"]
    )
    payload = orjson.loads(result.stdout)
    assert payload["check"]["tier"] == "fixture"
    assert payload["check"]["breaches"] == ["read_truncation_rate", "identical_reread_rate"]
    assert result.exit_code == exit_codes.VALIDATION_ERROR


def test_cli_check_exits_0_on_a_clean_run_from_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = _extract(run_key="RUN-00000001", usage=(_usage(1, 2, 3),), calls=(_read(5), _patch(9)))
    path = tmp_path / "clean.json"
    write_extract(clean, path)
    result = runner.invoke(
        app, ["bench", "dispatch", "RUN-00000001", "--extract", str(path), "--check"]
    )
    assert result.exit_code == 0, result.output
    assert "bench dispatch: ok" in result.output
    assert "   wall_seconds: 120 (limit 600)" in result.output


def test_cli_check_without_check_renders_breaches_and_exits_0() -> None:
    result = runner.invoke(app, ["bench", "dispatch", "RUN-00000507", "--extract", str(REREADER)])
    assert result.exit_code == 0, result.output
    assert "BREACHED" in result.output


def test_cli_check_writes_the_extract_the_fixtures_are_made_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _ledger_tree(tmp_path, monkeypatch)
    target = tmp_path / "out" / "RUN-00000001.json"
    result = runner.invoke(
        app,
        [
            "-w",
            str(tree.parent),
            "bench",
            "dispatch",
            "RUN-00000001",
            "--write-extract",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert load_extract(target) == read_dispatch_extract(tree, "RUN-00000001")


def test_cli_check_refuses_an_extract_of_another_run() -> None:
    result = runner.invoke(app, ["bench", "dispatch", "RUN-00000505", "--extract", str(REREADER)])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "RUN-00000507" in result.output


def test_cli_check_refuses_a_missing_extract_and_an_unknown_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = runner.invoke(
        app, ["bench", "dispatch", "RUN-00000001", "--extract", str(tmp_path / "none.json")]
    )
    assert missing.exit_code == exit_codes.USER_ERROR
    tree = _ledger_tree(tmp_path, monkeypatch)
    unknown = runner.invoke(app, ["-w", str(tree.parent), "bench", "dispatch", "RUN-00000009"])
    assert unknown.exit_code == exit_codes.USER_ERROR
    assert "RUN-00000009" in unknown.output


def test_cli_check_refuses_an_unknown_tier() -> None:
    result = runner.invoke(
        app, ["bench", "dispatch", "RUN-00000507", "--extract", str(REREADER), "--tier", "nightly"]
    )
    assert result.exit_code != 0


def test_cli_check_verb_is_listed_in_the_cli_reference() -> None:
    reference = Path(__file__).resolve().parents[2] / "docs" / "reference" / "autogen" / "cli.md"
    assert "| `dispatch` |" in reference.read_text(encoding="utf-8")
