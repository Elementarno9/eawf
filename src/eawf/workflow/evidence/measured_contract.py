"""Promote a :class:`MeasuredContract` onto the evidence path.

The 2026-08-13 preflight spikes measured three surfaces and left their
output under ``.ea/local/spikes/`` — a gitignored scratch tree. A number
that lives only there is unciteable: it has no URN, no evidence row, and
no way for a checkpoint to prove it read the measurement rather than
guessed it. This module closes that gap.

:data:`PREFLIGHT_CONTRACTS` holds the three contracts extracted from
those probe runs. :func:`promote_measured_contract` walks one through the
existing evidence path — :func:`eawf.workflow.evidence.artifact.add_artifact`
for the state-resident artifact row, and a
:class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` envelope for the
append-only evidence store — so the contract becomes addressable as
``urn:eawf:v1:artifact:<scope>/MCT-########``.

:func:`resolve_contract_citation` is the read side. It accepts an
artifact id or a full artifact URN, and it refuses a citation that points
back into ``.ea/local/spikes/``: that path means the contract was never
promoted, so the error names the promotion command instead of the generic
not-found. That refusal is the ``plan_reference_missing`` cause tag.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from eawf.kernel.spec.measured_contract import (
    MeasuredContract,
    MeasurementEnvironment,
    ObservedLimit,
    ScaleBand,
    scale_band_satisfies,
)
from eawf.kernel.state import urn as urn_mod
from eawf.kernel.state.enums import ArtifactKind, StoreKind
from eawf.kernel.state.models import Artifact, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord, mint_evidence_id
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence import _io
from eawf.workflow.evidence.artifact import add_artifact, show_artifact

logger = logging.getLogger(__name__)

#: Repo-relative root of the gitignored spike scratch tree. A contract
#: whose only address is a path under here was never promoted.
LOCAL_SPIKE_ROOT: Final[str] = ".ea/local/spikes/"

#: URN prefix every promoted contract resolves under.
ARTIFACT_URN_PREFIX: Final[str] = "urn:eawf:v1:artifact:"

#: Artifact kind a promoted measured contract registers as. A measured
#: contract is the spec a plan is licensed to assert against, so it files
#: alongside plan specs rather than as a research brief.
CONTRACT_ARTIFACT_KIND: Final[str] = ArtifactKind.PLAN_SPEC.value

#: Repo-relative body every promoted contract points at: this module IS
#: the durable, typed carrier of the extracted observations, so the
#: artifact ``uri`` addresses it rather than a rendered markdown copy that
#: would immediately drift from the typed rows.
CONTRACT_BODY_URI: Final[str] = "repo:src/eawf/workflow/evidence/measured_contract.py"

_PREFLIGHT_DATE: Final[datetime] = datetime(2026, 8, 13, tzinfo=UTC)


_EPOCH1_STATE_AT_SCALE = MeasuredContract(
    contract_id="MCT-26081301",
    surface="epoch-1 state.json population that the epoch-2 importer must consume",
    probe_command=(
        "uv run python .ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/probe.py"
    ),
    observed={
        "state_bytes": 5743039,
        "schema_version": "1.19",
        "collection_count": 15,
        "objects_total": 10684,
        "wave_rows": 1284,
        "actual_rows": 849,
        "duplicate_keys_in_source": 0,
        "key_order_is_sorted_everywhere": True,
        "floats_not_round_tripping": 0,
        "non_ascii_strings": 1243,
        "embedded_newlines": 0,
        "dangling_refs_across_five_edges": 0,
        "byte_identical_dump_variants": 2,
    },
    limits=(
        ObservedLimit(
            name="state_bytes",
            value=5743039.0,
            unit="bytes",
            direction="ceiling",
            basis="P5 source_bytes of .ea/state.json at schema_version 1.19",
        ),
        ObservedLimit(
            name="largest_collection_share",
            value=60.096,
            unit="percent",
            direction="ceiling",
            basis="P6 per-collection byte census; waves is the dominant collection",
        ),
        ObservedLimit(
            name="max_row_bytes",
            value=25794.0,
            unit="bytes",
            direction="ceiling",
            basis="P6 largest single row, waves/P30-I25-W25",
        ),
        ObservedLimit(
            name="float_roundtrip_failures",
            value=0.0,
            unit="count",
            direction="ceiling",
            basis="P5 numbers census over 5847 float values",
        ),
        ObservedLimit(
            name="non_ascii_strings_to_handle",
            value=1243.0,
            unit="count",
            direction="floor",
            basis="P5 non_ascii_strings census; an importer must survive at least this many",
        ),
    ),
    boundary=(
        "Byte-identity holds only under json.dumps(indent=2, ensure_ascii=False) plus a "
        "trailing newline; the other five dump settings probed all diverge, so a naive "
        "re-dump is not byte-identical. Measured on ONE epoch-1 population at "
        "schema_version 1.19 whose key order was already sorted everywhere and which "
        "carried zero duplicate keys and zero embedded newlines. A state exhibiting any "
        "of those three is outside the measured envelope, as is any schema_version "
        "beyond 1.19."
    ),
    observed_at=_PREFLIGHT_DATE,
    observed_at_ref=(
        ".ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/observations.json"
    ),
    environment=MeasurementEnvironment(
        scale_band=ScaleBand.PRODUCTION,
        population=(
            "the live epoch-1 .ea/state.json: 5,743,039 bytes across 15 collections and "
            "10,684 JSON objects, including 1,284 wave rows and 849 actual rows"
        ),
        population_size=10684,
        host_platform="darwin",
        toolchain="python 3.14.3",
    ),
)


_CROSS_PROVIDER_CONFORMANCE = MeasuredContract(
    contract_id="MCT-26081302",
    surface="installed provider runtime CLI capability surface (claude, codex, opencode)",
    probe_command=(
        "uv run python .ea/local/spikes/2026-08-13-v07-preflight/"
        "cross-provider-conformance/probe.py"
    ),
    observed={
        "runtimes_declared": 3,
        "runtimes_installed": 2,
        "claude_version": "2.1.231 (Claude Code)",
        "codex_version": "codex-cli 0.146.0",
        "opencode_installed": False,
        "capability_rows_per_runtime": 8,
        "drift_rows": 2,
        "unmeasured_rows": 8,
    },
    limits=(
        ObservedLimit(
            name="runtimes_installed",
            value=2.0,
            unit="count",
            direction="floor",
            basis="drift.json installed flag across the three declared runtimes",
        ),
        ObservedLimit(
            name="declared_vs_observed_drift",
            value=2.0,
            unit="count",
            direction="ceiling",
            basis="drift.json DRIFT rows: session_resume on claude-code and on codex",
        ),
        ObservedLimit(
            name="unmeasured_capability_rows",
            value=8.0,
            unit="count",
            direction="ceiling",
            basis="drift.json MISSING rows: all eight opencode rows, binary absent from PATH",
        ),
    ),
    boundary=(
        "Measured from --version, --help and config-inspection paths only, with zero "
        "billed generation, so every finding is about what the CLI ADVERTISES rather "
        "than what it does when driven. Capability rows carrying no probe rule (skills, "
        "plan_mode, sub_agents, cache_control, error_class_surface) are declared-only and "
        "were not measured at all. opencode was absent from PATH, so all eight of its "
        "rows are unmeasured rather than passing. Versions are a point-in-time reading of "
        "one workstation's installs."
    ),
    observed_at=datetime(2026, 8, 13, 15, 35, 41, tzinfo=UTC),
    observed_at_ref=(
        ".ea/local/spikes/2026-08-13-v07-preflight/cross-provider-conformance/drift.json"
    ),
    environment=MeasurementEnvironment(
        scale_band=ScaleBand.DEV,
        population=(
            "3 declared runtimes x 8 capability rows = 24 conformance rows; 16 probed "
            "against two installed binaries and 8 unmeasured"
        ),
        population_size=24,
        host_platform="darwin",
        toolchain="python 3.14.3",
    ),
)


_DAEMON_RPC_PARALLEL = MeasuredContract(
    contract_id="MCT-26081303",
    surface="eawf daemon JSON-RPC under concurrent readers and the portalocker write fallback",
    probe_command=(
        "uv run python .ea/local/spikes/2026-08-13-v07-preflight/daemon-rpc-parallel/probe.py"
    ),
    observed={
        "ping_p95_ms_at_c1": 0.22,
        "ping_p95_ms_at_c64": 5.32,
        "ping_errors_through_c64": 0,
        "state_read_p95_ms_at_c1": 84.41,
        "state_read_p95_ms_at_c4": 507.25,
        "portalock_wait_p95_ms_at_c8": 3692.97,
        "portalock_errors_at_c12": 2,
        "portalock_errors_at_c16": 6,
        "pings_during_1s_sync_hold": 1,
        "pings_during_1s_async_hold": 134,
        "rpc_call_timeout_s": 30.0,
        "portalock_default_timeout_s": 5.0,
    },
    limits=(
        ObservedLimit(
            name="ping_concurrency",
            value=64.0,
            unit="count",
            direction="ceiling",
            basis="read_ladder daemon.ping at c64: 1280 calls, 0 errors, p95 5.32 ms",
        ),
        ObservedLimit(
            name="state_read_concurrency",
            value=4.0,
            unit="count",
            direction="ceiling",
            basis="read_ladder state.read p95 507.25 ms at c4 versus 84.41 ms at c1, a 6x knee",
        ),
        ObservedLimit(
            name="portalock_writer_concurrency",
            value=8.0,
            unit="count",
            direction="ceiling",
            basis="portalock ladder: 0 errors through c8, then 2/12 at c12 and 6/16 at c16",
        ),
        ObservedLimit(
            name="ping_p95_ms",
            value=5.32,
            unit="ms",
            direction="ceiling",
            basis="worst read_ladder daemon.ping p95, measured at c64",
        ),
    ),
    boundary=(
        "Write concurrency was measured only in an isolated scratch workspace with its "
        "own socket path and its own daemon; the live daemon's write path was never "
        "contended, so the portalock ceiling is a scratch-workspace number. Head-of-line "
        "blocking was measured against a synthetic SYNCHRONOUS handler (1 ping completed "
        "during a 1 s sync hold versus 134 during an async hold of the same length) - a "
        "mixed sync/async handler set is outside the envelope. Every latency figure comes "
        "from one workstation against one 5.7 MB state file."
    ),
    observed_at=_PREFLIGHT_DATE,
    observed_at_ref=(
        ".ea/local/spikes/2026-08-13-v07-preflight/daemon-rpc-parallel/observations.jsonl"
    ),
    environment=MeasurementEnvironment(
        scale_band=ScaleBand.DEV,
        population=(
            "2,540 read RPCs across concurrency 1 to 64, plus 40 portalocker writers "
            "across concurrency 2 to 16, against one live daemon and one isolated "
            "scratch daemon"
        ),
        population_size=2540,
        host_platform="darwin",
        toolchain="python 3.14.3",
    ),
)


#: The three contracts extracted from the 2026-08-13 preflight spikes,
#: keyed by contract id.
PREFLIGHT_CONTRACTS: Final[Mapping[str, MeasuredContract]] = {
    _EPOCH1_STATE_AT_SCALE.contract_id: _EPOCH1_STATE_AT_SCALE,
    _CROSS_PROVIDER_CONFORMANCE.contract_id: _CROSS_PROVIDER_CONFORMANCE,
    _DAEMON_RPC_PARALLEL.contract_id: _DAEMON_RPC_PARALLEL,
}

#: Scale band the implementing checkpoint asserts over, per contract. The
#: importer checkpoint runs against the real state population, so it
#: demands a production-band measurement; the conformance and daemon
#: checkpoints assert over a developer workstation and demand dev band.
#: :func:`promote_measured_contract` refuses a contract measured below the
#: band its checkpoint needs.
PREFLIGHT_CHECKPOINT_BANDS: Final[Mapping[str, ScaleBand]] = {
    "MCT-26081301": ScaleBand.PRODUCTION,
    "MCT-26081302": ScaleBand.DEV,
    "MCT-26081303": ScaleBand.DEV,
}


@dataclass(frozen=True)
class ContractPromotion:
    """Result of promoting one measured contract.

    Attributes:
        contract: The contract that was promoted.
        artifact_id: State-resident artifact id (equals the contract id).
        urn: Canonical ``urn:eawf:v1:artifact:<scope>/<id>`` address.
        evidence: The minted evidence (EVD) record.
        artifact_event: Envelope for the ``artifact.add`` event stream.
        evidence_envelope: Envelope carrying *evidence* for the
            ``StoreKind.EVIDENCE`` store.
    """

    contract: MeasuredContract
    artifact_id: str
    urn: str
    evidence: EvidenceRecord
    artifact_event: Envelope
    evidence_envelope: Envelope


def _promotion_command(contract_id: str) -> str:
    """Return the CLI command that promotes *contract_id*.

    Args:
        contract_id: Measured-contract id to promote.

    Returns:
        The exact command an operator runs to promote the contract.
    """
    return f"eawf artifact promote-contract {contract_id}"


def _normalise_citation(citation: str) -> str:
    """Strip the ``repo:`` scheme and any leading ``./`` segments.

    Uses an explicit prefix loop rather than ``lstrip("./")``: the
    character-set form would also eat the leading dot of ``.ea/``,
    silently turning a spike path into ``ea/local/spikes/...`` that no
    longer matches :data:`LOCAL_SPIKE_ROOT`.

    Args:
        citation: Raw citation string.

    Returns:
        The repo-relative path form of *citation*.
    """
    stripped = citation.removeprefix("repo:")
    while stripped.startswith("./"):
        stripped = stripped[2:]
    return stripped


def _is_local_spike_citation(citation: str) -> bool:
    """Return whether *citation* addresses the gitignored spike tree.

    Accepts both a bare repo-relative path and the ``repo:``-prefixed
    artifact-uri form, since a citation reaches this check from both a
    spec body and an artifact row.

    Args:
        citation: Candidate reference string.

    Returns:
        ``True`` when the citation points under
        :data:`LOCAL_SPIKE_ROOT`.
    """
    return _normalise_citation(citation).startswith(LOCAL_SPIKE_ROOT)


def _contract_id_for_observation_ref(citation: str) -> str | None:
    """Return the contract id whose raw observations *citation* points at.

    Lets the ``plan_reference_missing`` error name the exact promotion
    command for a known spike output rather than a generic placeholder.

    Args:
        citation: A repo-relative (or ``repo:``-prefixed) path.

    Returns:
        The matching contract id, or ``None`` when no registered contract
        was extracted from that path's spike directory.
    """
    stripped = _normalise_citation(citation)
    for contract_id, contract in PREFLIGHT_CONTRACTS.items():
        spike_dir = contract.observed_at_ref.rsplit("/", 1)[0]
        if stripped == contract.observed_at_ref or stripped.startswith(f"{spike_dir}/"):
            return contract_id
    return None


def _contract_metadata(contract: MeasuredContract) -> dict[str, object]:
    """Render *contract* into the artifact ``metadata`` map.

    The promoted artifact row carries the whole contract so a consumer
    that resolved the URN can read the boundary and the measurement
    environment straight off ``state.json`` without a second lookup into
    the evidence store.

    Args:
        contract: Contract being promoted.

    Returns:
        A JSON-safe mapping suitable for ``Artifact.metadata``.
    """
    return {
        "contract_id": contract.contract_id,
        "surface": contract.surface,
        "probe_command": contract.probe_command,
        "observed": dict(contract.observed),
        "limits": [row.model_dump(mode="json") for row in contract.limits],
        "boundary": contract.boundary,
        "observed_at": contract.observed_at.isoformat(),
        "observed_at_ref": contract.observed_at_ref,
        "environment": contract.environment.model_dump(mode="json"),
    }


def promote_measured_contract(
    state: State,
    *,
    contract: MeasuredContract,
    scope_id: str,
    required_band: ScaleBand,
) -> ContractPromotion:
    """Promote *contract* onto the evidence path, mutating *state* in place.

    Registers the contract as an artifact via
    :func:`eawf.workflow.evidence.artifact.add_artifact` — the existing
    evidence path, so the contract inherits URN minting, uri validation
    and the duplicate-id guard — and mints one evidence (EVD) record
    describing the promotion. The caller appends both returned envelopes
    to their stores inside the same transaction.

    Args:
        state: Mutable state under transaction.
        contract: Contract to promote.
        scope_id: Owning scope (normally the project code) used to build
            the artifact URN.
        required_band: Scale band the implementing checkpoint asserts
            over. The contract's measured band must be at least this.

    Returns:
        A :class:`ContractPromotion` carrying the URN and both envelopes.

    Raises:
        UserError: When the contract's measured
            :attr:`~eawf.kernel.spec.measured_contract.MeasurementEnvironment.scale_band`
            is below *required_band* (``kind="scale_band_below_checkpoint"``),
            or when the contract id is already registered
            (``kind="InvalidInput"``, raised by the underlying artifact
            mutator).
    """
    observed_band = contract.environment.scale_band
    if not scale_band_satisfies(observed_band, required=required_band):
        logger.warning(
            f"promote_measured_contract reject contract_id={contract.contract_id!r} "
            f"observed_band={observed_band.value!r} required_band={required_band.value!r}"
        )
        raise UserError(
            f"contract {contract.contract_id} was measured at scale band "
            f"{observed_band.value!r} but its checkpoint asserts over "
            f"{required_band.value!r}; re-measure at the larger band before promoting",
            kind="scale_band_below_checkpoint",
        )

    artifact_event = add_artifact(
        state,
        artifact_id=contract.contract_id,
        kind=CONTRACT_ARTIFACT_KIND,
        uri=CONTRACT_BODY_URI,
        scope_id=scope_id,
        metadata=_contract_metadata(contract),
    )
    artifact_urn = _io.artifact_urn(scope_id, contract.contract_id)

    evidence = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status="pass",
        summary=(
            f"measured contract {contract.contract_id} promoted for {contract.surface} "
            f"at scale band {observed_band.value}"
        )[:500],
        refs=[contract.contract_id, artifact_urn],
        metrics={
            "contract_id": contract.contract_id,
            "scale_band": observed_band.value,
            "required_band": required_band.value,
            "population_size": contract.environment.population_size,
            "limit_count": len(contract.limits),
        },
        created_at=datetime.now(UTC),
    )
    evidence_envelope = _io.kind_envelope(
        record_id=evidence.id,
        kind=StoreKind.EVIDENCE,
        scope_id=scope_id,
        summary=evidence.summary,
        payload=evidence.model_dump(mode="json"),
        artifact_ids=[contract.contract_id],
    )
    logger.info(
        f"promote_measured_contract contract_id={contract.contract_id!r} "
        f"urn={artifact_urn!r} evidence_id={evidence.id!r}"
    )
    return ContractPromotion(
        contract=contract,
        artifact_id=contract.contract_id,
        urn=artifact_urn,
        evidence=evidence,
        artifact_event=artifact_event,
        evidence_envelope=evidence_envelope,
    )


def resolve_contract_citation(state: State, citation: str) -> Artifact:
    """Resolve *citation* to a promoted artifact row.

    Accepts an artifact id or a full ``urn:eawf:v1:artifact:<scope>/<id>``
    URN. A citation that addresses the gitignored spike tree is refused
    outright: that path means the contract was never promoted, so the
    error names the promotion command rather than reporting a bare
    not-found the operator cannot act on.

    Args:
        state: State to resolve against.
        citation: Artifact id, artifact URN, or a raw spike path.

    Returns:
        The registered :class:`~eawf.kernel.state.models.Artifact`.

    Raises:
        UserError: ``kind="plan_reference_missing"`` when *citation*
            resolves only under :data:`LOCAL_SPIKE_ROOT`;
            ``kind="InvalidInput"`` when it is a malformed or
            non-artifact URN; ``kind="NotFound"`` when the id is not
            registered.
    """
    if _is_local_spike_citation(citation):
        contract_id = _contract_id_for_observation_ref(citation)
        command = _promotion_command(contract_id) if contract_id else _promotion_command("<id>")
        logger.warning(f"resolve_contract_citation unpromoted citation={citation!r}")
        raise UserError(
            f"citation {citation!r} resolves only under {LOCAL_SPIKE_ROOT} and was never "
            f"promoted, so it has no artifact URN to cite; promote it first: {command}",
            kind="plan_reference_missing",
        )
    if citation.startswith(ARTIFACT_URN_PREFIX):
        try:
            parsed = urn_mod.parse(citation)
        except ValueError as exc:
            raise UserError(f"malformed artifact URN: {citation!r}", kind="InvalidInput") from exc
        if parsed.kind != "artifact" or not parsed.id:
            raise UserError(f"not an artifact URN with an id: {citation!r}", kind="InvalidInput")
        return show_artifact(state, parsed.id)
    return show_artifact(state, citation)


__all__ = [
    "ARTIFACT_URN_PREFIX",
    "CONTRACT_ARTIFACT_KIND",
    "CONTRACT_BODY_URI",
    "LOCAL_SPIKE_ROOT",
    "PREFLIGHT_CHECKPOINT_BANDS",
    "PREFLIGHT_CONTRACTS",
    "ContractPromotion",
    "promote_measured_contract",
    "resolve_contract_citation",
]
