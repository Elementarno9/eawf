"""Issue one checkpoint gate receipt per required gate of a checkpoint.

A train walks past a checkpoint only on receipts that bind the
checkpoint's exact source
(:func:`~eawf.workflow.release.advance.assert_prerequisite_receipts`).
This module issues them. How each gate is settled is read from the
profile's gate binding table, so a later profile's gates are data rather
than code:

* a **signal** or **signal component** gate is settled by the readiness
  row it is bound to. The row must pass, and it must have been computed
  at the pinned source;
* a **proof command** gate is settled by running its argv, pinned
  through :meth:`~eawf.kernel.release.gate_binding.GateBinding.resolve_proof`,
  in a git worktree checked out at the record's ``source_sha``. The
  command never runs in the working copy, whose HEAD may have moved on
  since the checkpoint was cut;
* the **waiver block** gate reads the sweep's waiver block itself rather
  than a gate row derived from it.

A gate that does not pass earns no receipt, and its refusal names what
it read. The gates that did pass still get their receipts. Each receipt
proves one gate, so fixing one failing command should not mean rerunning
the hour-long suite beside it.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Final

from eawf.kernel.release.gate_binding import (
    GateBinding,
    GateEvidenceKind,
    ResolvedProofCommand,
)
from eawf.kernel.release.signals import ReleaseSignalStatus
from eawf.kernel.spec.release import Release
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.advance import CheckpointGateReceipt
from eawf.workflow.verify.release_readiness import ReleaseReadiness

logger = logging.getLogger(__name__)

#: How long an issued receipt stays fresh when the caller names no
#: window. A day outlasts the longest proof command and the advance
#: that follows it, and it still stops an advance from leaning on last
#: week's run.
DEFAULT_RECEIPT_TTL_SECONDS: Final[int] = 24 * 3600

#: Seconds a caller waiting on the producer allows beyond the summed
#: proof budgets, for the sweep and the worktree checkout around them.
PROOF_BUDGET_MARGIN_SECONDS: Final[int] = 300

#: Wall-clock ceiling on one git invocation that manages the worktree.
_GIT_TIMEOUT_SECONDS: Final[int] = 300

#: How much of each output stream of a failing proof command its refusal
#: quotes. Each stream gets its own tail because a tool's summary and its
#: warnings can land on different streams, and one stream's tail would
#: hide the other's.
_OUTPUT_TAIL_CHARS: Final[int] = 200


class ReceiptProductionError(ValueError):
    """No receipt can be produced for the checkpoint at all.

    Raised for problems with the checkpoint as a whole, not with one
    gate: a record with no pinned source, a sweep of another checkpoint,
    a required gate the binding table does not bind, or a source commit
    that cannot be checked out.
    """


@dataclass(frozen=True, slots=True)
class ProofOutcome:
    """What one proof command run established.

    Attributes:
        passed: Whether the command exited zero inside its budget.
        finished_at: When the run ended (timezone-aware UTC).
        detail: One line naming the command, its exit and the revision.
            A failed run's detail ends with the tail of its output.
    """

    passed: bool
    finished_at: datetime
    detail: str


#: Runs one pinned proof command and reports what it established.
ProofRunner = Callable[[ResolvedProofCommand], ProofOutcome]


@dataclass(frozen=True, slots=True)
class IssuedReceipt:
    """One receipt the producer issued, with what settled its gate.

    Attributes:
        receipt: The receipt.
        evidence: One line naming the evidence that settled the gate,
            kept beside the stored row.
    """

    receipt: CheckpointGateReceipt
    evidence: str


@dataclass(frozen=True, slots=True)
class GateRefusal:
    """A required gate that earned no receipt.

    Attributes:
        gate: The gate.
        evidence_ref: The evidence it is bound to, per
            :attr:`~eawf.kernel.release.gate_binding.GateBinding.evidence_ref`.
        detail: Why the evidence did not settle it.
    """

    gate: ReleaseGateName
    evidence_ref: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReceiptProduction:
    """The outcome of one producer run over a checkpoint.

    Attributes:
        receipts: One issued receipt per gate that passed, in the
            configuration's gate order.
        refusals: One refusal per gate that did not, in the same order.
    """

    receipts: tuple[IssuedReceipt, ...]
    refusals: tuple[GateRefusal, ...]


@dataclass(frozen=True, slots=True)
class _Verdict:
    """How one gate settled, before a receipt is built from it."""

    passed: bool
    settled_at: datetime
    expires_at: datetime
    detail: str


def pinned_source(release: Release) -> tuple[str, str]:
    """Return the source commit and manifest digest *release* pins.

    Args:
        release: The checkpoint record receipts are produced for.

    Returns:
        ``(source_sha, manifest_digest)``.

    Raises:
        ReceiptProductionError: When either is unset. A receipt binds
            both, so a record that has not pinned them has nothing a
            receipt could bind.
    """
    if release.source_sha is None or release.manifest_digest is None:
        raise ReceiptProductionError(
            f"release_not_pinned: {release.key} stands at {release.status.value!r} with no "
            f"pinned source and manifest; gate receipts bind both, so pin the candidate first"
        )
    return release.source_sha, release.manifest_digest


def proof_budget_seconds(bindings: Mapping[ReleaseGateName, GateBinding]) -> int:
    """Return how long a caller should wait for the producer over *bindings*.

    Args:
        bindings: A profile's loaded binding table.

    Returns:
        The summed proof-command budgets plus
        :data:`PROOF_BUDGET_MARGIN_SECONDS`.
    """
    commands = sum(
        binding.proof.timeout_seconds for binding in bindings.values() if binding.proof is not None
    )
    return commands + PROOF_BUDGET_MARGIN_SECONDS


def receipt_ref(release_key: str, gate: ReleaseGateName, issued_at: datetime) -> str:
    """Return the reference, and store id, of one issued receipt.

    Args:
        release_key: ``REL-<version>`` the receipt is for.
        gate: The gate it settles.
        issued_at: When it was issued; distinguishes reruns.

    Returns:
        ``checkpoint-receipt://<key>/<gate>/<UTC stamp>``.
    """
    stamp = issued_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"checkpoint-receipt://{release_key}/{gate.value}/{stamp}"


def _settle_signal(
    binding: GateBinding,
    readiness: ReleaseReadiness,
    *,
    source_sha: str,
    ttl: timedelta,
) -> _Verdict:
    """Return the verdict of a gate bound to a readiness row.

    A component gate reads its parent row, because the sweep computes one
    verdict per row. The receipt never outlives the row it quotes.

    Args:
        binding: The gate's binding; its ``signal`` names the row.
        readiness: The sweep of the pinned source.
        source_sha: The commit the row must have been computed at.
        ttl: The receipt window the caller asked for.

    Returns:
        The verdict.
    """
    assert binding.signal is not None
    row = readiness.row(binding.signal)
    expires_at = min(row.expires_at, row.computed_at + ttl)
    if row.observed_revision != source_sha:
        detail = (
            f"{binding.evidence_ref} was computed at {row.observed_revision!r}, not the "
            f"pinned source {source_sha}"
        )
        return _Verdict(False, row.computed_at, expires_at, detail)
    if row.status is not ReleaseSignalStatus.PASS:
        detail = f"{binding.evidence_ref} is {row.status.value!r}: {row.remediation}"
        return _Verdict(False, row.computed_at, expires_at, detail)
    detail = f"{binding.evidence_ref} passed at {source_sha}"
    return _Verdict(True, row.computed_at, expires_at, detail)


def _settle_waiver_block(readiness: ReleaseReadiness, *, ttl: timedelta) -> _Verdict:
    """Return the verdict of the gate bound to the waiver block.

    Args:
        readiness: The sweep whose waiver block is read.
        ttl: The receipt window the caller asked for.

    Returns:
        Passing when the block leaves the checkpoint approvable.
    """
    counted = readiness.waiver_count
    disposition = readiness.waiver_disposition.value
    settled_at = readiness.computed_at
    if readiness.waivers_cleared:
        detail = f"waiver block cleared: {counted} counted waiver(s), disposition {disposition!r}"
        return _Verdict(True, settled_at, settled_at + ttl, detail)
    outstanding = ", ".join(
        f"{waiver.scope}/{waiver.protected_principal}"
        for waiver in readiness.unacknowledged_waivers
    )
    detail = (
        f"waiver block not cleared: {counted} counted waiver(s), disposition "
        f"{disposition!r}, unacknowledged [{outstanding}]"
    )
    return _Verdict(False, settled_at, settled_at + ttl, detail)


def _settle_proof(
    binding: GateBinding,
    *,
    source_sha: str,
    run_proof: ProofRunner,
    ttl: timedelta,
) -> _Verdict:
    """Return the verdict of a gate settled by its proof command.

    Args:
        binding: The gate's binding; its ``proof`` is the command.
        source_sha: The commit the command is pinned to.
        run_proof: Runs the pinned command.
        ttl: The receipt window the caller asked for.

    Returns:
        The verdict, issued at the moment the run ended.
    """
    outcome = run_proof(binding.resolve_proof(source_sha))
    return _Verdict(outcome.passed, outcome.finished_at, outcome.finished_at + ttl, outcome.detail)


def produce_checkpoint_receipts(
    release: Release,
    *,
    required: Sequence[ReleaseGateName],
    bindings: Mapping[ReleaseGateName, GateBinding],
    readiness: ReleaseReadiness,
    run_proof: ProofRunner,
    ttl_seconds: int,
) -> ReceiptProduction:
    """Settle every required gate of *release* and issue a receipt for each pass.

    Args:
        release: The checkpoint record; its pinned source and manifest
            are what every receipt binds.
        required: The configuration's ``gates.required`` list.
        bindings: The profile's loaded binding table.
        readiness: The sweep of the pinned source that the signal gates
            and the waiver-block gate read.
        run_proof: Runs one pinned proof command, normally in a worktree
            at the pinned source (:func:`pinned_worktree`).
        ttl_seconds: How long an issued receipt stays fresh.

    Returns:
        The receipts issued and the gates refused.

    Raises:
        ReceiptProductionError: When the record is unpinned, the sweep
            belongs to another checkpoint, or a required gate has no
            binding.
        ValueError: When *ttl_seconds* is not positive.
    """
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    source_sha, manifest_digest = pinned_source(release)
    if readiness.release_key != release.key:
        raise ReceiptProductionError(
            f"readiness sweeps {readiness.release_key}, not checkpoint {release.key}"
        )
    ttl = timedelta(seconds=ttl_seconds)
    issued: list[IssuedReceipt] = []
    refused: list[GateRefusal] = []
    for gate in required:
        binding = bindings.get(gate)
        if binding is None:
            raise ReceiptProductionError(
                f"gate {gate.value!r} of {release.key} has no binding in its profile table"
            )
        if binding.kind is GateEvidenceKind.PROOF_COMMAND:
            verdict = _settle_proof(binding, source_sha=source_sha, run_proof=run_proof, ttl=ttl)
        elif binding.kind is GateEvidenceKind.WAIVER_BLOCK:
            verdict = _settle_waiver_block(readiness, ttl=ttl)
        else:
            verdict = _settle_signal(binding, readiness, source_sha=source_sha, ttl=ttl)
        if not verdict.passed:
            refused.append(GateRefusal(gate, binding.evidence_ref, verdict.detail))
            continue
        receipt = CheckpointGateReceipt(
            gate=gate,
            release_key=release.key,
            source_sha=source_sha,
            manifest_digest=manifest_digest,
            issued_at=verdict.settled_at,
            expires_at=verdict.expires_at,
            receipt_ref=receipt_ref(release.key, gate, verdict.settled_at),
        )
        issued.append(IssuedReceipt(receipt, verdict.detail))
    logger.info(
        f"produce_checkpoint_receipts key={release.key!r} issued={len(issued)} "
        f"refused={[refusal.gate.value for refusal in refused]}"
    )
    return ReceiptProduction(receipts=tuple(issued), refusals=tuple(refused))


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command against *repo_root*; a non-zero exit is returned, not raised."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _output_tail(completed: subprocess.CompletedProcess[str]) -> str:
    """Return the last characters of each stream a failed command wrote, on one line."""
    tails = []
    for name, text in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        flat = " ".join(text.split())
        if flat:
            tails.append(f"{name}: {flat[-_OUTPUT_TAIL_CHARS:]}")
    return " | ".join(tails) or "no output"


def _run_in_tree(tree: Path, source_sha: str, command: ResolvedProofCommand) -> ProofOutcome:
    """Run *command* with *tree* as its working directory.

    Args:
        tree: A worktree checked out at *source_sha*.
        source_sha: The commit *tree* holds.
        command: The pinned proof command.

    Returns:
        What the run established. A timeout or a command that cannot be
        started is a failed run, not an exception: the gate simply earns
        no receipt.

    Raises:
        ValueError: When *command* is pinned to a commit other than the
            one *tree* holds. A run there would prove nothing about the
            pin.
    """
    if command.source_sha != source_sha:
        raise ValueError(
            f"proof {command.command_id!r} is pinned to {command.source_sha}, but the "
            f"worktree holds {source_sha}"
        )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command.argv),
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
            timeout=command.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        detail = f"proof {command.command_id} timed out after {command.timeout_seconds}s"
        return ProofOutcome(False, datetime.now(UTC), f"{detail} at {source_sha}")
    except OSError as exc:
        detail = f"proof {command.command_id} could not start: {exc}"
        return ProofOutcome(False, datetime.now(UTC), f"{detail} at {source_sha}")
    elapsed = time.monotonic() - started
    exit_line = (
        f"proof {command.command_id} exited {completed.returncode} after {elapsed:.0f}s "
        f"at {source_sha}"
    )
    if completed.returncode == 0:
        return ProofOutcome(True, datetime.now(UTC), exit_line)
    logger.warning(
        f"proof_command_failed command_id={command.command_id!r} rc={completed.returncode}"
    )
    return ProofOutcome(False, datetime.now(UTC), f"{exit_line}: {_output_tail(completed)}")


@contextmanager
def pinned_worktree(repo_root: Path, source_sha: str) -> Iterator[ProofRunner]:
    """Yield a runner whose proof commands run in a worktree at *source_sha*.

    The worktree lives in a scratch directory outside the checkout and is
    removed on exit, whether the commands passed or not.

    Args:
        repo_root: The checkout whose repository holds *source_sha*.
        source_sha: The commit to check out.

    Yields:
        A :data:`ProofRunner` bound to the worktree.

    Raises:
        ReceiptProductionError: When git cannot check *source_sha* out.
    """
    with tempfile.TemporaryDirectory(prefix="eawf-proof-") as scratch:
        tree = Path(scratch) / "tree"
        added = _git(repo_root, "worktree", "add", "--detach", str(tree), source_sha)
        if added.returncode != 0:
            raise ReceiptProductionError(
                f"cannot check out {source_sha} into a proof worktree: {added.stderr.strip()}"
            )
        logger.info(f"pinned_worktree_added source_sha={source_sha!r}")
        try:
            yield partial(_run_in_tree, tree, source_sha)
        finally:
            removed = _git(repo_root, "worktree", "remove", "--force", str(tree))
            if removed.returncode != 0:
                logger.warning(
                    f"pinned_worktree_remove_failed source_sha={source_sha!r} "
                    f"detail={removed.stderr.strip()!r}"
                )


__all__ = [
    "DEFAULT_RECEIPT_TTL_SECONDS",
    "PROOF_BUDGET_MARGIN_SECONDS",
    "GateRefusal",
    "IssuedReceipt",
    "ProofOutcome",
    "ProofRunner",
    "ReceiptProduction",
    "ReceiptProductionError",
    "pinned_source",
    "pinned_worktree",
    "produce_checkpoint_receipts",
    "proof_budget_seconds",
    "receipt_ref",
]
