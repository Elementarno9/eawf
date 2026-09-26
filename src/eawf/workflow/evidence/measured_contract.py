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

:func:`refresh_contract_metadata` is the follow-up write. A promoted row
freezes the contract as it read on promotion day, so correcting the typed
contract afterwards leaves the registered row serving a stale shape; the
refresh re-renders the in-code contract over that row's ``metadata``
without disturbing its identity.

:func:`resolve_contract_citation` is the read side. It accepts an
artifact id or a full artifact URN, and it refuses a citation that points
back into ``.ea/local/spikes/``: that path means the contract was never
promoted, so the error names the promotion command instead of the generic
not-found. That refusal is the ``plan_reference_missing`` cause tag. It
also refuses a citation whose contract was measured in a different
repository, the ``contract_environment_incompatible`` cause tag.

:func:`submit_evidence` generalises promotion beyond the four preflight
contracts above: a verified :class:`~eawf.kernel.spec.measured_contract.SpikeReport`
submitted here has each of its contracts promoted through
:func:`promote_measured_contract` -- an unverified report, or a contract
measured in a repository other than the submitting one, is refused before
any of it is written.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from eawf.kernel.runtime.compiled import canonical_digest
from eawf.kernel.spec.measured_contract import (
    MeasuredContract,
    MeasurementEnvironment,
    ObservedLimit,
    ScaleBand,
    SpikeReport,
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
        "capability_rows_total": 24,
        "evidence_backed_rows": 6,
        "drift_rows": 2,
        "declared_only_rows": 10,
        "uninstalled_rows": 8,
        "unmeasured_rows": 18,
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
            name="evidence_backed_capability_rows",
            value=6.0,
            unit="count",
            direction="floor",
            basis=(
                "drift.json rows resolved through a probe rule: session_resume, tool_use "
                "and streaming on each of the two installed runtimes"
            ),
        ),
        ObservedLimit(
            name="unmeasured_capability_rows",
            value=18.0,
            unit="count",
            direction="ceiling",
            basis=(
                "drift.json rows carrying no probe evidence: eight opencode rows whose "
                "binary was absent, plus ten declared-only rows across the two installed "
                "runtimes that no evidence rule covers"
            ),
        ),
    ),
    boundary=(
        "Measured from --version, --help and config-inspection paths only, with zero "
        "billed generation, so every finding is about what the CLI ADVERTISES rather "
        "than what it does when driven. Only six of the twenty-four rows rest on probe "
        "evidence. Ten rows carry no probe rule (skills, plan_mode, sub_agents, "
        "cache_control and error_class_surface on each installed runtime); they are "
        "declared-only and resolve UNKNOWN rather than passing, because the declaration "
        "is not its own evidence. opencode was absent from PATH, so all eight of its "
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
            "3 declared runtimes x 8 capability rows = 24 conformance rows; 6 resolved "
            "through a probe rule, 10 declared-only against two installed binaries, and "
            "8 unmeasured because the binary was absent"
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


_IMPORTER_AT_PRODUCTION_CORPUS = MeasuredContract(
    contract_id="MCT-26091101",
    surface=(
        "the epoch-2 importer run end to end over this project's own live epoch-1 corpus, "
        "at the scale the cutover actually has to survive"
    ),
    probe_command="uv run pytest tests/integration/kernel/migration/test_v07_rehearsal.py -q",
    observed={
        "corpus_magnitude": "thousands",
        "corpus_rows": 4638,
        "generation_multiplier": 1.319,
        "apply_wall_clock_s": 3.849,
        "residual_document_bytes": 396720,
        "rehearsed_corpora": 10,
        "apply_journal_stages": 12,
        "rollback_boundaries": 11,
        "scrub_findings": 0,
        "observed_at_revision": "4a0659cbba0e",  # pragma: allowlist secret
    },
    limits=(
        ObservedLimit(
            name="corpus_rows",
            value=4638.0,
            unit="count",
            direction="floor",
            basis=(
                "row census of the live epoch-1 corpus staged at cutover; the thousands "
                "band holds from this count up to the next order of magnitude"
            ),
        ),
        ObservedLimit(
            name="generation_multiplier",
            value=1.6,
            unit="ratio",
            direction="ceiling",
            basis=(
                "published epoch-2 generation over staged epoch-1 corpus bytes; "
                "observed 1.319 against a declared ceiling of 1.60"
            ),
        ),
        ObservedLimit(
            name="apply_wall_clock_s",
            value=30.0,
            unit="s",
            direction="ceiling",
            basis="one apply over the live corpus; observed 3.849 s against a 30 s budget",
        ),
        ObservedLimit(
            name="residual_document_bytes",
            value=1600000.0,
            unit="bytes",
            direction="ceiling",
            basis=(
                "the generation's own document after the terminal records move to their "
                "ledgers; observed 396,720 B against a 1.6 MB bound"
            ),
        ),
    ),
    boundary=(
        "Measured over the production corpus -- this project's own live epoch-1 .ea tree "
        "at cutover, 4,638 rows in the thousands band -- plus nine committed corpora that "
        "cover the structural shapes the live one does not exhibit. Ten corpora is the "
        "whole measured population: a corpus shape outside that set is unmeasured, not "
        "passing. The four legs are dry run, apply, idempotent rerun and rollback over a "
        "tree that declared itself disposable, so nothing here characterises an apply "
        "against a tree in use, a concurrent apply, or a corpus in the tens-of-thousands "
        "band. Two of the ten corpora are refusals rather than imports, and what is "
        "measured about them is that the refusal repeats identically and leaves the "
        "target byte-identical -- not that the importer handles their content."
    ),
    observed_at=datetime(2026, 9, 9, tzinfo=UTC),
    observed_at_ref="tests/fixtures/migration/live-cutover/corpus-pin.json",
    environment=MeasurementEnvironment(
        scale_band=ScaleBand.PRODUCTION,
        population=(
            "the live epoch-1 corpus at cutover: 4,638 rows, a thousands corpus magnitude, "
            "rehearsed alongside nine committed corpora covering the empty, terminal, "
            "multi-root, interrupted, attention-bearing, historical and refused shapes"
        ),
        population_size=4638,
        host_platform="darwin",
        toolchain="python 3.14.3",
    ),
)


#: The measured contracts a checkpoint may cite, keyed by contract id.
#: Three come from the 2026-08-13 preflight spikes; the fourth is the
#: importer contract re-measured against the production corpus once the
#: rehearsal existed to measure it with.
PREFLIGHT_CONTRACTS: Final[Mapping[str, MeasuredContract]] = {
    _EPOCH1_STATE_AT_SCALE.contract_id: _EPOCH1_STATE_AT_SCALE,
    _CROSS_PROVIDER_CONFORMANCE.contract_id: _CROSS_PROVIDER_CONFORMANCE,
    _DAEMON_RPC_PARALLEL.contract_id: _DAEMON_RPC_PARALLEL,
    _IMPORTER_AT_PRODUCTION_CORPUS.contract_id: _IMPORTER_AT_PRODUCTION_CORPUS,
}

#: The order-of-magnitude bucket the importer corpus falls in, read off
#: the same committed pin the rehearsal is judged against. Distinct from
#: :class:`~eawf.kernel.spec.measured_contract.ScaleBand`, which classes
#: the *environment* a measurement ran in rather than the corpus size.
IMPORTER_CORPUS_MAGNITUDE: Final[str] = "thousands"

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
    "MCT-26091101": ScaleBand.PRODUCTION,
}


@dataclass(frozen=True)
class ContractPromotion:
    """Result of promoting one measured contract.

    Attributes:
        contract: The contract that was promoted.
        artifact_id: State-resident artifact id (equals the contract id).
        urn: Canonical ``urn:eawf:v1:artifact:<scope>/<id>`` address.
        content_digest: The ``sha256:`` digest of the recorded metadata --
            the artifact revision this promotion minted is addressed by
            this digest as well as by ``artifact_id``, so a re-promotion
            of byte-identical content is detectable as such.
        evidence: The minted evidence (EVD) record.
        artifact_event: Envelope for the ``artifact.add`` event stream.
        evidence_envelope: Envelope carrying *evidence* for the
            ``StoreKind.EVIDENCE`` store.
    """

    contract: MeasuredContract
    artifact_id: str
    urn: str
    content_digest: str
    evidence: EvidenceRecord
    artifact_event: Envelope
    evidence_envelope: Envelope


@dataclass(frozen=True)
class ContractMetadataRefresh:
    """Result of refreshing one registered contract row's metadata.

    Attributes:
        contract: The in-code contract the row was rewritten from.
        artifact_id: State-resident artifact id (equals the contract id).
        urn: Canonical ``urn:eawf:v1:artifact:<scope>/<id>`` address of
            the row that was rewritten.
        changed_keys: Metadata keys whose value differed before the
            rewrite, sorted. Empty when the row already agreed with the
            in-code contract, which makes the refresh a no-op an operator
            can tell apart from a real migration.
        artifact_event: Envelope for the ``artifact.update`` event stream.
    """

    contract: MeasuredContract
    artifact_id: str
    urn: str
    changed_keys: tuple[str, ...]
    artifact_event: Envelope


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


def _content_digest(metadata: Mapping[str, object]) -> str:
    """Return the ``sha256:`` digest of *metadata* in canonical form.

    Args:
        metadata: The rendered ``Artifact.metadata`` mapping of a contract
            (see :func:`_contract_metadata`).

    Returns:
        ``sha256:`` followed by 64 lowercase hex characters. A
        byte-identical contract always digests identically, so a
        re-submission of the same measurement is detectable as such
        rather than minting a second, indistinguishable revision.
    """
    return canonical_digest(dict(metadata))


def _require_compatible_environment(contract: MeasuredContract, *, repository: str) -> None:
    """Refuse *contract* when its environment names a different repository.

    A contract whose :attr:`~MeasurementEnvironment.repository` is unset
    carries no repository restriction (every contract promoted before the
    field existed, and any contract meant to be portable), so only a
    contract that names a *different* repository is refused.

    Args:
        contract: Contract being submitted or cited.
        repository: The repository doing the submitting or citing.

    Raises:
        UserError: ``kind="contract_environment_incompatible"`` when the
            contract's environment names another repository.
    """
    measured_in = contract.environment.repository
    if measured_in is not None and measured_in != repository:
        logger.warning(
            f"_require_compatible_environment environment_refused "
            f"contract_id={contract.contract_id!r} measured_in={measured_in!r} "
            f"repository={repository!r}"
        )
        raise UserError(
            f"contract {contract.contract_id} was measured in repository {measured_in!r}, "
            f"not {repository!r}; a measurement does not transfer across repositories",
            kind="contract_environment_incompatible",
        )


def _require_measured_band(contract: MeasuredContract, *, required_band: ScaleBand) -> ScaleBand:
    """Return the contract's measured band once it clears *required_band*.

    Shared by every write path that puts a contract row into state, so a
    row can never reach ``state.json`` measured below the band its
    checkpoint asserts over -- not on first promotion, and not through a
    later metadata rewrite either.

    Args:
        contract: Contract about to be written.
        required_band: Band the implementing checkpoint asserts over.

    Returns:
        The contract's measured scale band.

    Raises:
        UserError: ``kind="scale_band_below_checkpoint"`` when the
            measured band is below *required_band*.
    """
    observed_band = contract.environment.scale_band
    if not scale_band_satisfies(observed_band, required=required_band):
        logger.warning(
            f"measured contract reject contract_id={contract.contract_id!r} "
            f"observed_band={observed_band.value!r} required_band={required_band.value!r}"
        )
        raise UserError(
            f"contract {contract.contract_id} was measured at scale band "
            f"{observed_band.value!r} but its checkpoint asserts over "
            f"{required_band.value!r}; re-measure at the larger band before promoting",
            kind="scale_band_below_checkpoint",
        )
    return observed_band


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
        A :class:`ContractPromotion` carrying the URN, the content digest
        and both envelopes.

    Raises:
        UserError: When the contract's measured
            :attr:`~eawf.kernel.spec.measured_contract.MeasurementEnvironment.scale_band`
            is below *required_band* (``kind="scale_band_below_checkpoint"``),
            or when the contract id is already registered
            (``kind="InvalidInput"``, raised by the underlying artifact
            mutator).
    """
    observed_band = _require_measured_band(contract, required_band=required_band)

    metadata = _contract_metadata(contract)
    content_digest = _content_digest(metadata)
    artifact_event = add_artifact(
        state,
        artifact_id=contract.contract_id,
        kind=CONTRACT_ARTIFACT_KIND,
        uri=CONTRACT_BODY_URI,
        scope_id=scope_id,
        sha256=content_digest.removeprefix("sha256:"),
        metadata=metadata,
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
        content_digest=content_digest,
        evidence=evidence,
        artifact_event=artifact_event,
        evidence_envelope=evidence_envelope,
    )


def submit_evidence(
    state: State, *, report: SpikeReport, scope_id: str
) -> tuple[ContractPromotion, ...]:
    """Promote every contract of a verified :class:`SpikeReport`, mutating *state* in place.

    This is the evidence path: a
    :class:`~eawf.kernel.spec.measured_contract.MeasuredContract` becomes
    canonical only by being submitted here out of a verified report, never
    by being hand-written into a promotion table. Each contract is
    promoted through :func:`promote_measured_contract`, so it inherits URN
    minting, the duplicate-id guard, and the recorded content digest that
    makes the write an immutable artifact revision. A blank
    ``boundary`` never reaches this function at all: the field is
    non-blank at the :class:`MeasuredContract` schema, so a report
    carrying one fails to parse. The caller appends every returned
    envelope to their stores inside the same transaction.

    Args:
        state: Mutable state under transaction.
        report: The spike report being submitted. Refused unless
            :attr:`~eawf.kernel.spec.measured_contract.SpikeReport.verified`.
        scope_id: Owning scope (normally the project code), and the
            repository every contract's environment is checked against.

    Returns:
        One :class:`ContractPromotion` per contract in *report*, in
        report order. Empty when the report measured nothing.

    Raises:
        UserError: ``kind="spike_report_unverified"`` when *report* is not
            verified; ``kind="contract_environment_incompatible"`` when a
            contract's environment names a repository other than
            *scope_id*; ``kind="scale_band_below_checkpoint"`` or
            ``kind="InvalidInput"`` as raised by
            :func:`promote_measured_contract`.
    """
    if not report.verified:
        logger.warning(f"submit_evidence unverified report_id={report.report_id!r}")
        raise UserError(
            f"spike report {report.report_id!r} is not verified, so nothing in it has "
            "earned canonical status; re-run its verifier and submit the verified report",
            kind="spike_report_unverified",
        )
    for contract in report.contracts:
        _require_compatible_environment(contract, repository=scope_id)
    promotions = tuple(
        promote_measured_contract(
            state,
            contract=contract,
            scope_id=scope_id,
            required_band=contract.environment.scale_band,
        )
        for contract in report.contracts
    )
    logger.info(
        f"submit_evidence report_id={report.report_id!r} scope_id={scope_id!r} "
        f"contracts={len(promotions)}"
    )
    return promotions


def refresh_contract_metadata(
    state: State,
    *,
    contract: MeasuredContract,
    required_band: ScaleBand,
) -> ContractMetadataRefresh:
    """Rewrite a registered contract row's metadata, mutating *state* in place.

    A promoted row is a snapshot of the contract as it read on promotion
    day. When the typed contract is later corrected -- a key renamed, an
    observation re-partitioned -- the registered row keeps serving the
    stale shape to every consumer that resolves the URN, and the
    duplicate-id guard means it cannot simply be promoted again. This is
    the migration path: the in-code contract is re-rendered and written
    over the row's ``metadata``, leaving identity (``id``, ``kind``,
    ``uri``, ``urn``, ``created_at``) fixed.

    No evidence row is minted. The promotion already recorded that the
    measurement exists; a refresh restates the same measurement in the
    current shape, so it is an artifact event and nothing more.

    The caller appends the returned envelope to the event store inside
    the same transaction that commits *state*.

    Args:
        state: Mutable state under transaction.
        contract: In-code contract the row is rewritten from.
        required_band: Scale band the implementing checkpoint asserts
            over, re-checked so a rewrite cannot lower a registered row
            below the band its checkpoint needs.

    Returns:
        A :class:`ContractMetadataRefresh` naming the keys that moved.

    Raises:
        UserError: ``kind="NotFound"`` when the contract has no
            registered row to refresh (the message names the promotion
            command); ``kind="scale_band_below_checkpoint"`` when the
            contract is measured below *required_band*.
    """
    _require_measured_band(contract, required_band=required_band)

    registered = state.artifacts.get(contract.contract_id)
    if registered is None:
        raise UserError(
            f"contract {contract.contract_id} has no registered artifact row to refresh; "
            f"promote it first: {_promotion_command(contract.contract_id)}",
            kind="NotFound",
        )

    metadata = _contract_metadata(contract)
    changed_keys = tuple(
        key
        for key in sorted(set(metadata) | set(registered.metadata))
        if registered.metadata.get(key) != metadata.get(key)
    )

    now = datetime.now(UTC)
    artifacts = dict(state.artifacts)
    artifacts[contract.contract_id] = registered.model_copy(
        update={"metadata": metadata, "sha256": _content_digest(metadata).removeprefix("sha256:")}
    )
    state.artifacts = artifacts
    state.updated_at = now

    scope_id = urn_mod.parse(registered.urn).owner
    summary = (
        f"contract {contract.contract_id} metadata refreshed ({len(changed_keys)} key(s) rewritten)"
    )
    artifact_event = _io.event_envelope(
        event_id=f"EVT-artifact-refresh-{contract.contract_id}-{int(now.timestamp() * 1000)}",
        scope_id=scope_id,
        event_type="artifact.update",
        actor="cli",
        command="artifact promote-contract --refresh-metadata",
        args={
            "contract_id": contract.contract_id,
            "changed_keys": list(changed_keys),
        },
        summary=summary,
        artifact_ids=[contract.contract_id],
    )
    logger.info(
        f"refresh_contract_metadata contract_id={contract.contract_id!r} "
        f"urn={registered.urn!r} changed_keys={changed_keys!r}"
    )
    return ContractMetadataRefresh(
        contract=contract,
        artifact_id=contract.contract_id,
        urn=registered.urn,
        changed_keys=changed_keys,
        artifact_event=artifact_event,
    )


def _require_citable_here(state: State, artifact: Artifact) -> None:
    """Refuse *artifact* when its recorded environment names another repository.

    Only a promoted :class:`~eawf.kernel.spec.measured_contract.MeasuredContract`
    row carries an ``environment`` key in its metadata, so any other
    artifact kind passes through unchecked; a contract whose environment
    carries no ``repository`` (or a state with no ``project`` to compare
    against) is likewise unrestricted.

    Args:
        state: State the citation was resolved against.
        artifact: The resolved artifact row.

    Raises:
        UserError: ``kind="contract_environment_incompatible"`` when the
            artifact's recorded environment names a repository other than
            *state*'s own project.
    """
    environment = artifact.metadata.get("environment")
    if not isinstance(environment, Mapping) or state.project is None:
        return
    measured_in = environment.get("repository")
    if measured_in is not None and measured_in != state.project.code:
        logger.warning(
            f"resolve_contract_citation environment_refused artifact_id={artifact.id!r} "
            f"measured_in={measured_in!r} repository={state.project.code!r}"
        )
        raise UserError(
            f"contract {artifact.id} was measured in repository {measured_in!r}, not "
            f"{state.project.code!r}; a measurement does not transfer across repositories",
            kind="contract_environment_incompatible",
        )


def resolve_contract_citation(state: State, citation: str) -> Artifact:
    """Resolve *citation* to a promoted artifact row.

    Accepts an artifact id or a full ``urn:eawf:v1:artifact:<scope>/<id>``
    URN. A citation that addresses the gitignored spike tree is refused
    outright: that path means the contract was never promoted, so the
    error names the promotion command rather than reporting a bare
    not-found the operator cannot act on. A citation that resolves to a
    contract measured in another repository is refused too -- see
    :func:`_require_citable_here`.

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
            registered; ``kind="contract_environment_incompatible"`` when
            it resolves to a contract measured in another repository.
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
        artifact = show_artifact(state, parsed.id)
    else:
        artifact = show_artifact(state, citation)
    _require_citable_here(state, artifact)
    return artifact


__all__ = [
    "ARTIFACT_URN_PREFIX",
    "CONTRACT_ARTIFACT_KIND",
    "CONTRACT_BODY_URI",
    "IMPORTER_CORPUS_MAGNITUDE",
    "LOCAL_SPIKE_ROOT",
    "PREFLIGHT_CHECKPOINT_BANDS",
    "PREFLIGHT_CONTRACTS",
    "ContractMetadataRefresh",
    "ContractPromotion",
    "promote_measured_contract",
    "refresh_contract_metadata",
    "resolve_contract_citation",
    "submit_evidence",
]
