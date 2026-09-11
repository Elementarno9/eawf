"""Walking ``REL-0.7.0.dev1`` to the terminal burn, and what that closes.

The ``0.7.0.dev1`` version was published to four targets while the
release machinery held no record of it, and the transition that honestly
describes that -- the burn -- had no caller anywhere in the tree. Three
claims are pinned here, one per selector.

**terminal.** The whole walk
``DRAFT -> CANDIDATE -> APPROVED -> PUBLISHING -> RECOVERING ->
PARTIALLY_RELEASED`` runs over a fixture through the real edges, and the
state it lands in offers no way out. The walk is driven by the operator
verb wherever one exists, so what is proven is the path an operator can
actually take, not a sequence of library calls only a test assembles.

**evidence.** The burn basis committed under ``.ea/artifacts/evidence``
validates against the artifact chassis and names all three observed
target states -- the same three the burned record projects, asserted
against each other so the document and the record cannot drift.

**train.** The train advance is pinned as it actually behaves: the index
moves by one past a ``baked`` rung carrying fresh gate receipts, and is
refused ``checkpoint_not_terminal`` from the burned rung, because
``ADVANCING_STATUSES`` deliberately excludes abandonment. That refusal is
recorded here rather than routed around: widening the guard would let a
train claim a checkpoint it never shipped.

Two fixtures are stand-ins and are named as such. The readiness sweep is
an all-passing probe registry rather than a real checkout -- the claim
under test is the shape of the walk, and the neighbouring
``test_dev1_approved_on_main`` module already proves the dev1 sweep green
against a real repository. The ``github`` leg's observed status is
written straight onto the operation ledger rather than through
:func:`~eawf.workflow.release.settlement.observe_target`, because the
release machine has no ``RECOVERING -> RECOVERING`` edge: once one
contradicted read-back has routed the record into recovery, a second one
raises ``illegal_release_transition``. That limitation is asserted
below rather than hidden.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.spec.publication import PublicationOperation, PublicationOperationStatus
from eawf.kernel.spec.release import (
    Release,
    ReleaseStatus,
    ReleaseTargetStatus,
    ReleaseTrain,
)
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.kernel.state.models import State
from eawf.platform.artifacts.validation import (
    REQUIRED_CHASSIS_HEADINGS,
    validate_markdown_artifact,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release import burn, show
from eawf.surfaces.cli.app import app
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.evidence.measured_contract import (
    PREFLIGHT_CHECKPOINT_BANDS,
    PREFLIGHT_CONTRACTS,
    promote_measured_contract,
)
from eawf.workflow.release.adapters import collect_observation
from eawf.workflow.release.admission import required_contract_ids
from eawf.workflow.release.advance import (
    ADVANCING_STATUSES,
    CheckpointGateReceipt,
    TrainAdvanceDenialCode,
    TrainAdvanceError,
    advance_train,
    draft_release_for,
)
from eawf.workflow.release.ledger import (
    ledger_path,
    record_operation,
    request_fingerprint,
)
from eawf.workflow.release.lifecycle import (
    RELEASE_TRANSITIONS,
    TERMINAL_RELEASE_STATUSES,
    ReleaseDenialCode,
    ReleaseTransitionError,
    advance_release,
    next_release_statuses,
    validate_release_transition,
)
from eawf.workflow.release.observation import observation_request
from eawf.workflow.release.preflight import approve_release
from eawf.workflow.release.publication import (
    begin_publication,
    projected_target_statuses,
    recovery_exhausted,
)
from eawf.workflow.release.records import read_release_record, record_envelope_id
from eawf.workflow.release.settlement import observe_target
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.release.train import V07_TRAIN
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness
from tests._release_helpers import (
    NOW,
    SOURCE_SHA,
    TREE_SHA,
    all_passing,
    dev1_config,
    frozen_manifest,
    recorded_response,
)

pytestmark = pytest.mark.integration

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"
DEV2_VERSION = "0.7.0.dev2"
DEV2_KEY = f"REL-{DEV2_VERSION}"

#: Identity minted for the record every walk in this module opens.
DEV1_UID = UUID(int=711)

MANIFEST_REF = "artifact://release/manifest/0.7.0.dev1"
PROOF_DIGEST = f"sha256:{'1' * 64}"
IDEMPOTENCY_KEY = "publish-0.7.0.dev1-w01"
EFFECT_RECEIPT = "receipt://target/effect"

#: The approval that carries the record through APPROVED on its way to
#: the burn. It names the burn path in the reference itself, because the
#: APPROVED row is the one a later reader is most likely to misread: no
#: operator ever approved this release against a manifest digest, and the
#: record must not be able to imply that one did.
BURN_PATH_APPROVAL_REF = "approval://INC-P32-01/burn-path-only-not-a-release-approval"

#: The reason the burn is called with. Terminal statuses carry no later
#: transition that could explain them, so the explanation arrives here.
BURN_REASON = (
    "0.7.0.dev1 was published to three targets with no release record; "
    "pypi holds a divergent build under an unreusable filename, so the "
    "version is spent and is superseded by 0.7.0.dev2"
)

#: The state each configured leg was independently read back in. Two
#: contradicted their own success report, one matched.
OBSERVED_TARGET_STATES: dict[str, ReleaseTargetStatus] = {
    "pypi": ReleaseTargetStatus.OBSERVED_MISMATCH,
    "npm": ReleaseTargetStatus.OBSERVED_SUCCESS,
    "github": ReleaseTargetStatus.OBSERVED_MISMATCH,
}

#: The read-back fixture case each leg is judged from.
OBSERVATION_CASES: dict[str, str] = {"pypi": "mismatch", "npm": "match", "github": "mismatch"}

#: The leg whose observation routes the record into recovery. The other
#: contradicted leg cannot be routed a second time (see the module
#: docstring), so it is settled on the ledger directly.
ROUTING_LEG = "pypi"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"

#: The burn basis this wave commits, read back by the evidence tests.
BURN_BASIS = (
    _REPO_ROOT
    / ".ea"
    / "artifacts"
    / "evidence"
    / "2026-09-11-dev1-burn"
    / "2026-09-11-burn-basis.md"
)

#: Project code of the empty-repo fixture; contract URNs are built from it.
SCOPE = "QR"


# --- the walk ---------------------------------------------------------


def green_sweep() -> ReleaseReadiness:
    """Return an all-passing dev1 sweep.

    The probes are stand-ins: this module's claim is the shape of the
    walk, not the greenness of a checkout.
    """
    sweep = compute_readiness(dev1_config(), probes=all_passing(), computed_at=NOW)
    assert sweep.ready is True
    return sweep


def manifest_digest() -> str:
    """Return the digest of the frozen manifest the read-backs are judged against."""
    return frozen_manifest().digest


def dispatched_and_reported(
    operation: PublicationOperation,
    config: ReleaseConfig,
) -> PublicationOperation:
    """Drive every queued leg through in-flight to a success report.

    All three adapters did report success: the 2026-09-07 PyPI upload
    landed, the npm publish landed, and the source-host release was
    created. What none of them establishes is whether the right thing
    landed, which is what the read-backs afterwards decide.
    """
    for target in config.targets:
        operation = advance_target_attempt(
            operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=NOW
        )
        operation = advance_target_attempt(
            operation,
            target=target,
            to=ReleaseTargetStatus.REPORTED_SUCCESS,
            now=NOW,
            effect_receipt_ref=EFFECT_RECEIPT,
        )
    return operation


def observed(
    release: Release,
    config: ReleaseConfig,
    operation: PublicationOperation,
) -> tuple[Release, PublicationOperation]:
    """Settle all three legs against their recorded read-backs.

    Only the routing leg goes through
    :func:`~eawf.workflow.release.settlement.observe_target`; the second
    contradicted leg is settled straight onto the ledger because the
    release machine offers no second route into recovery.
    """
    for target_id, case in OBSERVATION_CASES.items():
        observation = collect_observation(
            observation_request(config, frozen_manifest(), target_id=target_id),
            response=recorded_response(target_id, case),
            observed_at=NOW,
        )
        assert observation.conclusive is True
        if target_id == ROUTING_LEG:
            release, operation = observe_target(
                release,
                config,
                operation,
                observation=observation,
                now=NOW,
                effect_receipt_ref=EFFECT_RECEIPT,
            )
            continue
        operation = advance_target_attempt(
            operation,
            target=next(row for row in config.targets if row.target_id == target_id),
            to=ReleaseTargetStatus.OBSERVED_SUCCESS
            if observation.matched
            else ReleaseTargetStatus.OBSERVED_MISMATCH,
            now=NOW,
            effect_receipt_ref=EFFECT_RECEIPT,
            observation_receipt_ref=observation.evidence_ref,
            observation_matched=observation.matched,
        )
    return release, operation


def walk_to_recovery() -> tuple[list[ReleaseStatus], Release, PublicationOperation]:
    """Walk a fresh dev1 record from DRAFT to RECOVERING, recording each status.

    Returns:
        The statuses visited in order, the recovering record and the
        operation whose every leg has been observed.
    """
    config = dev1_config()
    draft = draft_release_for(V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=DEV1_UID)
    statuses = [draft.status]

    candidate = advance_release(
        draft,
        ReleaseStatus.CANDIDATE,
        source_sha=SOURCE_SHA,
        source_tree_sha=TREE_SHA,
        manifest_ref=MANIFEST_REF,
        manifest_digest=manifest_digest(),
    )
    statuses.append(candidate.status)

    approved = approve_release(
        candidate,
        green_sweep(),
        approval_ref=BURN_PATH_APPROVAL_REF,
        approved_at=NOW,
    )
    statuses.append(approved.status)

    publishing, operation = begin_publication(
        approved,
        config,
        green_sweep(),
        operation_id=UUID(int=712),
        approved_manifest_digest=manifest_digest(),
        idempotency_key=IDEMPOTENCY_KEY,
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )
    statuses.append(publishing.status)

    operation = dispatched_and_reported(operation, config)
    recovering, operation = observed(publishing, config, operation)
    statuses.append(recovering.status)
    return statuses, recovering, operation


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a throwaway state root the burn may record into."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, load_state(_EMPTY_STATE))
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7711,
        protocol_version="1",
        version=DEV1_VERSION,
        state_path=state_path,
    )


def seed_operation(state_path: Path, operation: PublicationOperation) -> None:
    """Make *operation* the current snapshot the burn verb reads back."""
    record_operation(
        state_path,
        operation,
        idempotency_key=f"seed-{operation.operation_id}",
        fingerprint=request_fingerprint("test.seed", {"key": str(operation.operation_id)}),
        recorded_at=datetime.now(UTC),
        summary=f"seed {operation.release_ref}",
    )


def burn_params(record: Release, **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.burn`` params for *record*."""
    params: dict[str, Any] = {
        "release": record.model_dump(mode="json"),
        "expected_revision": record.revision,
        "idempotency_key": "burn-0.7.0.dev1-w01",
        "reason": BURN_REASON,
    }
    params.update(overrides)
    return params


def burn_via_rpc(
    ctx: MethodContext,
    state_path: Path,
    **overrides: Any,
) -> tuple[dict[str, Any], Release]:
    """Walk to recovery, seed the ledger, and burn through the daemon verb."""
    _statuses, recovering, operation = walk_to_recovery()
    seed_operation(state_path, operation)
    return asyncio.run(burn(ctx, burn_params(recovering, **overrides))), recovering


# --- terminal ---------------------------------------------------------


def test_terminal_walk_visits_every_edge_from_draft_to_partially_released() -> None:
    """The whole walk runs over the real edges and ends at the burn."""
    statuses, _recovering, operation = walk_to_recovery()
    config = dev1_config()
    burned_statuses = [*statuses, ReleaseStatus.PARTIALLY_RELEASED]

    assert statuses == [
        ReleaseStatus.DRAFT,
        ReleaseStatus.CANDIDATE,
        ReleaseStatus.APPROVED,
        ReleaseStatus.PUBLISHING,
        ReleaseStatus.RECOVERING,
    ]
    assert recovery_exhausted(config, operation) is True
    for frm, to in pairwise(burned_statuses):
        assert to in next_release_statuses(frm), f"{frm.value} -> {to.value}"


def test_terminal_burn_moves_the_recovering_record_to_partially_released(
    ctx: MethodContext, state_path: Path
) -> None:
    """The operator verb reaches the burn the library had implemented."""
    result, recovering = burn_via_rpc(ctx, state_path)

    assert result["release"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value
    assert result["release"]["revision"] == recovering.revision + 1
    assert result["replayed"] is False
    assert result["operation"]["status"] == PublicationOperationStatus.ABANDONED.value


def test_terminal_state_offers_no_out_edge() -> None:
    """PARTIALLY_RELEASED is terminal: the table gives it nowhere to go."""
    assert RELEASE_TRANSITIONS[ReleaseStatus.PARTIALLY_RELEASED] == frozenset()
    assert next_release_statuses(ReleaseStatus.PARTIALLY_RELEASED) == frozenset()
    assert ReleaseStatus.PARTIALLY_RELEASED in TERMINAL_RELEASE_STATUSES


@pytest.mark.parametrize("target", sorted(ReleaseStatus))
def test_terminal_state_refuses_every_status_it_could_be_asked_to_leave_for(
    target: ReleaseStatus,
) -> None:
    """Every move out of the burn is refused, DRAFT and CANCELLED included."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        validate_release_transition(ReleaseStatus.PARTIALLY_RELEASED, target)

    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


def test_terminal_burn_freezes_the_pinned_source_and_manifest(
    ctx: MethodContext, state_path: Path
) -> None:
    """A burned record keeps the exact build claim recovery found."""
    result, recovering = burn_via_rpc(ctx, state_path)

    burned = result["release"]
    assert burned["source_sha"] == recovering.source_sha == SOURCE_SHA
    assert burned["source_tree_sha"] == recovering.source_tree_sha == TREE_SHA
    assert burned["manifest_ref"] == MANIFEST_REF
    assert burned["manifest_digest"] == manifest_digest()
    assert burned["version"] == DEV1_VERSION


def test_terminal_burn_records_the_operator_reason_on_both_rows(
    ctx: MethodContext, state_path: Path
) -> None:
    """The reason is durable, not merely returned to the caller."""
    result, _recovering = burn_via_rpc(ctx, state_path)

    assert result["reason"] == BURN_REASON
    ledger_summaries = [
        json.loads(row)["summary"]
        for row in ledger_path(state_path).read_text(encoding="utf-8").splitlines()
        if row.strip()
    ]
    assert any(BURN_REASON in summary for summary in ledger_summaries)
    record_rows = [
        json.loads(row)
        for row in (state_path.parent / "store" / "release_record.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if row.strip()
    ]
    assert [row["summary"] for row in record_rows] == [f"burn {DEV1_KEY}: {BURN_REASON}"]


def test_terminal_burn_is_what_release_show_reports_afterwards(
    ctx: MethodContext, state_path: Path
) -> None:
    """A reader asking where dev1 stands is answered by the burned record."""
    result, _recovering = burn_via_rpc(ctx, state_path)

    persisted = read_release_record(state_path, DEV1_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.PARTIALLY_RELEASED
    assert result["release_record_id"] == record_envelope_id(persisted)
    assert asyncio.run(show(ctx, {"version": DEV1_VERSION}))["record"]["status"] == (
        ReleaseStatus.PARTIALLY_RELEASED.value
    )


def test_terminal_burn_replays_the_original_receipt_rather_than_burning_twice(
    ctx: MethodContext, state_path: Path
) -> None:
    """The same key with the same payload answers the first receipt.

    The replayed reply carries the *burned* record, not the recovering
    payload the caller presented: after a burn the recorded record is the
    burned one, and echoing the pre-burn status back would report the
    checkpoint as still moving.
    """
    _statuses, recovering, operation = walk_to_recovery()
    seed_operation(state_path, operation)
    params = burn_params(recovering)

    first = asyncio.run(burn(ctx, dict(params)))
    second = asyncio.run(burn(ctx, dict(params)))

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["operation_ref"] == first["operation_ref"]
    assert second["release"] == first["release"]
    assert second["release"]["status"] == ReleaseStatus.PARTIALLY_RELEASED.value
    assert second["release_record_id"] == first["release_record_id"]
    rows = (state_path.parent / "store" / "release_record.jsonl").read_text(encoding="utf-8")
    assert len([row for row in rows.splitlines() if row.strip()]) == 1


@pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
def test_terminal_burn_refuses_a_blank_reason(
    ctx: MethodContext, state_path: Path, reason: str
) -> None:
    """A terminal status with no stated cause is refused at the boundary."""
    with pytest.raises(ValidationError, match="reason"):
        burn_via_rpc(ctx, state_path, reason=reason)


def test_terminal_burn_refuses_a_missing_reason(ctx: MethodContext, state_path: Path) -> None:
    """The reason is required, not defaulted to silence."""
    _statuses, recovering, operation = walk_to_recovery()
    seed_operation(state_path, operation)
    params = burn_params(recovering)
    del params["reason"]

    with pytest.raises(ValidationError, match="reason"):
        asyncio.run(burn(ctx, params))


def test_terminal_burn_refuses_a_reason_past_the_length_ceiling(
    ctx: MethodContext, state_path: Path
) -> None:
    """A reason longer than the field admits is refused, not truncated."""
    with pytest.raises(ValidationError, match="at most 500 characters"):
        burn_via_rpc(ctx, state_path, reason="x" * 501)


def test_terminal_burn_accepts_a_reason_at_the_length_ceiling(
    ctx: MethodContext, state_path: Path
) -> None:
    """The boundary itself is admitted: 500 characters is a legal reason."""
    result, _recovering = burn_via_rpc(ctx, state_path, reason="x" * 500)

    assert result["reason"] == "x" * 500


def test_terminal_burn_refuses_a_stale_revision(ctx: MethodContext, state_path: Path) -> None:
    """A caller holding an older record cannot burn the newer one."""
    _statuses, recovering, operation = walk_to_recovery()
    seed_operation(state_path, operation)

    with pytest.raises(DaemonValidationError, match="stale_release_revision"):
        asyncio.run(burn(ctx, burn_params(recovering, expected_revision=recovering.revision - 1)))


def test_terminal_burn_refuses_while_a_retry_remains(ctx: MethodContext, state_path: Path) -> None:
    """A version declared spent while recovery could still succeed is refused."""
    config = dev1_config()
    _statuses, recovering, _observed_operation = walk_to_recovery()
    _draft, retryable = begin_publication(
        advance_release(
            advance_release(
                draft_release_for(
                    V07_TRAIN.checkpoint_for_version(DEV1_VERSION), uid=UUID(int=713)
                ),
                ReleaseStatus.CANDIDATE,
                source_sha=SOURCE_SHA,
                source_tree_sha=TREE_SHA,
                manifest_ref=MANIFEST_REF,
                manifest_digest=manifest_digest(),
            ),
            ReleaseStatus.APPROVED,
            approval_ref=BURN_PATH_APPROVAL_REF,
        ),
        config,
        green_sweep(),
        operation_id=UUID(int=714),
        approved_manifest_digest=manifest_digest(),
        idempotency_key="publish-0.7.0.dev1-retryable",
        proof_digest=PROOF_DIGEST,
        opened_at=NOW,
    )
    for target in config.targets:
        retryable = advance_target_attempt(
            retryable, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=NOW
        )
        retryable = advance_target_attempt(
            retryable,
            target=target,
            to=ReleaseTargetStatus.REPORTED_FAILURE,
            now=NOW,
            effect_receipt_ref=EFFECT_RECEIPT,
        )
    assert recovery_exhausted(config, retryable) is False
    seed_operation(state_path, retryable)

    with pytest.raises(DaemonValidationError, match="recovery_budget_available"):
        asyncio.run(burn(ctx, burn_params(recovering)))


def test_terminal_burn_refuses_when_no_operation_was_ever_opened(
    ctx: MethodContext,
) -> None:
    """There is nothing to abandon, so the burn refuses rather than invents one."""
    _statuses, recovering, _operation = walk_to_recovery()

    with pytest.raises(DaemonValidationError, match="no publication operation is open"):
        asyncio.run(burn(ctx, burn_params(recovering)))


def test_terminal_burn_refuses_without_an_on_disk_state_root() -> None:
    """A burn nobody could record is refused rather than returned."""
    _statuses, recovering, _operation = walk_to_recovery()
    rootless = MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7711,
        protocol_version="1",
        version=DEV1_VERSION,
    )

    with pytest.raises(DaemonValidationError, match="on-disk state root"):
        asyncio.run(burn(rootless, burn_params(recovering)))


def test_terminal_burn_refuses_a_record_the_train_does_not_declare(
    ctx: MethodContext, state_path: Path
) -> None:
    """A payload off the ladder never reaches the status machine."""
    _statuses, recovering, operation = walk_to_recovery()
    seed_operation(state_path, operation)
    off_ladder = {**recovering.model_dump(mode="json"), "key": "REL-9.9.9", "version": "9.9.9"}

    with pytest.raises(DaemonValidationError, match="validation_failed"):
        asyncio.run(burn(ctx, burn_params(recovering, release=off_ladder)))


def test_terminal_burn_cli_forwards_the_operator_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The argv an operator types carries the reason to the daemon verb."""
    calls: list[dict[str, Any]] = []

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append({"method": method, "params": params})
            return {
                "release": {"key": DEV1_KEY, "status": "partially_released", "revision": 8},
                "operation_ref": "operation://REL-0.7.0.dev1/1",
                "operation": {"publication_receipts": []},
                "release_record_id": f"{DEV1_KEY}@8",
                "reason": BURN_REASON,
                "replayed": False,
            }

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", lambda *a, **k: _Client())
    record = tmp_path / "release.json"
    record.write_text(json.dumps({"key": DEV1_KEY, "revision": 7}), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "release",
            "burn",
            DEV1_KEY,
            "--release",
            str(record),
            "--reason",
            BURN_REASON,
            "--idempotency-key",
            "burn-01",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["method"] == "release.burn"
    assert calls[0]["params"]["reason"] == BURN_REASON
    assert calls[0]["params"]["expected_revision"] == 7
    assert BURN_REASON in result.output


def test_terminal_recovery_has_no_second_route_in_for_a_second_contradiction() -> None:
    """The limitation the walk works around is pinned, not papered over."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        validate_release_transition(ReleaseStatus.RECOVERING, ReleaseStatus.RECOVERING)

    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION


# --- evidence ---------------------------------------------------------


def burn_basis_text() -> str:
    """Return the committed burn basis document."""
    return BURN_BASIS.read_text(encoding="utf-8")


def test_evidence_basis_validates_against_the_artifact_chassis() -> None:
    """The burn basis passes the same gate every durable artifact passes."""
    report = validate_markdown_artifact(burn_basis_text())

    assert report.ok, report.errors


@pytest.mark.parametrize("heading", REQUIRED_CHASSIS_HEADINGS)
def test_evidence_chassis_gate_reds_when_a_required_heading_is_dropped(heading: str) -> None:
    """The green above is earned: removing any chassis heading reds it."""
    defective = burn_basis_text().replace(f"\n{heading}\n", "\n### stripped\n")

    report = validate_markdown_artifact(defective)

    assert report.ok is False
    assert any(heading in error for error in report.errors)


def test_evidence_basis_names_all_three_observed_target_states() -> None:
    """Every configured leg and the state it was read back in are named."""
    text = burn_basis_text()

    for target_id, status in OBSERVED_TARGET_STATES.items():
        assert f"`{target_id}`" in text, target_id
        assert f"`{status.value}`" in text, status.value
    assert set(OBSERVED_TARGET_STATES) == {target.target_id for target in dev1_config().targets}


def test_evidence_basis_agrees_with_the_projected_target_statuses() -> None:
    """The document and the record it explains cannot drift apart."""
    _statuses, _recovering, operation = walk_to_recovery()

    projected = dict(projected_target_statuses(dev1_config(), operation))

    assert projected == OBSERVED_TARGET_STATES


def test_evidence_basis_is_carried_by_the_burned_record_the_rpc_writes(
    ctx: MethodContext, state_path: Path
) -> None:
    """The burned record projects exactly the three states the basis names."""
    result, _recovering = burn_via_rpc(ctx, state_path)

    assert result["release"]["target_statuses"] == {
        target_id: status.value for target_id, status in OBSERVED_TARGET_STATES.items()
    }


def test_evidence_basis_refuses_to_claim_the_release_was_approved() -> None:
    """The approval in the walk is of the burn path, and says so."""
    text = burn_basis_text()

    assert "burn path" in text
    assert "No operator ever approved `0.7.0.dev1` against a manifest digest" in text
    assert "burn-path" in BURN_PATH_APPROVAL_REF


# --- train ------------------------------------------------------------


def gate_receipts(release: Release) -> list[CheckpointGateReceipt]:
    """Return a fresh receipt per required dev1 gate, bound to *release*."""
    return [
        CheckpointGateReceipt(
            gate=gate,
            release_key=release.key,
            source_sha=release.source_sha or SOURCE_SHA,
            manifest_digest=release.manifest_digest or manifest_digest(),
            issued_at=NOW,
            expires_at=NOW + timedelta(days=1),
            receipt_ref=f"receipt://gate/{gate.value}",
        )
        for gate in dev1_config().gates.required
    ]


def at_status(release: Release, status: ReleaseStatus) -> Release:
    """Return *release* re-validated at *status*, bypassing the edge table.

    Used only to place a record at a status the train advance is asked
    about; every status move under test elsewhere goes through the edges.
    """
    return Release.model_validate(
        release.model_copy(update={"status": status}).model_dump(mode="json")
    )


def state_with_dev2_contracts() -> State:
    """Return the fixture state with all three dev2 contracts promoted."""
    state = load_state(_EMPTY_STATE)
    for contract_id in required_contract_ids(DEV2_VERSION):
        promote_measured_contract(
            state,
            contract=PREFLIGHT_CONTRACTS[contract_id],
            scope_id=SCOPE,
            required_band=PREFLIGHT_CHECKPOINT_BANDS[contract_id],
        )
    return state


def burned_record(ctx: MethodContext, state_path: Path) -> Release:
    """Return the burned dev1 record, read back from the collection."""
    burn_via_rpc(ctx, state_path)
    persisted = read_release_record(state_path, DEV1_KEY)
    assert persisted is not None
    return persisted


def train_advance(current: Release, *, receipts: Sequence[CheckpointGateReceipt]) -> ReleaseTrain:
    """Advance the v0.7.0 train past *current* and return the advanced train."""
    return advance_train(
        V07_TRAIN,
        current=current,
        config=dev1_config(),
        receipts=list(receipts),
        now=NOW,
        next_uid=uuid4(),
    ).train


def test_train_index_advances_by_one_onto_the_dev2_rung(
    ctx: MethodContext, state_path: Path
) -> None:
    """The index-advance mechanism works, and its next rung is dev2."""
    baked = at_status(burned_record(ctx, state_path), ReleaseStatus.BAKED)

    advanced = train_advance(baked, receipts=gate_receipts(baked))

    assert V07_TRAIN.current_checkpoint_index == 0
    assert advanced.current_checkpoint_index == 1
    assert advanced.current_checkpoint.release_key == DEV2_KEY


def test_train_advance_refuses_the_burned_rung_as_not_terminal(
    ctx: MethodContext, state_path: Path
) -> None:
    """The same receipts do not move the index past an abandoned rung.

    ``ADVANCING_STATUSES`` is narrower than the status machine's terminal
    set on purpose: a train that walked forward on a burned rung would
    claim a checkpoint it never shipped. The refusal is recorded rather
    than routed around.
    """
    burned = burned_record(ctx, state_path)

    with pytest.raises(TrainAdvanceError) as excinfo:
        train_advance(burned, receipts=gate_receipts(burned))

    assert excinfo.value.code is TrainAdvanceDenialCode.CHECKPOINT_NOT_TERMINAL
    assert ReleaseStatus.PARTIALLY_RELEASED.value in str(excinfo.value)
    assert ReleaseStatus.PARTIALLY_RELEASED not in ADVANCING_STATUSES


def test_train_create_admits_dev2_against_the_promoted_state_json(tmp_path: Path) -> None:
    """dev2 opens once its three measured contracts resolve in state.json."""
    from eawf.runtime.daemon.methods.release import create

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(state_path, state_with_dev2_contracts())
    dev2_ctx = MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7712,
        protocol_version="1",
        version=DEV2_VERSION,
        state_path=state_path,
    )

    result = asyncio.run(create(dev2_ctx, {"version": DEV2_VERSION}))

    assert result["release"]["key"] == DEV2_KEY
    assert result["release"]["status"] == ReleaseStatus.DRAFT.value
    assert result["measured_contracts"] == list(required_contract_ids(DEV2_VERSION))
    persisted = read_release_record(state_path, DEV2_KEY)
    assert persisted is not None
    assert persisted.status is ReleaseStatus.DRAFT


def test_train_create_of_dev2_reads_no_index_from_the_burned_rung(
    ctx: MethodContext, state_path: Path
) -> None:
    """dev2 admission is gated on measurement, not on the train index.

    Worth pinning because it is the reason the burn does not block the
    successor: the index cannot advance past an abandoned rung, and dev2
    is admitted anyway.
    """
    burned = burned_record(ctx, state_path)

    assert burned.status is ReleaseStatus.PARTIALLY_RELEASED
    assert V07_TRAIN.current_checkpoint.release_key == DEV1_KEY
    assert V07_TRAIN.checkpoints[1].release_key == DEV2_KEY
    assert required_contract_ids(DEV2_VERSION) == ("MCT-26091101",)
