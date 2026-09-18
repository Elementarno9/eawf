"""The re-probe record refuses what a careless second measurement produces.

Three refusals carry the weight here, and each exists because the failure it
prevents is cheap to commit and expensive to notice.

**An unknown field.** A record written by a probe one revision ahead of the
reader is not a richer record, it is an unvalidated one, so the extra key fails
at the boundary rather than riding along unread.

**A breach with nowhere to go.** The whole point of freezing a measured
contract is that the next measurement cannot quietly widen it. A comparison
that left its limit is therefore only valid next to a breach row naming the
successor contract that will carry the new number -- and a breach row is only
valid next to a comparison that actually left a limit.

**A host path or a vendor session id in any value.** A re-probe drives real
provider processes on a developer workstation and reads back whatever they
print. The scan runs over every string the record holds, at any depth, so a
path smuggled into a prose sentence is refused exactly like one in a
path-shaped field. A URI authority is not a path and must not be mistaken for
one, or the writer learns to disable the check.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.workflow.evidence.contract_reprobe import (
    UNMEASURED_ROW_CEILING,
    CapabilityProbe,
    CrossProviderReprobe,
    DaemonRpcReprobe,
    EvidenceBasis,
    LimitComparison,
    ProbedStatus,
    breach_rows,
    compare_limit,
    contains_absolute_path,
    contains_vendor_session_id,
    frozen_contract,
    load_cross_provider_reprobe,
    load_daemon_rpc_reprobe,
)

pytestmark = pytest.mark.unit

#: A digest in the spelling every record field expects.
DIGEST = f"sha256:{'a' * 64}"

#: A UUID of the shape both installed runtimes hand back for a session.
SESSION_UUID = "cf40ba54-43d7-40dc-b85e-d7653eb05ab3"

#: A home-directory path fixture, concatenated rather than written whole so
#: the repository's own path-leak gate does not read the fixture as a real
#: leak. The detector under test sees the joined string either way.
HOME_PATH = "/" + "Users/someone/Workspace/repo"

#: A Windows drive root fixture, composed for the same reason.
DRIVE_PATH = "C:" + "\\Users\\someone\\repo"


def dispatch_fields(**overrides: Any) -> dict[str, Any]:
    """Return one valid dispatch observation with *overrides* applied."""
    document: dict[str, Any] = {
        "runtime_id": "claude-code",
        "provider_kind": "claude",
        "run_key": "RUN-00000010",
        "stage": "announced",
        "handshake_disposition": "accepted",
        "model_requested": "haiku",
        "output_tokens": 256,
        "input_tokens": 18,
        "stream_lines": 20,
        "wall_seconds": 7.1,
        "provider_session_digest": DIGEST,
        "subprocess_observed": True,
        "launch_adjustment": None,
    }
    document.update(overrides)
    return document


def capability_fields(**overrides: Any) -> dict[str, Any]:
    """Return one valid capability row with *overrides* applied."""
    document: dict[str, Any] = {
        "runtime_id": "claude-code",
        "capability_id": "streaming",
        "declared": "supported",
        "status": "OK",
        "basis": "dispatched_run",
        "measured": True,
        "evidence": "the driver drained 20 stream-json lines as they arrived",
    }
    document.update(overrides)
    return document


def rung_fields(**overrides: Any) -> dict[str, Any]:
    """Return one valid ladder rung with *overrides* applied."""
    document: dict[str, Any] = {
        "method": "daemon.ping",
        "concurrency": 64,
        "calls": 1280,
        "errors": 0,
        "p50_ms": 1.2,
        "p95_ms": 4.6,
        "max_ms": 9.9,
        "native_runs_in_flight_at_start": 2,
        "native_runs_in_flight_at_end": 2,
    }
    document.update(overrides)
    return document


def comparison_fields(**overrides: Any) -> dict[str, Any]:
    """Return one valid limit comparison with *overrides* applied."""
    document: dict[str, Any] = {
        "limit_name": "ping_p95_ms",
        "direction": "ceiling",
        "frozen_value": 5.32,
        "observed_value": 4.6,
        "unit": "ms",
        "within_limit": True,
        "label": "daemon.ping p95 at c64",
    }
    document.update(overrides)
    return document


def breach_fields(**overrides: Any) -> dict[str, Any]:
    """Return one valid breach row with *overrides* applied."""
    document: dict[str, Any] = {
        "limit_name": "ping_p95_ms",
        "direction": "ceiling",
        "frozen_value": 5.32,
        "observed_value": 9.1,
        "unit": "ms",
        "label": "daemon.ping p95 at c64",
        "successor_contract_id": "MCT-26091802",
        "rationale": "the frozen record stays as measured; a successor carries the new number",
    }
    document.update(overrides)
    return document


def environment_fields(**overrides: Any) -> dict[str, Any]:
    """Return a valid measurement environment with *overrides* applied."""
    document: dict[str, Any] = {
        "scale_band": "dev",
        "population": "two native Runs in one canary",
        "population_size": 24,
        "host_platform": "darwin",
        "toolchain": "python 3.14.3",
    }
    document.update(overrides)
    return document


def cross_fields(**overrides: Any) -> dict[str, Any]:
    """Return a valid cross-provider record with *overrides* applied."""
    document: dict[str, Any] = {
        "schema_version": "contract-reprobe/v1",
        "contract_id": "MCT-26081302",
        "probe_command": "uv run python probe.py --live",
        "observed_at": datetime(2026, 9, 18, 14, 0, tzinfo=UTC).isoformat(),
        "dispatches": [dispatch_fields()],
        "comparisons": [comparison_fields(limit_name="runtimes_installed", direction="floor")],
        "breaches": [],
        "capability_rows": [capability_fields()],
        "boundary": "one workstation, one turn per runtime",
        "environment": environment_fields(),
    }
    document.update(overrides)
    return document


def rpc_fields(**overrides: Any) -> dict[str, Any]:
    """Return a valid daemon-RPC record with *overrides* applied."""
    document: dict[str, Any] = {
        "schema_version": "contract-reprobe/v1",
        "contract_id": "MCT-26081303",
        "probe_command": "uv run python probe.py --live",
        "observed_at": datetime(2026, 9, 18, 14, 0, tzinfo=UTC).isoformat(),
        "dispatches": [dispatch_fields()],
        "comparisons": [comparison_fields()],
        "breaches": [],
        "ladder_rungs": [rung_fields()],
        "rpc_errors": 0,
        "boundary": "a canary-scoped daemon on its own socket",
        "environment": environment_fields(),
    }
    document.update(overrides)
    return document


# ---- the unknown field ------------------------------------------------------


def test_cross_provider_reprobe_refuses_an_unknown_field() -> None:
    """A drifted writer is refused at the boundary, not read half-way."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        CrossProviderReprobe.model_validate(cross_fields(surprise=1))


def test_daemon_rpc_reprobe_refuses_an_unknown_field() -> None:
    """The RPC record is strict for the same reason the other one is."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        DaemonRpcReprobe.model_validate(rpc_fields(surprise=1))


def test_a_nested_row_refuses_an_unknown_field() -> None:
    """Strictness reaches the rows, not only the record that holds them."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        CrossProviderReprobe.model_validate(
            cross_fields(capability_rows=[capability_fields(surprise=1)])
        )


# ---- the breach that names no successor --------------------------------------


def test_a_breach_row_without_a_successor_contract_is_refused() -> None:
    """A new number with nowhere to go invites a widened frozen limit."""
    fields = breach_fields()
    del fields["successor_contract_id"]
    with pytest.raises(ValidationError, match="successor_contract_id"):
        DaemonRpcReprobe.model_validate(
            rpc_fields(
                comparisons=[comparison_fields(within_limit=False, observed_value=9.1)],
                breaches=[fields],
            )
        )


def test_a_breach_row_with_a_malformed_successor_contract_is_refused() -> None:
    """The successor is a contract id or it is not a successor."""
    with pytest.raises(ValidationError, match="successor_contract_id"):
        DaemonRpcReprobe.model_validate(
            rpc_fields(
                comparisons=[comparison_fields(within_limit=False, observed_value=9.1)],
                breaches=[breach_fields(successor_contract_id="MCT-123")],
            )
        )


def test_a_breached_comparison_with_no_breach_row_is_refused() -> None:
    """Leaving a limit silently is the failure this record exists to stop."""
    with pytest.raises(ValidationError, match="needs a successor contract id"):
        DaemonRpcReprobe.model_validate(
            rpc_fields(comparisons=[comparison_fields(within_limit=False, observed_value=9.1)])
        )


def test_a_breach_row_for_a_comparison_inside_its_limit_is_refused() -> None:
    """A breach nobody measured would licence an unneeded successor."""
    with pytest.raises(ValidationError, match="must name a comparison that left its limit"):
        DaemonRpcReprobe.model_validate(rpc_fields(breaches=[breach_fields()]))


def test_a_breached_comparison_validates_alongside_its_successor() -> None:
    """An honestly recorded breach is a valid record, not a rejected one."""
    record = DaemonRpcReprobe.model_validate(
        rpc_fields(
            comparisons=[comparison_fields(within_limit=False, observed_value=9.1)],
            breaches=[breach_fields()],
        )
    )
    assert record.breaches[0].successor_contract_id == "MCT-26091802"


# ---- the host path and the vendor session id ---------------------------------


@pytest.mark.parametrize(
    "value",
    [
        HOME_PATH,
        "/private/var/folders/f1/tmpabc/repo",
        "~/Workspace/repo",
        DRIVE_PATH,
    ],
)
def test_an_absolute_path_in_any_value_is_refused(value: str) -> None:
    """A workstation path in prose is refused exactly like one in a field."""
    with pytest.raises(ValidationError, match="must be scrubbed"):
        CrossProviderReprobe.model_validate(cross_fields(boundary=f"measured under {value}"))


def test_an_absolute_path_deep_in_a_row_is_refused() -> None:
    """The scan reaches rows nested inside lists, not just the top level."""
    with pytest.raises(ValidationError, match="capability_rows"):
        CrossProviderReprobe.model_validate(
            cross_fields(
                capability_rows=[capability_fields(evidence=f"the child ran in {HOME_PATH}")]
            )
        )


def test_a_uri_authority_is_not_mistaken_for_a_path() -> None:
    """A check that fires on ``driver://x/y`` is a check writers disable."""
    record = CrossProviderReprobe.model_validate(
        cross_fields(boundary="bound to driver://codex-app-server/v1 under repo:src/eawf")
    )
    assert "driver://codex-app-server/v1" in record.boundary


def test_a_raw_vendor_session_id_in_any_value_is_refused() -> None:
    """A session is cited by digest; the identifier itself never lands."""
    with pytest.raises(ValidationError, match="raw vendor session id"):
        CrossProviderReprobe.model_validate(
            cross_fields(dispatches=[dispatch_fields(launch_adjustment=f"resumed {SESSION_UUID}")])
        )


def test_a_session_digest_is_not_mistaken_for_a_session_id() -> None:
    """The scrubbed citation must survive the check that rejects the raw one."""
    record = CrossProviderReprobe.model_validate(cross_fields())
    assert record.dispatches[0].provider_session_digest == DIGEST


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", False),
        ("/", False),
        (f"{HOME_PATH}/", True),
        (".ea/artifacts/evidence/file.json", False),
        ("repo:src/eawf/workflow/evidence/contract_reprobe.py", False),
        ("urn:eawf:v1:artifact:EAWF/MCT-26081302", False),
        ("driver://claude-sdk/v1", False),
        ("see /tmp/x/ for the scratch tree", True),
    ],
)
def test_contains_absolute_path_reads_the_shapes_a_record_carries(
    value: str, expected: bool
) -> None:
    """The detector separates paths from the URIs a record legitimately holds."""
    assert contains_absolute_path(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", False),
        (SESSION_UUID, True),
        (SESSION_UUID.upper(), True),
        (f"session {SESSION_UUID} ended", True),
        (DIGEST, False),
        ("RUN-00000010", False),
    ],
)
def test_contains_vendor_session_id_reads_the_shapes_a_record_carries(
    value: str, expected: bool
) -> None:
    """Both installed runtimes hand back a UUID, so the UUID shape is the rule."""
    assert contains_vendor_session_id(value) is expected


# ---- the rows' own rules -----------------------------------------------------


def test_a_measured_row_that_resolved_nothing_is_refused() -> None:
    """A row cannot claim a measurement and an unresolved status at once."""
    with pytest.raises(ValidationError, match="UNKNOWN row is unmeasured"):
        CapabilityProbe.model_validate(capability_fields(status="UNKNOWN", measured=True))


def test_an_unmeasured_row_claiming_a_status_is_refused() -> None:
    """A row nobody reached cannot report OK either."""
    with pytest.raises(ValidationError, match="UNKNOWN row is unmeasured"):
        CapabilityProbe.model_validate(capability_fields(status="OK", measured=False))


def test_a_row_outside_the_declared_vocabulary_is_refused() -> None:
    """The declared cell comes from the capability matrix or from nowhere."""
    with pytest.raises(ValidationError, match="declared"):
        CapabilityProbe.model_validate(capability_fields(declared="probably"))


def test_a_run_that_generated_nothing_is_not_a_dispatched_run() -> None:
    """Zero output tokens means the child never generated; the record says so."""
    with pytest.raises(ValidationError, match="output_tokens"):
        CrossProviderReprobe.model_validate(
            cross_fields(dispatches=[dispatch_fields(output_tokens=0)])
        )


def test_a_single_output_token_is_enough_for_a_dispatched_run() -> None:
    """One token is the floor, so the boundary sits where the record says."""
    record = CrossProviderReprobe.model_validate(
        cross_fields(dispatches=[dispatch_fields(output_tokens=1)])
    )
    assert record.dispatches[0].output_tokens == 1


@pytest.mark.parametrize("field", ["dispatches", "comparisons", "capability_rows"])
def test_an_empty_collection_is_refused(field: str) -> None:
    """A record with nothing in it measured nothing and must not validate."""
    with pytest.raises(ValidationError, match=field):
        CrossProviderReprobe.model_validate(cross_fields(**{field: []}))


def test_a_rung_that_completed_no_call_is_refused() -> None:
    """A rung with no call is an absent rung, not a measured one."""
    with pytest.raises(ValidationError, match="calls"):
        DaemonRpcReprobe.model_validate(rpc_fields(ladder_rungs=[rung_fields(calls=0)]))


def test_the_headline_error_count_must_match_the_rungs() -> None:
    """A clean headline over dirty rungs is the shape of a flattering record."""
    with pytest.raises(ValidationError, match="rpc_errors=0 but the rungs hold 3"):
        DaemonRpcReprobe.model_validate(rpc_fields(ladder_rungs=[rung_fields(errors=3)]))


def test_the_record_reports_the_concurrency_it_reached() -> None:
    """The ladder's own top rung is what a consumer reads, not a claim."""
    record = DaemonRpcReprobe.model_validate(
        rpc_fields(ladder_rungs=[rung_fields(concurrency=1), rung_fields(concurrency=64)])
    )
    assert record.concurrency_reached == 64


def test_the_record_counts_its_own_unmeasured_and_drift_rows() -> None:
    """Consumers read the counts off the rows rather than off a headline."""
    record = CrossProviderReprobe.model_validate(
        cross_fields(
            capability_rows=[
                capability_fields(),
                capability_fields(status="DRIFT", capability_id="skills"),
                capability_fields(
                    status="UNKNOWN", measured=False, runtime_id="opencode", basis="runtime_surface"
                ),
            ]
        )
    )
    assert (record.unmeasured_rows, record.drift_rows) == (1, 1)


# ---- comparing against the frozen contract ------------------------------------


def test_compare_limit_admits_a_value_exactly_on_a_ceiling() -> None:
    """The frozen number itself is inside the limit, not outside it."""
    contract = frozen_contract("MCT-26081303")
    comparison = compare_limit(contract, "ping_p95_ms", 5.32, "daemon.ping p95 at c64")
    assert comparison.within_limit is True


def test_compare_limit_refuses_a_value_just_past_a_ceiling() -> None:
    """One step past the frozen ceiling is a breach, and reads as one."""
    contract = frozen_contract("MCT-26081303")
    comparison = compare_limit(contract, "ping_p95_ms", 5.33, "daemon.ping p95 at c64")
    assert comparison.within_limit is False


def test_compare_limit_admits_a_value_exactly_on_a_floor() -> None:
    """A floor is satisfied at its own value, the mirror of the ceiling."""
    contract = frozen_contract("MCT-26081302")
    comparison = compare_limit(contract, "runtimes_installed", 2.0, "installed runtimes")
    assert comparison.within_limit is True


def test_compare_limit_refuses_a_value_just_under_a_floor() -> None:
    """One runtime short of the floor is a breach of the floor."""
    contract = frozen_contract("MCT-26081302")
    comparison = compare_limit(contract, "runtimes_installed", 1.0, "installed runtimes")
    assert comparison.within_limit is False


def test_compare_limit_refuses_a_limit_the_contract_does_not_declare() -> None:
    """A comparison against an invented limit compares against nothing."""
    with pytest.raises(KeyError, match="declares no limit named"):
        compare_limit(frozen_contract("MCT-26081302"), "invented_limit", 1.0, "nothing")


def test_frozen_contract_refuses_an_unregistered_id() -> None:
    """A re-probe of a contract nobody froze has nothing to compare with."""
    with pytest.raises(KeyError, match="no measured contract is registered"):
        frozen_contract("MCT-99999999")


def test_breach_rows_are_minted_only_for_comparisons_that_left_a_limit() -> None:
    """The helper mints exactly the rows the record will be asked for."""
    comparisons = [
        LimitComparison.model_validate(comparison_fields()),
        LimitComparison.model_validate(comparison_fields(within_limit=False, observed_value=9.1)),
    ]
    rows = breach_rows(
        comparisons, successor_contract_id="MCT-26091802", rationale="a successor carries it"
    )
    assert [row.observed_value for row in rows] == [9.1]


def test_breach_rows_over_a_clean_ladder_are_empty() -> None:
    """A re-probe that breached nothing mints no successor obligation."""
    rows = breach_rows(
        [LimitComparison.model_validate(comparison_fields())],
        successor_contract_id="MCT-26091802",
        rationale="unused",
    )
    assert rows == ()


# ---- reading a recorded file --------------------------------------------------


def test_loading_a_record_that_was_never_written_names_the_file(tmp_path: Path) -> None:
    """A missing observation file is a missing measurement, and says so."""
    with pytest.raises(FileNotFoundError, match="no re-probe record is recorded at"):
        load_cross_provider_reprobe(tmp_path / "absent.json")


def test_loading_a_record_that_is_not_json_names_the_file(tmp_path: Path) -> None:
    """A truncated write is a parse failure at the reader, not a KeyError later."""
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="is not valid JSON"):
        load_daemon_rpc_reprobe(path)


def test_loading_a_record_validates_it_rather_than_trusting_it(tmp_path: Path) -> None:
    """A file on disk is a claim; the loader is where it becomes a record."""
    path = tmp_path / "record.json"
    path.write_text(json.dumps(cross_fields(contract_id="nope")), encoding="utf-8")
    with pytest.raises(ValidationError, match="contract_id"):
        load_cross_provider_reprobe(path)


def test_loading_a_written_record_round_trips_it(tmp_path: Path) -> None:
    """What the writer dumped is what the reader validates back."""
    path = tmp_path / "record.json"
    path.write_text(json.dumps(cross_fields()), encoding="utf-8")
    record = load_cross_provider_reprobe(path)
    assert record.contract_id == "MCT-26081302"
    assert record.capability_rows[0].status is ProbedStatus.OK
    assert record.capability_rows[0].basis is EvidenceBasis.DISPATCHED_RUN


def test_the_unmeasured_ceiling_is_one_absent_runtime() -> None:
    """The driven ceiling is eight because one absent binary is eight rows."""
    assert UNMEASURED_ROW_CEILING == 8
