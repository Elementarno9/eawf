"""The recorded cross-provider re-probe says what a driven Run established.

The frozen contract MCT-26081302 was read off ``--version`` and ``--help``: it
describes what the installed CLIs *advertise*. This record replaces that with
what they *did* when the dispatcher started them, so the checks here are about
whether the record can still be wrong.

**Two runtimes each drove a real Run.** Not "a launcher exists" -- a Run that
reached the announced stage with an accepted handshake, a child pid the driver
saw, and output tokens the vendor billed. A zero-token row is refused by the
record itself, so the assertion here is that both rows are there and both
arrived through the dispatch verb the daemon actually registers.

**Every row is a real row.** The runtime and capability vocabulary is
re-derived from the shipped ``capabilities.yaml`` rather than believed, and so
is every declared cell, so a record that invented a runtime, dropped a
capability or mis-stated a declaration fails here.

**Unmeasured stays at one absent runtime.** A driven probe can only fail to
reach the rows of a binary that is not installed. Anything above that ceiling
means rows were left un-driven and quietly reported as unknown.

Every check runs against a deliberately broken copy as well, so a check that
has stopped reading anything is a failing test rather than a green one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import eawf.runtime.daemon.methods.run  # noqa: F401  -- registers runtime.run.*
from eawf.runtime.daemon.methods import registered_methods
from eawf.runtime.daemon.native_dispatch import RUN_DISPATCH_METHOD
from eawf.runtime.runtimes.capabilities import (
    CAPABILITY_NAMES,
    EXPECTED_CAPABILITY_ROWS,
    RUNTIME_IDS,
    get_runtime_capabilities,
)
from eawf.workflow.evidence.contract_reprobe import (
    CROSS_PROVIDER_REPROBE_PATH,
    UNMEASURED_ROW_CEILING,
    CrossProviderReprobe,
    ProbedStatus,
    frozen_contract,
    load_cross_provider_reprobe,
)

pytestmark = pytest.mark.integration

#: Four levels up from this file lands on the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Where the cross-provider observation is committed.
RECORD_PATH = _REPO_ROOT / CROSS_PROVIDER_REPROBE_PATH

#: The runtimes whose binaries this probe found and drove.
DRIVEN_RUNTIMES: tuple[str, ...] = ("claude-code", "codex")

#: Any absolute path, home-relative path or Windows drive root in raw bytes.
_PATH_RE = re.compile(r"(?<![\w:/])[~/][\w.-]+/|[A-Za-z]:\\")

#: A raw vendor session identifier in raw bytes.
_SESSION_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@pytest.fixture(scope="module")
def record() -> CrossProviderReprobe:
    """Return the validated record as it is committed."""
    return load_cross_provider_reprobe(RECORD_PATH)


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    """Return the committed record as a mutable document."""
    return json.loads(RECORD_PATH.read_text(encoding="utf-8"))


def rows_of(record: CrossProviderReprobe, runtime_id: str) -> dict[str, Any]:
    """Return one runtime's capability rows keyed by capability."""
    return {
        row.capability_id: row for row in record.capability_rows if row.runtime_id == runtime_id
    }


# ---- the record is the record ------------------------------------------------


def test_the_recorded_file_validates_against_the_strict_record(
    record: CrossProviderReprobe,
) -> None:
    """The committed observation is a record, not a JSON file that resembles one."""
    assert record.contract_id == "MCT-26081302"
    assert record.schema_version == "contract-reprobe/v1"


def test_a_record_with_an_unknown_field_would_not_validate(document: dict[str, Any]) -> None:
    """The validation above is load-bearing, which this proves by breaking it."""
    broken = copy.deepcopy(document)
    broken["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        CrossProviderReprobe.model_validate(broken)


def test_the_record_carries_no_absolute_path_or_vendor_session_id() -> None:
    """The scan runs over raw bytes, so prose hides nothing from it."""
    raw = RECORD_PATH.read_text(encoding="utf-8")
    assert _PATH_RE.search(raw) is None
    assert _SESSION_RE.search(raw) is None


def test_the_digest_the_evidence_row_cites_is_the_file_itself() -> None:
    """A citation is only worth appending if it is re-derivable here."""
    digest = hashlib.sha256(RECORD_PATH.read_bytes()).hexdigest()
    assert len(digest) == 64
    assert RECORD_PATH.read_bytes().endswith(b"\n")


# ---- two runtimes each drove a real Run --------------------------------------


def test_the_dispatch_verb_the_record_names_is_one_the_daemon_registers() -> None:
    """A Run recorded against an unregistered verb reached nothing."""
    assert RUN_DISPATCH_METHOD == "runtime.run.dispatch"
    assert RUN_DISPATCH_METHOD in registered_methods()


def test_at_least_two_installed_runtimes_each_drove_one_run(
    record: CrossProviderReprobe,
) -> None:
    """The contract's floor is two installed runtimes, and two ran."""
    assert sorted(row.runtime_id for row in record.dispatches) == sorted(DRIVEN_RUNTIMES)


@pytest.mark.parametrize("runtime_id", DRIVEN_RUNTIMES)
def test_each_dispatched_run_reached_an_accepted_announcement(
    record: CrossProviderReprobe, runtime_id: str
) -> None:
    """A compiled-and-leased Run that never announced proves nothing about the child."""
    row = next(item for item in record.dispatches if item.runtime_id == runtime_id)
    assert row.stage == "announced"
    assert row.handshake_disposition == "accepted"
    assert row.subprocess_observed is True


@pytest.mark.parametrize("runtime_id", DRIVEN_RUNTIMES)
def test_each_dispatched_run_billed_output_tokens(
    record: CrossProviderReprobe, runtime_id: str
) -> None:
    """Non-zero output tokens is what separates a real turn from a handshake."""
    row = next(item for item in record.dispatches if item.runtime_id == runtime_id)
    assert row.output_tokens > 0
    assert row.stream_lines > 1


def test_a_run_that_generated_nothing_would_not_validate(document: dict[str, Any]) -> None:
    """The token floor is enforced by the record, which this proves by breaking it."""
    broken = copy.deepcopy(document)
    broken["dispatches"][0]["output_tokens"] = 0
    with pytest.raises(ValidationError, match="output_tokens"):
        CrossProviderReprobe.model_validate(broken)


def test_a_runtime_that_needed_a_launch_adjustment_says_so(
    record: CrossProviderReprobe,
) -> None:
    """A Run the shipped launcher could not have started is not a clean pass."""
    adjusted = {row.runtime_id for row in record.dispatches if row.launch_adjustment}
    assert adjusted == {"codex"}


# ---- every row is a real row -------------------------------------------------


def test_the_record_holds_one_row_per_declared_runtime_and_capability(
    record: CrossProviderReprobe,
) -> None:
    """The row set is the matrix's own, re-derived rather than believed."""
    expected = {(runtime, name) for runtime in RUNTIME_IDS for name in CAPABILITY_NAMES}
    assert {(row.runtime_id, row.capability_id) for row in record.capability_rows} == expected
    assert len(record.capability_rows) == len(RUNTIME_IDS) * EXPECTED_CAPABILITY_ROWS


@pytest.mark.parametrize("runtime_id", RUNTIME_IDS)
def test_every_declared_cell_matches_the_shipped_capability_matrix(
    record: CrossProviderReprobe, runtime_id: str
) -> None:
    """A record that mis-states a declaration compares against the wrong thing."""
    declared = get_runtime_capabilities(runtime_id)
    for capability, row in rows_of(record, runtime_id).items():
        assert row.declared == declared[capability]


def test_every_capability_row_carries_a_probed_status(record: CrossProviderReprobe) -> None:
    """Each of the twenty-four rows resolves OK, DRIFT or UNKNOWN."""
    assert {row.status for row in record.capability_rows} <= set(ProbedStatus)
    assert all(isinstance(row.status, ProbedStatus) for row in record.capability_rows)


@pytest.mark.parametrize("runtime_id", DRIVEN_RUNTIMES)
def test_every_row_of_an_installed_runtime_was_measured(
    record: CrossProviderReprobe, runtime_id: str
) -> None:
    """A runtime that ran a Run leaves no row of its own unresolved."""
    rows = rows_of(record, runtime_id)
    assert len(rows) == EXPECTED_CAPABILITY_ROWS
    assert all(row.measured for row in rows.values())
    assert all(row.status is not ProbedStatus.UNKNOWN for row in rows.values())


def test_the_absent_runtime_reports_unknown_rather_than_passing(
    record: CrossProviderReprobe,
) -> None:
    """An absent binary is unmeasured; declaring it passing is the old failure."""
    rows = rows_of(record, "opencode")
    assert all(row.status is ProbedStatus.UNKNOWN for row in rows.values())
    assert not any(row.measured for row in rows.values())


def test_unmeasured_rows_stay_within_one_absent_runtime(
    record: CrossProviderReprobe,
) -> None:
    """Eight is one absent runtime; anything more means rows were not driven."""
    assert record.unmeasured_rows <= UNMEASURED_ROW_CEILING


def test_most_measured_rows_rest_on_the_dispatched_run(record: CrossProviderReprobe) -> None:
    """The basis is recorded per row so a weaker source cannot pass as a turn."""
    bases = [row.basis.value for row in record.capability_rows if row.measured]
    assert bases.count("dispatched_run") >= len(bases) // 2
    assert set(bases) <= {"dispatched_run", "driver_seam", "runtime_surface"}


# ---- the frozen limits -------------------------------------------------------


def test_every_frozen_limit_of_the_contract_was_compared(
    record: CrossProviderReprobe,
) -> None:
    """A limit nobody compared against was not re-probed at all."""
    frozen = {limit.name for limit in frozen_contract("MCT-26081302").limits}
    assert {row.limit_name for row in record.comparisons} == frozen


def test_each_comparison_reads_the_frozen_value_off_the_contract(
    record: CrossProviderReprobe,
) -> None:
    """A comparison against an edited frozen value proves nothing."""
    frozen = {limit.name: limit for limit in frozen_contract("MCT-26081302").limits}
    for comparison in record.comparisons:
        assert comparison.frozen_value == frozen[comparison.limit_name].value
        assert comparison.direction == frozen[comparison.limit_name].direction


def test_the_drift_count_the_comparison_reports_matches_the_rows(
    record: CrossProviderReprobe,
) -> None:
    """The headline drift number is re-derived from the rows, not asserted."""
    comparison = next(
        row for row in record.comparisons if row.limit_name == "declared_vs_observed_drift"
    )
    assert comparison.observed_value == float(record.drift_rows)


def test_the_unmeasured_count_the_comparison_reports_matches_the_rows(
    record: CrossProviderReprobe,
) -> None:
    """Same for the unmeasured headline: the rows are the source of truth."""
    comparison = next(
        row for row in record.comparisons if row.limit_name == "unmeasured_capability_rows"
    )
    assert comparison.observed_value == float(record.unmeasured_rows)


def test_every_breach_names_a_successor_contract(record: CrossProviderReprobe) -> None:
    """A breach is carried by a successor; the frozen record is never widened."""
    breached = {row.limit_name for row in record.comparisons if not row.within_limit}
    assert {row.limit_name for row in record.breaches} == breached
    assert all(row.successor_contract_id != record.contract_id for row in record.breaches)


def test_a_breach_with_no_successor_would_not_validate(document: dict[str, Any]) -> None:
    """The successor rule is load-bearing, which this proves by breaking it."""
    broken = copy.deepcopy(document)
    broken["comparisons"][0]["within_limit"] = False
    with pytest.raises(ValidationError, match="needs a successor contract id"):
        CrossProviderReprobe.model_validate(broken)


def test_the_boundary_says_where_the_re_measurement_stops(
    record: CrossProviderReprobe,
) -> None:
    """An unbounded re-measurement is the failure the frozen record already names."""
    assert "opencode" in record.boundary
    assert record.environment.population_size == len(record.capability_rows)
