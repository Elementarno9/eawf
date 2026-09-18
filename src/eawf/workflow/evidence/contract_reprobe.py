"""Strict carrier for a measured contract re-probed against live behaviour.

A :class:`~eawf.kernel.spec.measured_contract.MeasuredContract` freezes what
one probe saw on one day. When the same surface is measured again -- this
time by driving it rather than by reading what it advertises -- the second
observation needs a home that cannot quietly become the first. This module
is that home.

Three rules make the record trustworthy rather than merely typed:

*Every value is scrubbed.* A re-probe that drives real provider processes
reads back whatever those processes print, which on a developer
workstation includes home directories, repository roots and vendor session
identifiers. :func:`contains_absolute_path` and
:func:`contains_vendor_session_id` run over every string the record holds,
so a writer that forgot to scrub is refused at ``model_validate`` instead
of leaking into a committed artifact. A session that must be cited is
cited by digest.

*Every frozen limit is compared, not edited.* :func:`compare_limit` reads
the :class:`~eawf.kernel.spec.measured_contract.ObservedLimit` rows off the
frozen contract and answers whether the new observation sits inside them.
The frozen record is never rewritten.

*Every breach names its successor.* A comparison that left its limit is
only valid alongside a :class:`LimitBreach` naming the contract id that
will carry the new number. Widening a frozen limit in place is exactly the
failure this record exists to prevent, so the model refuses a record whose
breach has nowhere to go.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.measured_contract import (
    ContractIdStr,
    LimitDirection,
    MeasuredContract,
    MeasurementEnvironment,
    NonBlankStr,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.workflow.evidence.measured_contract import PREFLIGHT_CONTRACTS

logger = logging.getLogger(__name__)

#: Repo-relative directory the re-probe observation files are written to.
REPROBE_DIR: Final[str] = ".ea/artifacts/evidence/2026-09-18-dev3-reprobe"

#: Repo-relative path of the cross-provider conformance re-probe.
CROSS_PROVIDER_REPROBE_PATH: Final[str] = f"{REPROBE_DIR}/cross-provider-reprobe.json"

#: Repo-relative path of the daemon JSON-RPC re-probe.
DAEMON_RPC_REPROBE_PATH: Final[str] = f"{REPROBE_DIR}/daemon-rpc-reprobe.json"

#: Schema tag every re-probe record carries.
REPROBE_SCHEMA_VERSION: Final[str] = "contract-reprobe/v1"

#: The most capability rows a driven re-probe may leave unmeasured. The
#: frozen contract tolerates eighteen because it never drove anything; a
#: re-probe that dispatches a real Run per installed runtime can only fail
#: to reach the rows of a runtime whose binary is absent, and one absent
#: runtime is eight rows.
UNMEASURED_ROW_CEILING: Final[int] = 8

#: An absolute filesystem path: a ``/`` or ``~`` that opens a path segment
#: rather than one that follows a scheme (``driver://x/y``) or another
#: separator. Windows drive roots are caught by :data:`_DRIVE_ROOT_RE`.
_ABSOLUTE_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w:/])[~/][\w.-]+/")

#: A Windows drive root such as ``C:\\Users``.
_DRIVE_ROOT_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]:\\")

#: A raw vendor session identifier. Both installed runtimes hand back a
#: UUID -- claude as ``session_id``, codex as ``thread_id`` -- so the UUID
#: shape is the one rule that covers both.
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def contains_absolute_path(value: str) -> bool:
    """Return whether *value* carries an absolute filesystem path.

    Args:
        value: Any string a record is about to carry.

    Returns:
        ``True`` when the string holds a POSIX absolute path, a
        home-relative ``~/`` path, or a Windows drive root. A URI
        authority such as ``driver://codex/v1`` is not a path and
        answers ``False``.
    """
    return bool(_ABSOLUTE_PATH_RE.search(value) or _DRIVE_ROOT_RE.search(value))


def contains_vendor_session_id(value: str) -> bool:
    """Return whether *value* carries a raw vendor session identifier.

    Args:
        value: Any string a record is about to carry.

    Returns:
        ``True`` when the string holds a UUID-shaped token, which is the
        form both installed runtimes hand back for a session or thread.
    """
    return bool(_SESSION_ID_RE.search(value))


def _offending_strings(payload: Any, *, path: str = "") -> list[str]:
    """Return one message per string in *payload* that must not be recorded.

    Args:
        payload: A dumped record fragment: mapping, sequence or scalar.
        path: Dotted location of *payload* inside the record, used to
            name the field in the message.

    Returns:
        A list of ``"<field>: <reason>"`` messages, empty when nothing in
        the fragment carries a host path or a vendor session id.
    """
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, item in payload.items():
            found.extend(_offending_strings(item, path=f"{path}.{key}" if path else str(key)))
        return found
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(_offending_strings(item, path=f"{path}[{index}]"))
        return found
    if isinstance(payload, str):
        if contains_absolute_path(payload):
            found.append(f"{path or '<root>'}: carries an absolute path")
        if contains_vendor_session_id(payload):
            found.append(f"{path or '<root>'}: carries a raw vendor session id")
    return found


class ProbedStatus(StrEnum):
    """How a re-probed capability row compares with its declared cell."""

    OK = "OK"
    DRIFT = "DRIFT"
    UNKNOWN = "UNKNOWN"


class EvidenceBasis(StrEnum):
    """What a capability row's observation was taken from.

    ``DISPATCHED_RUN`` is the strongest: the row rests on what the child
    process emitted during a Run the dispatcher started. ``DRIVER_SEAM``
    rests on the eawf driver call itself being exercised. ``RUNTIME_SURFACE``
    rests on a separate, zero-token subprocess of the installed CLI, which
    is weaker than a driven turn and is recorded as such rather than
    dressed up as one.
    """

    DISPATCHED_RUN = "dispatched_run"
    DRIVER_SEAM = "driver_seam"
    RUNTIME_SURFACE = "runtime_surface"


#: Digest spelling shared with the evidence store: ``sha256:`` + 64 hex.
DigestStr = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class CapabilityProbe(_StrictModel):
    """One capability row re-measured against one runtime.

    Attributes:
        runtime_id: Canonical runtime identifier the row belongs to.
        capability_id: The capability row's identifier.
        declared: The cell ``capabilities.yaml`` declares for the pair.
        status: How the observation compares with the declaration.
        basis: What the observation was taken from.
        measured: Whether the probe reached the row at all. A row with
            status ``UNKNOWN`` was not reached.
        evidence: Scrubbed prose naming what was observed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: NonBlankStr
    capability_id: NonBlankStr
    declared: Literal["supported", "unsupported", "partial", "unknown"]
    status: ProbedStatus
    basis: EvidenceBasis
    measured: bool
    evidence: Annotated[str, Field(min_length=1, max_length=400)]

    @model_validator(mode="after")
    def _unknown_is_unmeasured(self) -> Self:
        """Keep ``measured`` and ``status`` from disagreeing.

        Returns:
            The unchanged row.

        Raises:
            ValueError: The row claims a measurement it has no status
                for, or claims a status it did not measure.
        """
        if (self.status is ProbedStatus.UNKNOWN) == self.measured:
            raise ValueError("a measured row resolves OK or DRIFT; an UNKNOWN row is unmeasured")
        return self


class DispatchObservation(_StrictModel):
    """What one real native Run produced when the dispatcher drove it.

    Attributes:
        runtime_id: Canonical runtime identifier that served the Run.
        provider_kind: The compiled provider discriminator the launcher
            answered to.
        run_key: The Run record key the dispatch addressed.
        stage: How far the dispatch got.
        handshake_disposition: How the worker's announcement was judged.
        model_requested: The model the compiled spec named.
        output_tokens: Output tokens the turn billed. At least one, so a
            record cannot claim a Run that never generated anything.
        input_tokens: Input tokens the turn billed.
        stream_lines: How many stream lines the driver drained.
        wall_seconds: Wall clock of the spawn.
        provider_session_digest: Digest of the vendor session identifier.
            The identifier itself is never recorded.
        subprocess_observed: Whether the driver saw a child pid.
        launch_adjustment: What the probe had to change for the shipped
            launcher to start this runtime at all; ``None`` when the
            shipped path started it unchanged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: NonBlankStr
    provider_kind: NonBlankStr
    run_key: NonBlankStr
    stage: NonBlankStr
    handshake_disposition: NonBlankStr
    model_requested: NonBlankStr
    output_tokens: Annotated[int, Field(ge=1)]
    input_tokens: Annotated[int, Field(ge=0)]
    stream_lines: Annotated[int, Field(ge=1)]
    wall_seconds: Annotated[float, Field(ge=0.0)]
    provider_session_digest: DigestStr
    subprocess_observed: bool
    launch_adjustment: Annotated[str, Field(min_length=1, max_length=400)] | None = None


class LadderRung(_StrictModel):
    """One concurrency level of the JSON-RPC ladder.

    Attributes:
        method: The RPC method driven at this level.
        concurrency: How many connections called at once.
        calls: How many calls completed.
        errors: How many calls failed or answered an error.
        p50_ms: Median latency in milliseconds.
        p95_ms: 95th-percentile latency in milliseconds.
        max_ms: Worst latency in milliseconds.
        native_runs_in_flight_at_start: Live provider children when the
            rung began.
        native_runs_in_flight_at_end: Live provider children when the
            rung ended.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: NonBlankStr
    concurrency: Annotated[int, Field(ge=1)]
    calls: Annotated[int, Field(ge=1)]
    errors: Annotated[int, Field(ge=0)]
    p50_ms: Annotated[float, Field(ge=0.0)]
    p95_ms: Annotated[float, Field(ge=0.0)]
    max_ms: Annotated[float, Field(ge=0.0)]
    native_runs_in_flight_at_start: Annotated[int, Field(ge=0)]
    native_runs_in_flight_at_end: Annotated[int, Field(ge=0)]


class LimitComparison(_StrictModel):
    """One frozen limit held up against one new observation.

    Attributes:
        limit_name: Name of the frozen limit row.
        direction: Whether the frozen value is a ceiling or a floor.
        frozen_value: The number the frozen contract recorded.
        observed_value: The number this re-probe recorded.
        unit: Unit both numbers are expressed in.
        within_limit: Whether the observation sits inside the limit.
        label: What the observation was, in prose.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_name: NonBlankStr
    direction: LimitDirection
    frozen_value: float
    observed_value: float
    unit: NonBlankStr
    within_limit: bool
    label: NonBlankStr


class LimitBreach(_StrictModel):
    """A comparison that left its limit, and where the new number goes.

    Attributes:
        limit_name: Name of the frozen limit the observation left.
        direction: Whether the frozen value is a ceiling or a floor.
        frozen_value: The number the frozen contract recorded.
        observed_value: The number this re-probe recorded.
        unit: Unit both numbers are expressed in.
        label: What the observation was, in prose.
        successor_contract_id: The contract id that will carry the new
            number. Mandatory: a breach with nowhere to go is an
            invitation to widen the frozen limit in place.
        rationale: Why the successor is the right home for it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit_name: NonBlankStr
    direction: LimitDirection
    frozen_value: float
    observed_value: float
    unit: NonBlankStr
    label: NonBlankStr
    successor_contract_id: ContractIdStr
    rationale: NonBlankStr


class ReprobeRecord(_StrictModel):
    """A measured contract observed a second time, under live behaviour.

    Attributes:
        schema_version: Record schema tag.
        contract_id: The frozen contract this observation re-measures.
        probe_command: The re-runnable command that produced it.
        observed_at: When the re-probe ran.
        dispatches: The real native Runs the observation rests on.
        comparisons: Every frozen limit held up against a new number.
        breaches: One row per comparison that left its limit.
        boundary: Where this re-measurement stops being valid.
        environment: The population and host it was taken on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["contract-reprobe/v1"]
    contract_id: ContractIdStr
    probe_command: NonBlankStr
    observed_at: UtcDatetime
    dispatches: Annotated[tuple[DispatchObservation, ...], Field(min_length=1)]
    comparisons: Annotated[tuple[LimitComparison, ...], Field(min_length=1)]
    breaches: tuple[LimitBreach, ...] = ()
    boundary: NonBlankStr
    environment: MeasurementEnvironment

    @model_validator(mode="after")
    def _every_breach_names_a_successor(self) -> Self:
        """Pair each out-of-limit comparison with a successor contract.

        Returns:
            The unchanged record.

        Raises:
            ValueError: A comparison left its limit with no breach row
                naming a successor, or a breach row names a comparison
                that stayed inside its limit.
        """
        breached = {(row.limit_name, row.label) for row in self.comparisons if not row.within_limit}
        declared = {(row.limit_name, row.label) for row in self.breaches}
        if missing := sorted(breached - declared):
            raise ValueError(
                "a breached limit needs a successor contract id, not a widened limit: "
                f"{', '.join(f'{name} ({label})' for name, label in missing)}"
            )
        if stray := sorted(declared - breached):
            raise ValueError(
                "a breach row must name a comparison that left its limit: "
                f"{', '.join(f'{name} ({label})' for name, label in stray)}"
            )
        return self

    @model_validator(mode="after")
    def _no_host_paths_or_session_ids(self) -> Self:
        """Refuse a record carrying a host path or a vendor session id.

        Returns:
            The unchanged record.

        Raises:
            ValueError: Any string in the record holds an absolute path
                or a UUID-shaped vendor session identifier. Scrubbing
                belongs at the writer, so the reader refuses rather than
                repairs.
        """
        if offences := _offending_strings(self.model_dump(mode="json")):
            raise ValueError("re-probe record must be scrubbed: " + "; ".join(offences))
        return self


class CrossProviderReprobe(ReprobeRecord):
    """Cross-provider conformance re-measured by dispatching real Runs.

    Attributes:
        capability_rows: One row per declared runtime and capability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_rows: Annotated[tuple[CapabilityProbe, ...], Field(min_length=1)]

    @property
    def unmeasured_rows(self) -> int:
        """Return how many capability rows the probe never reached."""
        return sum(1 for row in self.capability_rows if not row.measured)

    @property
    def drift_rows(self) -> int:
        """Return how many capability rows disagree with their cell."""
        return sum(1 for row in self.capability_rows if row.status is ProbedStatus.DRIFT)


class DaemonRpcReprobe(ReprobeRecord):
    """Daemon JSON-RPC re-measured while native Runs were in flight.

    Attributes:
        ladder_rungs: One row per concurrency level driven.
        rpc_errors: How many calls failed across the whole ladder.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ladder_rungs: Annotated[tuple[LadderRung, ...], Field(min_length=1)]
    rpc_errors: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _error_count_matches_the_ladder(self) -> Self:
        """Keep the headline error count honest against the rungs.

        Returns:
            The unchanged record.

        Raises:
            ValueError: The declared total disagrees with the rungs.
        """
        total = sum(rung.errors for rung in self.ladder_rungs)
        if total != self.rpc_errors:
            raise ValueError(f"rpc_errors={self.rpc_errors} but the rungs hold {total}")
        return self

    @property
    def concurrency_reached(self) -> int:
        """Return the highest concurrency level the ladder drove."""
        return max(rung.concurrency for rung in self.ladder_rungs)


def frozen_contract(contract_id: str) -> MeasuredContract:
    """Return the frozen contract a re-probe is measured against.

    Args:
        contract_id: Id of the contract being re-probed.

    Returns:
        The frozen :class:`MeasuredContract`.

    Raises:
        KeyError: No contract is registered under that id.
    """
    try:
        return PREFLIGHT_CONTRACTS[contract_id]
    except KeyError as exc:
        raise KeyError(f"no measured contract is registered as {contract_id!r}") from exc


def compare_limit(
    contract: MeasuredContract, limit_name: str, observed: float, label: str
) -> LimitComparison:
    """Hold one frozen limit of *contract* up against a new observation.

    Args:
        contract: The frozen contract carrying the limit.
        limit_name: Name of the limit row to compare against.
        observed: The number this re-probe measured.
        label: What the observation was, in prose.

    Returns:
        The comparison, carrying whether the observation stayed inside
        the frozen limit.

    Raises:
        KeyError: The contract declares no limit under that name.
    """
    for limit in contract.limits:
        if limit.name != limit_name:
            continue
        within = (
            observed <= limit.value if limit.direction == "ceiling" else observed >= limit.value
        )
        return LimitComparison(
            limit_name=limit.name,
            direction=limit.direction,
            frozen_value=limit.value,
            observed_value=observed,
            unit=limit.unit,
            within_limit=within,
            label=label,
        )
    raise KeyError(f"{contract.contract_id} declares no limit named {limit_name!r}")


def _read_document(path: Path) -> Any:
    """Return the JSON document at *path*.

    Args:
        path: Filesystem path of the observation file.

    Returns:
        The parsed document.

    Raises:
        FileNotFoundError: Nothing is recorded at that path.
        ValueError: The file is not valid JSON.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"no re-probe record is recorded at {path.name}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc.msg}") from exc


def load_cross_provider_reprobe(path: Path) -> CrossProviderReprobe:
    """Return the validated cross-provider re-probe recorded at *path*.

    Args:
        path: Filesystem path of the observation file.

    Returns:
        The validated record.

    Raises:
        FileNotFoundError: Nothing is recorded at that path.
        ValueError: The document is not valid JSON.
        ValidationError: The document does not satisfy the record.
    """
    return CrossProviderReprobe.model_validate(_read_document(path))


def load_daemon_rpc_reprobe(path: Path) -> DaemonRpcReprobe:
    """Return the validated daemon-RPC re-probe recorded at *path*.

    Args:
        path: Filesystem path of the observation file.

    Returns:
        The validated record.

    Raises:
        FileNotFoundError: Nothing is recorded at that path.
        ValueError: The document is not valid JSON.
        ValidationError: The document does not satisfy the record.
    """
    return DaemonRpcReprobe.model_validate(_read_document(path))


def breach_rows(
    comparisons: Iterable[LimitComparison], *, successor_contract_id: str, rationale: str
) -> tuple[LimitBreach, ...]:
    """Return one breach row per comparison that left its frozen limit.

    Args:
        comparisons: The comparisons a re-probe produced.
        successor_contract_id: The contract id that will carry every new
            number this re-probe measured outside a frozen limit.
        rationale: Why the successor is the right home for them.

    Returns:
        The breach rows, empty when every comparison stayed inside.
    """
    return tuple(
        LimitBreach(
            limit_name=comparison.limit_name,
            direction=comparison.direction,
            frozen_value=comparison.frozen_value,
            observed_value=comparison.observed_value,
            unit=comparison.unit,
            label=comparison.label,
            successor_contract_id=successor_contract_id,
            rationale=rationale,
        )
        for comparison in comparisons
        if not comparison.within_limit
    )
