"""``release.produce_receipts``: one stored receipt per passing gate, on pinned source.

The train advance only moves on receipts that bind a checkpoint's exact
source, and nothing produced them. These tests pin the producer:

* every required gate of the profile binding table gets exactly one
  stored receipt bound to the record's ``source_sha`` and
  ``manifest_digest``;
* signal gates are settled by the readiness rows of a sweep of the
  pinned source, component gates by their parent row, and the waiver
  gate by the waiver block itself;
* proof commands run through ``resolve_proof`` in a git worktree at the
  pinned source. The RPC cases move HEAD past the pin and give the green
  proof a marker file only the pinned commit carries, so a proof run in
  the working copy would fail;
* a failing, or timed-out, proof command stores no receipt for its gate,
  while the gates that passed keep theirs.

The RPC cases run on the real dev1 checkout from the package conftest,
with no probe patched. Only the binding table is swapped, so the three
dev1 proof commands run small committed tests instead of the whole
suite. The library cases cover the dev2 profile, the one with a waiver
gate, using injected probes and a stub runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.release.gate_binding import (
    GateBinding,
    ResolvedProofCommand,
    load_gate_bindings,
)
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
)
from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release import Release, ReleaseGateProfile, ReleaseStatus
from eawf.kernel.spec.release_config import ReleaseConfig, ReleaseGateName, load_release_config
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release_receipts import produce_receipts
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.workflow.release.checkpoint_receipts import (
    PROOF_BUDGET_MARGIN_SECONDS,
    ProofOutcome,
    ReceiptProductionError,
    pinned_worktree,
    produce_checkpoint_receipts,
    proof_budget_seconds,
)
from eawf.workflow.release.records import record_release
from eawf.workflow.release.train import DEV2_RELEASE_CONFIG_YAML, V07_TRAIN, gate_bindings_for
from eawf.workflow.release.train_store import (
    checkpoint_receipts_path,
    read_checkpoint_receipts,
)
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    SOURCE_SHA,
    dev1_binding_rows,
    dev1_config,
    dev1_draft,
    release_record,
    stage_passing_receipts,
)
from tests.integration.runtime.daemon.methods.conftest import (
    DEV1_VERSION,
    RELEASE_KEY,
    context_for,
    git,
    manifest_digest,
    pinned_payload,
)

pytestmark = pytest.mark.integration

#: The three dev1 gates settled by a proof command, in profile order.
PROOF_GATES = (
    ReleaseGateName.EPOCH1_STABILIZATION,
    ReleaseGateName.TELEMETRY_PRODUCER,
    ReleaseGateName.FRONT_DOOR_JOURNEY,
)

#: Proof argvs over the tests :func:`pinned` commits. Each passes the L0
#: argv policy the binding loader applies.
GREEN = ("pytest", "-q", "-p", "no:cacheprovider", "proofs/test_green.py")
RED = ("pytest", "-q", "-p", "no:cacheprovider", "proofs/test_red.py")
SLOW = ("pytest", "-q", "-p", "no:cacheprovider", "proofs/test_slow.py")

#: A proof that passes only where the pinned marker is checked out.
GREEN_TEST = """\
from pathlib import Path


def test_the_pinned_marker_is_checked_out():
    assert Path("proofs/marker.txt").read_text(encoding="utf-8") == "pinned\\n"
"""

RED_TEST = """\
def test_red_on_purpose():
    assert False, "red on purpose"
"""

SLOW_TEST = """\
import time


def test_outlives_its_budget():
    time.sleep(30)
"""


def dev1_bindings(
    front_door: Sequence[str] = GREEN,
    *,
    front_door_timeout: int = 120,
) -> Mapping[ReleaseGateName, GateBinding]:
    """Return the dev1 binding table with its proofs swapped for small tests.

    Args:
        front_door: The argv the ``front_door_journey`` proof runs.
        front_door_timeout: That proof's budget in seconds.

    Returns:
        The validated table; the five signal bindings are the authored ones.
    """
    rows = dev1_binding_rows()
    for row in rows:
        if row["gate"] in {gate.value for gate in PROOF_GATES}:
            front = row["gate"] == ReleaseGateName.FRONT_DOOR_JOURNEY.value
            row["proof"]["argv"] = list(front_door if front else GREEN)
            row["proof"]["timeout_seconds"] = front_door_timeout if front else 120
    return load_gate_bindings({"bindings": rows}, profile=ReleaseGateProfile.DEV1)


def use_bindings(monkeypatch: pytest.MonkeyPatch, table: Mapping[ReleaseGateName, Any]) -> None:
    """Make the handler read *table* as the dev1 binding table."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.release_receipts.gate_bindings_for",
        lambda _profile: table,
    )


def _write(repo: Path, relative: str, text: str) -> None:
    """Write *text* to *relative* under *repo*, creating its parents."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def pinned(dev1_checkout: Path) -> tuple[Path, str]:
    """Return the dev1 checkout and the commit its record pins, HEAD moved past it.

    The pinned commit carries the proof tests and a marker reading
    ``pinned``. The commit after it rewrites the marker, so only a proof
    run at the pin can pass the green test. The bare origin is shared by
    every copy of the session template, so nothing is pushed to it: the
    local ``origin/main`` ref, which is all the ancestry probe reads, is
    moved onto the new HEAD instead.
    """
    repo = dev1_checkout
    _write(repo, "proofs/marker.txt", "pinned\n")
    _write(repo, "proofs/test_green.py", GREEN_TEST)
    _write(repo, "proofs/test_red.py", RED_TEST)
    _write(repo, "proofs/test_slow.py", SLOW_TEST)
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", "test: commit the proof commands")
    pinned_sha = git(repo, "rev-parse", "HEAD")
    stage_passing_receipts(repo, version=DEV1_VERSION, source_sha=pinned_sha)
    _write(repo, "proofs/marker.txt", "moved\n")
    git(repo, "commit", "--quiet", "--all", "--message", "chore: move the marker past the pin")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, pinned_sha


def store_record(
    repo: Path,
    source_sha: str,
    *,
    status: ReleaseStatus = ReleaseStatus.BAKED,
) -> Release:
    """Store the dev1 record pinned to *source_sha* at *status* and return it."""
    record = Release.model_validate(
        {
            **pinned_payload(repo),
            "status": status.value,
            "source_sha": source_sha,
            "source_tree_sha": "b" * 40,
        }
    )
    record_release(repo / ".ea" / "state.json", record, recorded_at=NOW, summary="seed")
    return record


def produce(ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Run ``release.produce_receipts`` for dev1 and return its reply."""
    result: dict[str, Any] = asyncio.run(produce_receipts(ctx, {"version": DEV1_VERSION, **params}))
    return result


def refused(ctx: MethodContext, **params: Any) -> str:
    """Return the refusal a producer call that must not run answers."""
    with pytest.raises(DaemonValidationError) as excinfo:
        produce(ctx, **params)
    return str(excinfo.value)


def worktrees(repo: Path) -> list[str]:
    """Return the worktree paths *repo* knows about."""
    listing = git(repo, "worktree", "list", "--porcelain")
    return [line for line in listing.splitlines() if line.startswith("worktree ")]


# --- the producer over the real checkout ------------------------------------


def test_produce_receipts_stores_one_receipt_per_required_gate(
    pinned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight dev1 gates pass at the pin, so eight receipts are stored, each bound to it."""
    repo, pinned_sha = pinned
    use_bindings(monkeypatch, dev1_bindings())
    store_record(repo, pinned_sha)

    result = produce(context_for(repo))

    required = [gate.value for gate in dev1_config().gates.required]
    assert result["refused"] == []
    assert [row["gate"] for row in result["receipts"]] == required
    assert {row["source_sha"] for row in result["receipts"]} == {pinned_sha}
    assert {row["manifest_digest"] for row in result["receipts"]} == {manifest_digest()}
    stored = read_checkpoint_receipts(repo / ".ea" / "state.json", RELEASE_KEY)
    assert [receipt.model_dump(mode="json") for receipt in stored] == result["receipts"]
    # HEAD carries the moved marker, yet the marker proofs passed: they ran at the pin.
    assert git(repo, "rev-parse", "HEAD") != pinned_sha
    assert (repo / "proofs" / "marker.txt").read_text(encoding="utf-8") == "moved\n"


def test_produce_receipts_stores_none_for_a_failing_proof_command(
    pinned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The red proof's gate gets no receipt; the rest keep theirs; the worktree goes."""
    repo, pinned_sha = pinned
    use_bindings(monkeypatch, dev1_bindings(front_door=RED))
    store_record(repo, pinned_sha)

    result = produce(context_for(repo))

    (refusal,) = result["refused"]
    assert refusal["gate"] == ReleaseGateName.FRONT_DOOR_JOURNEY.value
    assert "exited 1" in refusal["detail"]
    assert "red on purpose" in refusal["detail"]
    stored = read_checkpoint_receipts(repo / ".ea" / "state.json", RELEASE_KEY)
    assert len(stored) == len(dev1_config().gates.required) - 1
    assert ReleaseGateName.FRONT_DOOR_JOURNEY not in {receipt.gate for receipt in stored}
    assert len(worktrees(repo)) == 1


def test_produce_receipts_refuses_a_proof_that_outlives_its_budget(
    pinned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proof killed at its timeout settles nothing."""
    repo, pinned_sha = pinned
    use_bindings(monkeypatch, dev1_bindings(front_door=SLOW, front_door_timeout=1))
    store_record(repo, pinned_sha)

    result = produce(context_for(repo))

    assert [row["gate"] for row in result["refused"]] == [ReleaseGateName.FRONT_DOOR_JOURNEY.value]
    assert "timed out after 1s" in result["refused"][0]["detail"]


def test_produce_receipts_stores_a_rerun_beside_the_first_run(
    pinned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun adds rows rather than overwriting, and the newest speak for each gate."""
    repo, pinned_sha = pinned
    use_bindings(monkeypatch, dev1_bindings())
    store_record(repo, pinned_sha)
    state_path = repo / ".ea" / "state.json"

    produce(context_for(repo))
    second = produce(context_for(repo))

    lines = checkpoint_receipts_path(state_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * len(dev1_config().gates.required)
    newest = read_checkpoint_receipts(state_path, RELEASE_KEY)
    assert [receipt.receipt_ref for receipt in newest] == [
        row["receipt_ref"] for row in second["receipts"]
    ]


def test_produce_receipts_refuses_a_source_git_cannot_check_out(
    pinned: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin the repository does not hold stores nothing."""
    repo, _pinned_sha = pinned
    use_bindings(monkeypatch, dev1_bindings())
    store_record(repo, "f" * 40)

    message = refused(context_for(repo))

    assert "cannot check out" in message
    assert not checkpoint_receipts_path(repo / ".ea" / "state.json").exists()


# --- refusals before anything runs -------------------------------------------


def test_produce_receipts_refuses_an_unpinned_record(ctx: MethodContext) -> None:
    """A DRAFT pins nothing a receipt could bind."""
    state_path = Path(str(ctx.state_path))
    record_release(state_path, dev1_draft(), recorded_at=NOW, summary="seed")

    message = refused(ctx)

    assert "release_not_pinned" in message
    assert not checkpoint_receipts_path(state_path).exists()


def test_produce_receipts_resolves_a_membership_rung_against_its_record(
    ctx: MethodContext,
) -> None:
    """dev3 requires membership, so its configuration takes the record's refs.

    Resolved without them the loader refuses the cardinality before any
    gate is looked at, so a dev3 checkpoint could never be receipted. An
    unpinned dev3 DRAFT therefore has to get as far as the pin check.
    """
    state_path = Path(str(ctx.state_path))
    draft = Release(
        uid=dev1_draft().uid,
        key="REL-0.7.0.dev3",
        version="0.7.0.dev3",
        channel=dev1_draft().channel,
        authority_epoch=2,
        membership_refs=("eawf://WSP-ABC/PRJ-ABC/REP-ABC/milestone/MLS-0001#MAB-0001-MLS-0001",),
    )
    record_release(state_path, draft, recorded_at=NOW, summary="seed")

    message = refused(ctx, version="0.7.0.dev3")

    assert "release_not_pinned" in message
    assert "membership_cardinality" not in message


def test_produce_receipts_refuses_a_checkpoint_with_no_stored_record(ctx: MethodContext) -> None:
    """Nothing stored for the rung means nothing to prove."""
    assert "no release record is stored" in refused(ctx)


def test_produce_receipts_refuses_a_version_off_the_train(ctx: MethodContext) -> None:
    """A version the ladder does not declare is refused by name."""
    assert "declares no checkpoint for '9.9.9'" in refused(ctx, version="9.9.9")


def test_produce_receipts_refuses_a_rung_with_no_configuration(ctx: MethodContext) -> None:
    """dev4 has no authored configuration yet, so it has no gates to prove."""
    assert "no release configuration" in refused(ctx, version="0.7.0.dev4")


def test_produce_receipts_refuses_without_a_state_root() -> None:
    """With nowhere to read the record or store the receipts, the verb refuses."""
    rootless = MethodContext(
        started_at="2026-09-17T00:00:00+00:00", pid=1, protocol_version="1", version="test"
    )

    assert "on-disk state root" in refused(rootless)


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_produce_receipts_rejects_a_window_that_is_not_positive(
    ctx: MethodContext, ttl_seconds: int
) -> None:
    """A receipt that is never fresh is refused at the params boundary."""
    with pytest.raises(ValidationError):
        produce(ctx, ttl_seconds=ttl_seconds)


# --- the library over the dev2 profile ---------------------------------------


def dev2_config() -> ReleaseConfig:
    """Return the rendered dev2 checkpoint configuration."""
    return load_release_config(DEV2_RELEASE_CONFIG_YAML, train=V07_TRAIN)


def dev2_record() -> Release:
    """Return the baked dev2 record pinned to the shared test commit."""
    return release_record(
        key="REL-0.7.0.dev2",
        version="0.7.0.dev2",
        status=ReleaseStatus.BAKED,
        approval_ref="receipt://approval/dev2",
    )


def sweep(
    *,
    red: Sequence[ReleaseSignalName] = (),
    observed_revision: str = SOURCE_SHA,
    waivers: Sequence[ReleaseWaiver] = (),
    acknowledgements: Sequence[WaiverAcknowledgement] = (),
    config: ReleaseConfig | None = None,
) -> ReleaseReadiness:
    """Return a dev2 sweep whose *red* signals fail and the rest pass."""

    def probe(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        if context.signal in red:
            return ReleaseSignalOutcome(
                status=ReleaseSignalStatus.FAIL, remediation=f"repair {context.signal.value}"
            )
        return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS)

    return compute_readiness(
        dev2_config() if config is None else config,
        probes=dict.fromkeys(ReleaseSignalName, probe),
        observed_revision=observed_revision,
        computed_at=NOW,
        waivers=waivers,
        acknowledgements=acknowledgements,
    )


class StubRunner:
    """A proof runner that passes every command and remembers what it ran."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.ran: list[ResolvedProofCommand] = []
        self._failing = failing

    def __call__(self, command: ResolvedProofCommand) -> ProofOutcome:
        """Record *command* and report it as passed unless listed as failing."""
        self.ran.append(command)
        passed = command.command_id not in self._failing
        return ProofOutcome(passed, NOW + timedelta(minutes=5), f"stub {command.command_id}")


def settle(
    readiness: ReleaseReadiness,
    runner: StubRunner | None = None,
    *,
    ttl_seconds: int = 3600,
    bindings: Mapping[ReleaseGateName, GateBinding] | None = None,
    release: Release | None = None,
) -> Any:
    """Produce dev2's receipts over *readiness* with a stub runner."""
    return produce_checkpoint_receipts(
        dev2_record() if release is None else release,
        required=dev2_config().gates.required,
        bindings=gate_bindings_for(ReleaseGateProfile.DEV2) if bindings is None else bindings,
        readiness=readiness,
        run_proof=StubRunner() if runner is None else runner,
        ttl_seconds=ttl_seconds,
    )


EXPLAINED = ReleaseWaiver(
    scope="P33-I01-W05", reason="the census ran on a partial corpus", protected_principal="strict"
)


def test_produce_checkpoint_receipts_settles_every_dev2_gate() -> None:
    """Twelve gates, twelve receipts: six rows, five proofs and the waiver block."""
    production = settle(sweep())

    assert production.refusals == ()
    assert [issued.receipt.gate for issued in production.receipts] == list(
        dev2_config().gates.required
    )
    assert {issued.receipt.source_sha for issued in production.receipts} == {SOURCE_SHA}
    assert {issued.receipt.manifest_digest for issued in production.receipts} == {MANIFEST_DIGEST}


def test_produce_checkpoint_receipts_pins_every_proof_to_the_record_source() -> None:
    """The runner is handed each authored proof, resolved to the pinned commit."""
    runner = StubRunner()

    settle(sweep(), runner)

    authored = [
        binding.proof.command_id
        for binding in gate_bindings_for(ReleaseGateProfile.DEV2).values()
        if binding.proof is not None
    ]
    assert [command.command_id for command in runner.ran] == authored
    assert {command.source_sha for command in runner.ran} == {SOURCE_SHA}
    assert runner.ran[0].argv == ("uv", "run", "pytest", "tests", "-q")


def test_produce_checkpoint_receipts_refuses_only_the_failing_proof() -> None:
    """A red proof loses its own receipt and nothing else."""
    production = settle(sweep(), StubRunner(failing=frozenset({"epoch2_strictness_census"})))

    assert [refusal.gate for refusal in production.refusals] == [ReleaseGateName.SCHEMA_STRICTNESS]
    assert len(production.receipts) == len(dev2_config().gates.required) - 1


def test_produce_checkpoint_receipts_refuses_an_unacknowledged_waiver_block() -> None:
    """An explained waiver nobody accepted holds the waiver gate back."""
    production = settle(sweep(waivers=(EXPLAINED,)))

    (refusal,) = production.refusals
    assert refusal.gate is ReleaseGateName.WAIVER_COUNT
    assert refusal.evidence_ref == "readiness:waivers"
    assert "P33-I01-W05/strict" in refusal.detail


def test_produce_checkpoint_receipts_passes_an_acknowledged_waiver_block() -> None:
    """Once the operator accepts the loss, the waiver gate earns its receipt."""
    acknowledged = WaiverAcknowledgement(
        scope=EXPLAINED.scope, protected_principal="strict", acknowledged_by="operator"
    )

    production = settle(sweep(waivers=(EXPLAINED,), acknowledgements=(acknowledged,)))

    assert production.refusals == ()
    assert ReleaseGateName.WAIVER_COUNT in {issued.receipt.gate for issued in production.receipts}


def test_produce_checkpoint_receipts_refuses_a_red_signal_gate() -> None:
    """A failing changelog row refuses the gate bound to it, naming the repair."""
    production = settle(sweep(red=(ReleaseSignalName.CHANGELOG,)))

    (refusal,) = production.refusals
    assert refusal.gate is ReleaseGateName.CHANGELOG_ENTRY
    assert "repair changelog" in refusal.detail


def test_produce_checkpoint_receipts_reads_component_gates_off_their_parent_row() -> None:
    """A red dependencies row refuses both gates reading one of its components."""
    production = settle(sweep(red=(ReleaseSignalName.DEPENDENCIES,)))

    assert {refusal.gate for refusal in production.refusals} == {
        ReleaseGateName.DEPENDENCY_INVENTORY,
        ReleaseGateName.SECURITY_REVIEW,
    }


def test_produce_checkpoint_receipts_refuses_rows_computed_at_other_source() -> None:
    """Rows swept at another commit prove nothing about the pin."""
    production = settle(sweep(observed_revision="d" * 40))

    refused_gates = {refusal.gate for refusal in production.refusals}
    assert refused_gates == {
        gate
        for gate, binding in gate_bindings_for(ReleaseGateProfile.DEV2).items()
        if binding.signal is not None
    }
    assert all("not the pinned source" in refusal.detail for refusal in production.refusals)


def test_produce_checkpoint_receipts_never_outlives_the_row_it_quotes() -> None:
    """A long window is capped at the row's expiry; a proof keeps the full window."""
    readiness = sweep()
    production = settle(readiness, ttl_seconds=30 * 24 * 3600)

    by_gate = {issued.receipt.gate: issued.receipt for issued in production.receipts}
    row = readiness.row(ReleaseSignalName.CHANGELOG)
    assert by_gate[ReleaseGateName.CHANGELOG_ENTRY].expires_at == row.expires_at
    proof = by_gate[ReleaseGateName.EPOCH1_STABILIZATION]
    assert proof.expires_at - proof.issued_at == timedelta(days=30)


def test_produce_checkpoint_receipts_issues_distinct_refs_per_gate() -> None:
    """Every receipt has its own ref, which is also its store id."""
    production = settle(sweep())

    refs = [issued.receipt.receipt_ref for issued in production.receipts]
    assert len(set(refs)) == len(refs)
    assert all(ref.startswith("checkpoint-receipt://REL-0.7.0.dev2/") for ref in refs)


def test_produce_checkpoint_receipts_refuses_an_unpinned_record() -> None:
    """A DRAFT carries nothing to bind."""
    with pytest.raises(ReceiptProductionError, match="release_not_pinned"):
        settle(sweep(config=dev1_config()), release=dev1_draft())


def test_produce_checkpoint_receipts_refuses_a_sweep_of_another_checkpoint() -> None:
    """dev1's sweep cannot settle dev2's gates."""
    with pytest.raises(ReceiptProductionError, match=r"not checkpoint REL-0\.7\.0\.dev2"):
        settle(sweep(config=dev1_config()))


def test_produce_checkpoint_receipts_refuses_a_required_gate_with_no_binding() -> None:
    """A table missing a required gate is refused rather than skipping the gate."""
    partial = dict(gate_bindings_for(ReleaseGateProfile.DEV2))
    del partial[ReleaseGateName.MIGRATION]

    with pytest.raises(ReceiptProductionError, match="'migration'"):
        settle(sweep(), bindings=partial)


@pytest.mark.parametrize("ttl_seconds", [0, -5])
def test_produce_checkpoint_receipts_rejects_a_window_that_is_not_positive(
    ttl_seconds: int,
) -> None:
    """A zero or negative window is refused before any gate is settled."""
    runner = StubRunner()

    with pytest.raises(ValueError, match="ttl_seconds must be positive"):
        settle(sweep(), runner, ttl_seconds=ttl_seconds)

    assert runner.ran == []


def test_proof_budget_seconds_sums_the_proof_timeouts_and_the_margin() -> None:
    """dev2's five proofs budget 3600 + 4 * 900 seconds, plus the margin."""
    budget = proof_budget_seconds(gate_bindings_for(ReleaseGateProfile.DEV2))

    assert budget == 3600 + 4 * 900 + PROOF_BUDGET_MARGIN_SECONDS


def test_pinned_worktree_refuses_a_command_pinned_elsewhere(pinned: tuple[Path, str]) -> None:
    """The runner is answerable for one commit and refuses a proof pinned to another."""
    repo, pinned_sha = pinned
    stray = ResolvedProofCommand(
        command_id="stray", argv=GREEN, source_sha="d" * 40, timeout_seconds=5
    )

    with pinned_worktree(repo, pinned_sha) as run_proof, pytest.raises(ValueError, match="pinned"):
        run_proof(stray)

    assert len(worktrees(repo)) == 1


def test_pinned_worktree_reports_a_command_that_cannot_start(pinned: tuple[Path, str]) -> None:
    """An argv whose head is not installed is a failed run, not a crash."""
    repo, pinned_sha = pinned
    missing = ResolvedProofCommand(
        command_id="missing",
        argv=("eawf-no-such-tool-anywhere",),
        source_sha=pinned_sha,
        timeout_seconds=5,
    )

    with pinned_worktree(repo, pinned_sha) as run_proof:
        outcome = run_proof(missing)

    assert outcome.passed is False
    assert "could not start" in outcome.detail


def test_pinned_worktree_refuses_a_directory_that_is_not_a_repository(tmp_path: Path) -> None:
    """Without a repository there is nothing to check out."""
    with (
        pytest.raises(ReceiptProductionError, match="cannot check out"),
        pinned_worktree(tmp_path, SOURCE_SHA),
    ):
        pass


# --- the CLI verb ------------------------------------------------------------


class _RecordingClient:
    """A ``DaemonClient`` stand-in that remembers how it was built and called."""

    def __init__(self, reply: dict[str, Any], seen: dict[str, Any], **kwargs: Any) -> None:
        self._reply = reply
        self._seen = seen
        seen["client_kwargs"] = kwargs

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record the call and answer the canned reply."""
        self._seen["method"] = method
        self._seen["params"] = params
        return self._reply


def use_client(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> dict[str, Any]:
    """Route the CLI's daemon calls into a recording stand-in answering *reply*."""
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda **kwargs: _RecordingClient(reply, seen, **kwargs),
    )
    return seen


def test_release_receipts_cli_waits_as_long_as_the_proofs_may_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call timeout is the profile's proof budget, not the 30 s default."""
    seen = use_client(monkeypatch, {"release_key": "REL-0.7.0.dev2", "receipts": [], "refused": []})

    result = CliRunner().invoke(app, ["release", "receipts", "0.7.0.dev2", "--ttl-seconds", "600"])

    assert result.exit_code == 0, result.output
    assert seen["method"] == "release.produce_receipts"
    assert seen["params"] == {"version": "0.7.0.dev2", "waiver_count": 0, "ttl_seconds": 600}
    assert seen["client_kwargs"] == {
        "call_timeout_seconds": float(
            proof_budget_seconds(gate_bindings_for(ReleaseGateProfile.DEV2))
        )
    }


def test_release_receipts_cli_exits_non_zero_naming_each_refused_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused gate is printed and turns the exit code red."""
    use_client(
        monkeypatch,
        {
            "release_key": "REL-0.7.0.dev2",
            "source_sha": SOURCE_SHA,
            "receipts": [
                {"gate": "migration", "receipt_ref": "checkpoint-receipt://x", "expires_at": "t"}
            ],
            "refused": [{"gate": "schema_strictness", "detail": "proof exited 1"}],
        },
    )

    result = CliRunner().invoke(app, ["release", "receipts", "0.7.0.dev2"])

    assert result.exit_code == exit_codes.STATE_CONFLICT
    assert "pass migration" in result.output
    assert "FAIL schema_strictness" in result.output
    assert "proof exited 1" in result.output


def test_release_receipts_cli_refuses_a_version_off_the_train(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version with no rung is refused before the daemon is asked."""
    seen = use_client(monkeypatch, {})

    result = CliRunner().invoke(app, ["release", "receipts", "9.9.9"])

    assert result.exit_code != 0
    assert "cannot prove the gates of '9.9.9'" in result.output
    assert "method" not in seen


def test_release_receipts_cli_forwards_the_waiver_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--waivers`` arrives as the decoded rows the waiver gate reads."""
    seen = use_client(monkeypatch, {"refused": []})
    waivers = tmp_path / "waivers.json"
    waivers.write_text('{"waivers": [{"scope": "W05"}]}', encoding="utf-8")

    result = CliRunner().invoke(
        app, ["release", "receipts", "0.7.0.dev2", "--waivers", str(waivers)]
    )

    assert result.exit_code == 0, result.output
    assert seen["params"]["waivers"] == [{"scope": "W05"}]
