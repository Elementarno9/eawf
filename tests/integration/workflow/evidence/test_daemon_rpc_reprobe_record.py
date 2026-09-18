"""The recorded RPC re-probe says what the daemon did under real dispatch.

The frozen contract MCT-26081303 measured an idle daemon: nothing was
dispatching while the ladder ran, so the numbers describe a machine with
nothing else to do. This record replaces that with a ladder driven while two
real provider children were alive in the same canary, and the checks here are
about whether that claim survives inspection.

**Concurrency is asserted per rung, not once.** A ladder that started with two
children in flight and finished against none measured an idle daemon for most
of its length. Every rung therefore records the live-child count at its start
AND at its end, and both are checked.

**The ceiling is the contract's own.** The ladder climbs to the frozen
``ping_concurrency`` of 64 and no further, because a rung past a frozen ceiling
is outside the envelope rather than a better result. Every rung's p95 is held
up against the frozen ``ping_p95_ms``.

**The evidence row cites this file's digest.** The store append is the
operator's, through the daemon, because that store is daemon-canonical -- so
what is enforced here is that the row is well-formed, that its digest is
re-derived from the bytes on disk, and that any row already in the store cites
the current digest rather than a stale one.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.workflow.evidence.contract_reprobe import (
    CROSS_PROVIDER_REPROBE_PATH,
    DAEMON_RPC_REPROBE_PATH,
    DaemonRpcReprobe,
    frozen_contract,
    load_daemon_rpc_reprobe,
)

pytestmark = pytest.mark.integration

#: Four levels up from this file lands on the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Where the daemon-RPC observation is committed.
RECORD_PATH = _REPO_ROOT / DAEMON_RPC_REPROBE_PATH

#: The append-only evidence store the citation rows land in.
EVIDENCE_STORE = _REPO_ROOT / ".ea/store/evidence.jsonl"

#: The scope every re-probe evidence row is filed under.
SCOPE_ID = "P33-I01-W85"

#: The two contracts this wave re-probed, and the file each is cited by.
CITATIONS: tuple[tuple[str, str], ...] = (
    ("MCT-26081302", CROSS_PROVIDER_REPROBE_PATH),
    ("MCT-26081303", DAEMON_RPC_REPROBE_PATH),
)

#: How many native provider children must be alive for a rung to count.
REQUIRED_IN_FLIGHT = 2

#: Any absolute path, home-relative path or Windows drive root in raw bytes.
_PATH_RE = re.compile(r"(?<![\w:/])[~/][\w.-]+/|[A-Za-z]:\\")

#: A raw vendor session identifier in raw bytes.
_SESSION_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@pytest.fixture(scope="module")
def record() -> DaemonRpcReprobe:
    """Return the validated record as it is committed."""
    return load_daemon_rpc_reprobe(RECORD_PATH)


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    """Return the committed record as a mutable document."""
    return json.loads(RECORD_PATH.read_text(encoding="utf-8"))


def file_digest(path: Path) -> str:
    """Return the sha256 digest of *path* in the store's spelling."""
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def citation_rows(contract_id: str) -> list[dict[str, Any]]:
    """Return every evidence row in the store that cites *contract_id*."""
    if not EVIDENCE_STORE.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in EVIDENCE_STORE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or contract_id not in line:
            continue
        envelope = json.loads(line)
        payload = envelope.get("payload", {})
        if contract_id in payload.get("refs", []) and payload.get("scope_id") == SCOPE_ID:
            rows.append(envelope)
    return rows


def citation_envelope(contract_id: str, repo_relative_path: str) -> Envelope:
    """Return the evidence envelope that cites one re-probe observation.

    Args:
        contract_id: The frozen contract the observation re-probes.
        repo_relative_path: Repo-relative path of the observation file.

    Returns:
        The envelope the operator appends through the daemon.
    """
    digest = file_digest(_REPO_ROOT / repo_relative_path)
    payload = EvidenceRecord(
        id="EV-000000000000",
        scope_id=SCOPE_ID,
        produced_by="canary",
        evidence_kind="deterministic",
        status="pass",
        summary=(
            f"{contract_id} re-probed under concurrent native dispatch; "
            f"observation recorded at {repo_relative_path}"
        ),
        refs=[contract_id, SCOPE_ID],
        metrics={"observation_path": repo_relative_path, "observation_digest": digest},
        created_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    return Envelope(
        id=payload.id,
        kind=StoreKind.EVIDENCE,
        scope_id=SCOPE_ID,
        created_at=payload.created_at,
        summary=payload.summary,
        payload=payload.model_dump(mode="json"),
    )


# ---- the record is the record ------------------------------------------------


def test_the_recorded_file_validates_against_the_strict_record(
    record: DaemonRpcReprobe,
) -> None:
    """The committed observation is a record, not a file that resembles one."""
    assert record.contract_id == "MCT-26081303"
    assert record.schema_version == "contract-reprobe/v1"


def test_a_record_with_an_unknown_field_would_not_validate(document: dict[str, Any]) -> None:
    """The validation above is load-bearing, which this proves by breaking it."""
    broken = copy.deepcopy(document)
    broken["surprise"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        DaemonRpcReprobe.model_validate(broken)


def test_the_record_carries_no_absolute_path_or_vendor_session_id() -> None:
    """The scan runs over raw bytes, so prose hides nothing from it."""
    raw = RECORD_PATH.read_text(encoding="utf-8")
    assert _PATH_RE.search(raw) is None
    assert _SESSION_RE.search(raw) is None


# ---- the ladder ran under real dispatch --------------------------------------


def test_the_ladder_ran_while_two_native_runs_were_in_flight(
    record: DaemonRpcReprobe,
) -> None:
    """A rung that finished against an idle daemon measured the frozen case."""
    for rung in record.ladder_rungs:
        assert rung.native_runs_in_flight_at_start >= REQUIRED_IN_FLIGHT
        assert rung.native_runs_in_flight_at_end >= REQUIRED_IN_FLIGHT


def test_the_runs_in_flight_are_the_dispatches_the_record_names(
    record: DaemonRpcReprobe,
) -> None:
    """The children in flight are the ones this record accounts for."""
    assert len(record.dispatches) >= REQUIRED_IN_FLIGHT
    assert all(row.subprocess_observed for row in record.dispatches)
    assert all(row.output_tokens > 0 for row in record.dispatches)


def test_the_ladder_climbs_to_the_contract_s_own_concurrency_ceiling(
    record: DaemonRpcReprobe,
) -> None:
    """The frozen ping ceiling is 64, and the ladder reaches exactly it."""
    ceiling = next(
        limit
        for limit in frozen_contract("MCT-26081303").limits
        if limit.name == "ping_concurrency"
    )
    assert ceiling.value == 64.0
    assert record.concurrency_reached == int(ceiling.value)


def test_the_ladder_climbs_by_doubling_from_one(record: DaemonRpcReprobe) -> None:
    """Rungs that skip levels cannot locate a knee between them."""
    assert [rung.concurrency for rung in record.ladder_rungs] == [1, 2, 4, 8, 16, 32, 64]


def test_every_rung_drove_the_ping_method_only(record: DaemonRpcReprobe) -> None:
    """A read or a write at the live daemon is outside what this re-probe claims."""
    assert {rung.method for rung in record.ladder_rungs} == {"daemon.ping"}


def test_the_ladder_reported_no_rpc_error_at_any_rung(record: DaemonRpcReprobe) -> None:
    """Zero errors through the frozen ceiling is the contract's own claim."""
    assert record.rpc_errors == 0
    assert all(rung.errors == 0 for rung in record.ladder_rungs)


def test_an_error_at_a_rung_would_not_validate_against_a_clean_headline(
    document: dict[str, Any],
) -> None:
    """A flattering headline over dirty rungs is refused by the record."""
    broken = copy.deepcopy(document)
    broken["ladder_rungs"][0]["errors"] = 1
    with pytest.raises(ValidationError, match="rpc_errors=0 but the rungs hold 1"):
        DaemonRpcReprobe.model_validate(broken)


def test_every_rung_completed_the_calls_its_concurrency_implies(
    record: DaemonRpcReprobe,
) -> None:
    """A rung whose workers died would report fewer calls than it opened."""
    for rung in record.ladder_rungs:
        assert rung.calls % rung.concurrency == 0
        assert rung.calls >= rung.concurrency


# ---- each p95 against the frozen limit ---------------------------------------


def test_every_rung_p95_is_compared_with_the_frozen_latency_limit(
    record: DaemonRpcReprobe,
) -> None:
    """A rung with no comparison was measured and then not read."""
    labels = {row.label for row in record.comparisons if row.limit_name == "ping_p95_ms"}
    assert labels == {f"daemon.ping p95 at c{rung.concurrency}" for rung in record.ladder_rungs}


def test_each_comparison_repeats_the_rung_it_came_from(record: DaemonRpcReprobe) -> None:
    """A comparison whose number drifted from its rung compares nothing."""
    by_label = {row.label: row for row in record.comparisons if row.limit_name == "ping_p95_ms"}
    for rung in record.ladder_rungs:
        assert by_label[f"daemon.ping p95 at c{rung.concurrency}"].observed_value == rung.p95_ms


def test_each_comparison_reads_the_frozen_value_off_the_contract(
    record: DaemonRpcReprobe,
) -> None:
    """A comparison against an edited frozen value proves nothing."""
    frozen = {limit.name: limit for limit in frozen_contract("MCT-26081303").limits}
    for comparison in record.comparisons:
        assert comparison.frozen_value == frozen[comparison.limit_name].value
        assert comparison.direction == frozen[comparison.limit_name].direction


def test_every_breach_names_a_successor_contract(record: DaemonRpcReprobe) -> None:
    """A latency past the frozen ceiling belongs to a successor, not to an edit."""
    breached = {(row.limit_name, row.label) for row in record.comparisons if not row.within_limit}
    assert {(row.limit_name, row.label) for row in record.breaches} == breached
    assert all(row.successor_contract_id != record.contract_id for row in record.breaches)


def test_a_breach_with_no_successor_would_not_validate(document: dict[str, Any]) -> None:
    """The successor rule is load-bearing, which this proves by breaking it."""
    broken = copy.deepcopy(document)
    broken["comparisons"][0]["within_limit"] = False
    with pytest.raises(ValidationError, match="needs a successor contract id"):
        DaemonRpcReprobe.model_validate(broken)


def test_the_boundary_says_the_frozen_numbers_came_from_an_idle_daemon(
    record: DaemonRpcReprobe,
) -> None:
    """Without that sentence a reader compares two different populations."""
    assert "idle daemon" in record.boundary
    assert record.environment.population_size == sum(rung.calls for rung in record.ladder_rungs)


# ---- the evidence row that cites the file ------------------------------------


@pytest.mark.parametrize(("contract_id", "path"), CITATIONS)
def test_the_evidence_row_citing_each_contract_is_well_formed(contract_id: str, path: str) -> None:
    """The row the operator appends validates before it is ever appended."""
    envelope = citation_envelope(contract_id, path)
    payload = EvidenceRecord.model_validate(envelope.payload)
    assert envelope.kind is StoreKind.EVIDENCE
    assert payload.refs == [contract_id, SCOPE_ID]
    assert payload.metrics is not None
    assert payload.metrics["observation_digest"] == file_digest(_REPO_ROOT / path)


@pytest.mark.parametrize(("contract_id", "path"), CITATIONS)
def test_the_store_holds_at_most_one_row_per_contract_for_this_scope(
    contract_id: str, path: str
) -> None:
    """Two rows for one contract leave a reader unable to tell which one holds."""
    assert len(citation_rows(contract_id)) <= 1


@pytest.mark.parametrize(("contract_id", "path"), CITATIONS)
def test_any_stored_row_cites_the_digest_of_the_file_as_it_is_now(
    contract_id: str, path: str
) -> None:
    """A row surviving an edit of the observation it cites is the drift to catch."""
    for envelope in citation_rows(contract_id):
        metrics = envelope["payload"].get("metrics") or {}
        assert metrics.get("observation_path") == path
        assert metrics.get("observation_digest") == file_digest(_REPO_ROOT / path)
