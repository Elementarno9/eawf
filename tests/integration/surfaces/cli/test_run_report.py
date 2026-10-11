"""SURF-172: ``eawf run report <RUN> [--parts ...]`` writes one Run's plain-text report.

The file lands under ``.ea/local/`` as ``<date>-run-report-<RUN>.txt``, one line per
fact; its head states the author, the projection revision, the redaction policy and the
purged ranges before the first fact; a purged range reads ``∅ purged`` in place; secrets
are never carried and the report says so; the consequence block prints before the
write; and nothing canonical moves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.events import QuarantineReason, RunEventKind, RunEventRecord
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import LOCAL_DIRNAME, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect import run_report as rr
from eawf.observability.reflect.runs import read_tree_run, read_tree_runs
from eawf.platform.scrub.scan import redact_text, scan_text
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests.contract.surfaces.cli._reflect_tree import (
    command,
    event,
    gap,
    native_tree,
    refuse_egress,
    run_row,
    summarized,
    tree_digest,
)

__all__ = ["refuse_egress"]

runner = CliRunner()

#: A home path and an email the transcript carries; neither may reach the file.
LEAKY_SUMMARY = "read /Users/<name>/project/loader.py and mailed dev@example.com"


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a canary whose Run lost sequences 3 and 4 and quoted a local path."""
    monkeypatch.delenv("EA_STATE", raising=False)
    return native_tree(
        tmp_path,
        [run_row("RUN-00000001")],
        [
            summarized("RUN-00000001", 1, LEAKY_SUMMARY),
            command("RUN-00000001", 2),
            gap("RUN-00000001", 5, expected=3),
            command("RUN-00000001", 6),
        ],
        canonical_sequence=77,
    )


def _report(tree: Path, *args: str) -> tuple[Path, str, str]:
    result = runner.invoke(app, ["-w", str(tree.parent), "run", "report", "RUN-00000001", *args])
    assert result.exit_code == 0, result.stderr
    path = Path(result.stdout.strip())
    return path, path.read_text(encoding="utf-8"), result.stderr


def test_surf_172_the_file_lands_under_local_with_the_dated_name(tree: Path) -> None:
    """The report is a dated local artifact named for its Run."""
    path, _, _ = _report(tree)
    today = datetime.now(UTC).date().isoformat()
    assert path == tree / LOCAL_DIRNAME / f"{today}-run-report-RUN-00000001.txt"


def test_surf_172_the_head_precedes_every_fact(tree: Path) -> None:
    """Author, projection revision, policy and purged ranges come before the first fact."""
    _, text, _ = _report(tree, "--actor", "OP-0001")
    lines = text.splitlines()
    first_fact = next(i for i, line in enumerate(lines) if line.startswith("timeline "))
    head = "\n".join(lines[:first_fact])
    assert "author OP-0001" in head
    assert "projection revision 77 · run revision 3" in head
    assert f"redaction policy: {rr.REDACTION_POLICY}" in head
    assert "purged ranges: 3-4" in head
    assert rr.QUOTABILITY in head


def test_surf_172_a_purged_range_is_written_not_omitted(tree: Path) -> None:
    """Sequences 3-4 read as purged in both the timeline and the transcript."""
    _, text, _ = _report(tree)
    assert f"timeline 3-4 {rr.PURGED_TOKEN} (2 sequences)" in text
    assert f"transcript 3-4 {rr.PURGED_TOKEN}" in text


def test_surf_172_the_report_is_redacted_and_passes_the_scrub(tree: Path) -> None:
    """The home path and the email are rewritten, and the file passes the scrub scan."""
    _, text, stderr = _report(tree)
    assert "/Users/<name>" not in text
    assert "dev@example.com" not in text
    assert "<local-path>" in text
    assert scan_text(text) == []
    assert "redactions: 1 line scrubbed" in stderr


def test_surf_172_secrets_are_never_included_and_the_report_says_so(tree: Path) -> None:
    """Asking for secrets still carries none; the part states the policy instead."""
    _, text, stderr = _report(tree, "--parts", "secrets,timeline")
    assert rr.SECRETS_LINE in text
    assert "part secrets: never · redacted by policy" in stderr
    assert "transcript " not in text
    assert "parts: timeline · secrets never" in text


def test_surf_172_the_consequence_block_names_parts_sizes_and_destination(tree: Path) -> None:
    """The block prints on stderr with each part's size and the destination."""
    path, _, stderr = _report(tree, "--parts", "timeline,sandbox_decisions")
    assert stderr.startswith("consequence: run report ")
    assert "part timeline: 4 lines" in stderr
    assert "part sandbox_decisions: 1 line" in stderr
    assert f"destination: .ea/{LOCAL_DIRNAME}/{path.name}" in stderr
    assert "not: no canonical record moves; nothing leaves the machine" in stderr


def test_surf_172_a_part_with_nothing_to_state_says_so(tree: Path) -> None:
    """A Run the gateway decided nothing for and an uncaptured runtime say so, not a blank."""
    _, text, _ = _report(tree, "--parts", "sandbox_decisions,usage_and_cost")
    assert rr.NO_SANDBOX_DECISIONS in text
    assert "usage_and_cost unavailable: the Run holds no captured runtime" in text


def test_surf_172_nothing_canonical_moves_and_nothing_leaves(
    tree: Path, refuse_egress: list[str]
) -> None:
    """The verb is a local file write: no canonical byte changes and no egress."""
    before = tree_digest(tree, skip=tree / LOCAL_DIRNAME)
    _report(tree)
    assert tree_digest(tree, skip=tree / LOCAL_DIRNAME) == before
    assert refuse_egress == []


def test_surf_172_a_run_the_tree_does_not_hold_is_not_found(tree: Path) -> None:
    """Error path: an unknown Run key is a user error naming it."""
    result = runner.invoke(app, ["-w", str(tree.parent), "run", "report", "RUN-0000FFFF"])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "RUN-0000FFFF" in result.stdout


def test_surf_172_an_unknown_part_is_refused_before_anything_is_read(tree: Path) -> None:
    """Error path: a part outside the declared set is an invalid input."""
    result = runner.invoke(
        app, ["-w", str(tree.parent), "run", "report", "RUN-00000001", "--parts", "billing"]
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert list((tree / LOCAL_DIRNAME).glob("*-run-report-*")) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, rr.DEFAULT_PARTS),
        ("transcript", (rr.ReportPartName.TRANSCRIPT,)),
        (
            "transcript, timeline,transcript",
            (rr.ReportPartName.TIMELINE, rr.ReportPartName.TRANSCRIPT),
        ),
        (",".join(part.value for part in rr.ReportPartName), tuple(rr.ReportPartName)),
    ],
)
def test_surf_172_parts_parse_in_render_order(
    text: str | None, expected: tuple[rr.ReportPartName, ...]
) -> None:
    """Default, single, duplicate and every part, always in render order."""
    assert rr.parse_parts(text) == expected


@pytest.mark.parametrize("text", ["", " , ", "timeline,billing"])
def test_surf_172_an_empty_or_unknown_parts_value_is_refused(text: str) -> None:
    """Error path: no part or an undeclared one is a ValueError."""
    with pytest.raises(ValueError, match="part"):
        rr.parse_parts(text)


def test_surf_172_a_run_is_found_by_its_urn_as_well_as_its_key(tree: Path) -> None:
    """Boundary: the qualified URN names the same Run as its key."""
    by_key = read_tree_run(tree, "RUN-00000001")
    assert read_tree_run(tree, str(by_key.run.urn)).run.key == "RUN-00000001"


def test_surf_172_an_epoch_one_tree_holds_no_run(tmp_path: Path) -> None:
    """Boundary: a tree without a native generation reports the Run as absent."""
    (tmp_path / ".ea").mkdir()
    with pytest.raises(LookupError):
        read_tree_run(tmp_path / ".ea", "RUN-00000001")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ""),
        ("plain words", "plain words"),
        ("mail dev@example.com now", "mail <email> now"),
        ("keep noreply@anthropic.com", "keep noreply@anthropic.com"),
        ("at ~/code and ops@example.com", "at <local-path> and <email>"),
    ],
)
def test_surf_172_the_report_scrub_leaves_nothing_the_scan_flags(text: str, expected: str) -> None:
    """Empty, clean, email, allowlisted email and a mixed line; each passes the scan."""
    assert redact_text(text) == expected
    assert scan_text(redact_text(text)) == []


#: A runtime tuple whose every member was observed.
FULL_TUPLE = {
    "harness": "claude-code",
    "harness_version": "2.1.3",
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
}


def _tuple_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str] | None
) -> Path:
    """Return a canary whose one Run recorded ``runtime`` as its tuple, or none at all."""
    monkeypatch.delenv("EA_STATE", raising=False)
    row = run_row("RUN-00000001")
    if runtime is not None:
        row["runtime_tuple"] = runtime
    return native_tree(tmp_path, [row], [command("RUN-00000001", 1)], canonical_sequence=9)


def _report_json(tree: Path, *args: str) -> dict[str, Any]:
    result = runner.invoke(
        app, ["--json", "-w", str(tree.parent), "run", "report", "RUN-00000001", *args]
    )
    assert result.exit_code == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok", envelope
    payload: dict[str, Any] = envelope["result"]
    return payload


@pytest.mark.parametrize(
    ("runtime", "expected_lines"),
    [
        (
            FULL_TUPLE,
            [
                "runtime_tuple harness claude-code",
                "runtime_tuple harness_version 2.1.3",
                "runtime_tuple provider anthropic",
                "runtime_tuple model claude-sonnet-4-5",
            ],
        ),
        ({"harness": "codex"}, ["runtime_tuple harness codex"]),
        (None, [rr.NO_RUNTIME_TUPLE]),
    ],
    ids=["full", "partial", "none"],
)
def test_the_report_states_the_recorded_runtime_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: dict[str, str] | None,
    expected_lines: list[str],
) -> None:
    """The JSON carries the tuple as recorded, the head one line per member; absent stays absent."""
    tree = _tuple_tree(tmp_path, monkeypatch, runtime)
    payload = _report_json(tree)
    assert payload["runtime_tuple"] == runtime
    lines = Path(payload["path"]).read_text(encoding="utf-8").splitlines()
    stated = [line for line in lines if line.startswith("runtime_tuple ")]
    assert stated == expected_lines
    # the tuple is part of the head: stated before the first fact line
    first_fact = next(i for i, line in enumerate(lines) if line.startswith("timeline "))
    assert lines.index(expected_lines[-1]) < first_fact


def test_the_json_tuple_is_carried_whichever_parts_were_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a report asking only for secrets still states the tuple in JSON and text."""
    tree = _tuple_tree(tmp_path, monkeypatch, FULL_TUPLE)
    payload = _report_json(tree, "--parts", "secrets")
    assert payload["runtime_tuple"] == FULL_TUPLE
    assert "runtime_tuple" not in payload["parts"]
    assert "runtime_tuple provider anthropic" in Path(payload["path"]).read_text(encoding="utf-8")


def test_a_recorded_tuple_without_its_harness_is_refused_at_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a stored tuple missing its required harness fails validation."""
    tree = _tuple_tree(tmp_path, monkeypatch, {"provider": "anthropic"})
    with pytest.raises(ValidationError):
        read_tree_run(tree, "RUN-00000001")


# ---- a finished Run compacted into the run ledger -----------------------------------


def _document_path(tree: Path) -> Path:
    """Return the document of the tree's selected generation."""
    authority = resolve_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _file_run(tree: Path, row: dict[str, Any]) -> None:
    """Append *row* to the run ledger as the effective record of its Run."""
    append_ledger_record(
        ledger_path(_document_path(tree), Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=row["key"],
            status=row["status"],
            recorded_at=datetime.fromisoformat(row["updated_at"]),
            payload=row,
        ),
    )


def _compact(tree: Path, key: str) -> None:
    """Move Run *key* out of the document into the run ledger, as terminal compaction does."""
    document_path = _document_path(tree)
    document = read_document(document_path)
    row = document[Epoch2Collection.RUN.value].pop(key)
    write_document(document_path, document)
    _file_run(tree, row)


def test_a_compacted_run_is_reported_from_the_run_ledger(tree: Path) -> None:
    """The finished Run left the document; the report reads its effective ledger line."""
    _compact(tree, "RUN-00000001")

    path, text, _ = _report(tree)

    assert path.name.endswith("-run-report-RUN-00000001.txt")
    assert "run revision 3" in text
    assert "purged ranges: 3-4" in text
    assert "timeline 6 " in text
    assert read_tree_runs(tree) == ()


def test_the_latest_ledger_line_of_a_compacted_run_is_the_one_reported(tree: Path) -> None:
    """Boundary: a Run filed twice reads as its last line, not its first."""
    _compact(tree, "RUN-00000001")
    _file_run(tree, {**run_row("RUN-00000001"), "revision": 4})

    assert read_tree_run(tree, "RUN-00000001").run.revision == 4


def test_a_run_in_neither_tier_is_still_not_found(tree: Path) -> None:
    """Error path: compaction widens the lookup to the ledger and no further."""
    _compact(tree, "RUN-00000001")
    with pytest.raises(LookupError, match="RUN-00000002"):
        read_tree_run(tree, "RUN-00000002")


# ---- a Run's status and the usage its own stream measured --------------------------


def _usage(
    sequence: int, *, output: int, cumulative: bool = True, **overrides: Any
) -> RunEventRecord:
    """Return one measured usage reading of RUN-00000001, as a native launcher relays it."""
    return event(
        "RUN-00000001",
        sequence,
        event_kind=RunEventKind.USAGE_OBSERVED,
        payload=UsagePayload(
            input_tokens=2,
            output_tokens=output,
            cache_tokens=2385,
            usage_source="provider_transcript",
            is_cumulative=cumulative,
            measurement_quality="measured",
        ),
        **overrides,
    )


def _failed_native_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[RunEventRecord]
) -> Path:
    """Return a canary whose one Run FAILED unbound and uncaptured, with *events*, compacted."""
    monkeypatch.delenv("EA_STATE", raising=False)
    row = run_row("RUN-00000001", status="FAILED")
    row["failure"] = {
        "code": "report-unbound",
        "message": "the worker's turn ended without binding a report",
        "evidence_refs": [],
    }
    row["counter_baseline"] = {"outcome": "uncaptured", "reason": "no_source"}
    row["captured_runtime"] = {"outcome": "uncaptured", "reason": "no_baseline"}
    tree = native_tree(tmp_path, [row], events, canonical_sequence=53)
    _compact(tree, "RUN-00000001")
    return tree


def _usage_lines(payload: dict[str, Any]) -> list[str]:
    """Return the usage part of the report a JSON answer names."""
    text = Path(payload["path"]).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith("usage_and_cost ")]


def test_the_report_states_a_failed_runs_status_and_failure_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON and the head both name the status and the code the Run failed with."""
    tree = _failed_native_tree(tmp_path, monkeypatch, [_usage(1, output=8)])

    payload = _report_json(tree)

    assert payload["run_status"] == "FAILED"
    assert payload["failure_code"] == "report-unbound"
    lines = Path(payload["path"]).read_text(encoding="utf-8").splitlines()
    assert lines[1] == "status FAILED · failure report-unbound"


def test_a_run_that_did_not_fail_states_its_status_and_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a completed Run carries a null failure code and a bare status line."""
    tree = _tuple_tree(tmp_path, monkeypatch, None)

    payload = _report_json(tree)

    assert payload["run_status"] == "COMPLETED"
    assert payload["failure_code"] is None
    assert Path(payload["path"]).read_text(encoding="utf-8").splitlines()[1] == "status COMPLETED"


def test_an_uncaptured_run_reports_the_usage_its_stream_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running totals read as the last total, not their sum, and absence is not zero."""
    tree = _failed_native_tree(tmp_path, monkeypatch, [_usage(1, output=8), _usage(2, output=371)])

    lines = _usage_lines(_report_json(tree, "--parts", "usage_and_cost"))

    assert lines == [
        "usage_and_cost quality measured · source provider_transcript · 2 readings",
        "usage_and_cost input_tokens 2",
        "usage_and_cost output_tokens 371",
        "usage_and_cost cache_tokens 2385",
        "usage_and_cost cost_microusd unobserved: no reading reported it",
    ]


def test_delta_readings_are_summed_and_one_reading_is_singular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: deltas add up; a single reading is counted as one."""
    tree = _failed_native_tree(
        tmp_path,
        monkeypatch,
        [_usage(1, output=5, cumulative=False), _usage(2, output=7, cumulative=False)],
    )
    assert "usage_and_cost output_tokens 12" in _usage_lines(_report_json(tree))

    single = _failed_native_tree(tmp_path / "single", monkeypatch, [_usage(1, output=5)])
    assert _usage_lines(_report_json(single))[0].endswith("· 1 reading")


def test_a_quarantined_reading_measures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a reading kept out of every derivation leaves the usage unavailable."""
    late = _usage(1, output=8, quarantine=QuarantineReason.LATE_AFTER_TERMINAL)
    tree = _failed_native_tree(tmp_path, monkeypatch, [late])

    assert _usage_lines(_report_json(tree)) == [
        "usage_and_cost unavailable: uncaptured (no_baseline)"
    ]


def test_an_uncaptured_run_with_no_readings_still_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty: with nothing measured the uncaptured reason is stated, not a blank part."""
    tree = _failed_native_tree(tmp_path, monkeypatch, [])

    assert _usage_lines(_report_json(tree)) == [
        "usage_and_cost unavailable: uncaptured (no_baseline)"
    ]
