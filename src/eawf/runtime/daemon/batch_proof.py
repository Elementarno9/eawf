"""Proving a landed Batch's criteria at the head it closes on, before the close walks it.

A Batch closing on landing completes its ready Tasks, so before it moves the daemon
proves each criterion that names a pytest verify command -- a ``command_exit_zero``
response observed by pytest whose measurable signal carries ``uv run pytest`` and test
paths in backticks -- at the landed head, and files one proof receipt per criterion in
the receipt ledger ``task prove`` files to. A measurable signal is prose, so nothing
else in it is ever run: a criterion naming any other command, a placeholder or no
command at all is answered by the Task's bound evidence, as before. A test path the
head does not hold leaves the criterion unmet without running anything.

A proof never runs on a request's path. The close reads the receipts already filed: a
passing one at the same freshness key is reused, so a criterion is run once per tree,
and a failing one keeps the Batch open naming the criterion. Only when some criterion
has no receipt does the close start a background proof and answer that the Batch is
proving; the proof runs every such criterion at once, each under
:data:`PROOF_BUDGET_SECONDS`, in a detached checkout of the head so the live working
tree never matters. The checkout and its environment are built once per Batch head and
kept for every later proof of that head, until the Batch closes, its head moves or it
stops being proof-eligible; a checkout a stopped daemon left behind is removed when the
next one starts. When a proof settles it wakes the daemon's sweep, which closes the
Batch on the receipts it filed. A criterion whose gate crashed files a failing receipt
too, and a proof that failed as a whole is remembered against its head, so neither is
run again on every sweep.

What holds a Batch -- proving, or a criterion unmet -- is announced on the daemon's bus
whenever it changes, so a console holding the Batch shows it as it happens.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from eawf.kernel.delivery.receipts import (
    ProofReceipt,
    RevisionBinding,
    RevisionRefKind,
    canonical_digest,
)
from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.store.envelope import Envelope
from eawf.platform.process_liveness import pid_alive
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.batch_close_sweep import SWEEP_WAKE
from eawf.runtime.daemon.batch_proof_notice import BatchProofNotice
from eawf.runtime.daemon.epoch2_recovery import publish_projection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.gate_execution import GateExecutionContext
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_completion import filed_proofs
from eawf.runtime.daemon.methods.delivery_proof import daemon_proof_facts, file_proofs, run_leg
from eawf.runtime.verification.receipts import VerificationLeg, build_verification_leg
from eawf.workflow.audit_dsl.models import CheckResult
from eawf.workflow.delivery.criteria import CriteriaAuthoring, compile_execution_contracts

logger = logging.getLogger(__name__)

#: How long one criterion's command may run before it counts as unmet, over budget.
PROOF_BUDGET_SECONDS: Final = 300

#: How many criteria one proof runs at once.
PROVERS: Final = max(1, min(os.cpu_count() or 1, 8))

#: The response gate whose criterion names a command to run.
VERIFY_GATE_KIND: Final = "command_exit_zero"

#: The command a measurable signal names: its first backticked span.
_COMMAND: Final = re.compile(r"`([^`]+)`")

#: The command heads a verify command may start with: pytest through the project runner.
_PYTEST_HEADS: Final = (("uv", "run", "pytest"), ("uv", "run", "python", "-m", "pytest"))

#: The pytest flags a verify command may carry. An allowlist, since other options load
#: plugins, read another config or remove a directory.
_FLAGS: Final = frozenset(
    {"-q", "-qq", "-v", "-vv", "-x", "-s", "-l", "-ra", "-rA", "--no-header", "--lf", "--ff"}
)

#: The pytest options that take a value, as the next word or after ``=``, and its shape.
_VALUED_OPTIONS: Final = frozenset({"-k", "-m", "-n", "--maxfail", "--tb", "--durations"})
_OPTION_VALUE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ .,:-]*")

#: A test path under ``tests/`` with an optional node id.
_TEST_PATH: Final = re.compile(r"tests/[A-Za-z0-9_./-]+(?:::[A-Za-z0-9_\[\]:.-]+)?")

#: The gate id a criterion that references none is proved under.
_DEFAULT_GATE_ID: Final = "G-01"

#: What a close reads off a proof.
ProofState = Literal["proving", "proved", "unmet", "over budget", "crashed"]

_LOCK: Final = threading.Lock()
_RUNNING: Final[dict[tuple[Path, str], threading.Thread]] = {}
_NOTES: Final[dict[tuple[Path, str], str]] = {}
#: The proofs that failed as a whole, by Batch: the head each failed at and why.
_FAILED: Final[dict[tuple[Path, str], tuple[str, str]]] = {}

#: The file in a checkout's scratch directory naming the daemon process that owns it.
_OWNER: Final = "owner"


@dataclass(frozen=True, slots=True)
class _Checkout:
    """The detached checkout one Batch head is proved in, under a scratch directory."""

    head: str
    scratch: Path

    @property
    def cwd(self) -> Path:
        """Return the checkout's working tree."""
        return self.scratch / "checkout"


_CHECKOUTS: Final[dict[tuple[Path, str], _Checkout]] = {}


@dataclass(frozen=True, slots=True)
class ProofOutcome:
    """Where a Batch's proof stands at one close attempt.

    Attributes:
        state: ``None`` when no criterion names a command, else how the proof stands.
        reason: The sentence a Batch kept open carries.
        citations: The receipt URNs each Task's criteria passed on, by Task key.
    """

    state: ProofState | None
    reason: str = ""
    citations: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Leg:
    """One criterion of one Task, compiled to the leg it is proved as."""

    task: Task
    criterion_id: str
    leg: VerificationLeg


def _is_pytest(argv: Sequence[str]) -> bool:
    """Return whether *argv* is pytest over test paths and options, and nothing else."""
    head = next((head for head in _PYTEST_HEADS if tuple(argv[: len(head)]) == head), None)
    if head is None:
        return False
    rest = argv[len(head) :]
    index = 0
    while index < len(rest):
        word = rest[index]
        name, joined, value = word.partition("=")
        if word in _VALUED_OPTIONS:
            index += 1
            if index == len(rest) or not _OPTION_VALUE.fullmatch(rest[index]):
                return False
        elif joined and name in _VALUED_OPTIONS:
            if not _OPTION_VALUE.fullmatch(value):
                return False
        elif word not in _FLAGS and (
            not _TEST_PATH.fullmatch(word) or ".." in word.split("::")[0].split("/")
        ):
            return False
        index += 1
    return True


def verify_argv(criterion: CriterionSpec) -> list[str] | None:
    """Return the pytest command *criterion* is proved by, or ``None`` when it names none.

    Only ``uv run pytest`` (or ``uv run python -m pytest``) over options and paths under
    ``tests/`` counts, on a ``command_exit_zero`` response observed by pytest; any other
    backticked span is prose, a placeholder or an arbitrary command and is never run.
    """
    response = criterion.response
    if response is None or response.gate_ref != VERIFY_GATE_KIND:
        return None
    if response.locus is not None and response.locus != "pytest":
        return None
    found = _COMMAND.search(criterion.measurable_signal)
    if found is None:
        return None
    try:
        argv = shlex.split(found.group(1))
    except ValueError:
        return None
    return argv if _is_pytest(argv) else None


def _missing_path(repository: Path, head: str, argv: Sequence[str]) -> str | None:
    """Return the first test path *argv* names that *head* does not hold, if any."""
    for word in argv:
        if not word.startswith("tests/"):
            continue
        path = word.split("::", 1)[0]
        if not _git_quietly(repository, "cat-file", "-e", f"{head}:{path.rstrip('/')}"):
            return path
    return None


def proof_notes(tree_root: Path) -> dict[str, str]:
    """Return why each Batch of *tree_root* is held by its proof, by Batch URN."""
    root = tree_root.resolve()
    with _LOCK:
        return {urn: note for (where, urn), note in _NOTES.items() if where == root}


def _binding(
    batch: DeliveryBatch,
    criterion: CriterionSpec,
    *,
    head: str,
    tree: str,
    parent: str | None,
    now: datetime,
) -> RevisionBinding:
    """Return the revision a criterion is proved at: the landed head of the Batch's branch."""
    return RevisionBinding(
        repository_ref=batch.repository_ref,
        ref_kind=RevisionRefKind.TARGET_BRANCH,
        head_sha=head,
        tree_sha=tree,
        parent_sha=parent,
        batch_ref=batch.urn,
        integration_generation=1,
        manifest_digest=canonical_digest({"tree": tree}),
        criteria_digest=canonical_digest(criterion.model_dump(mode="json")),
        policy_digest=canonical_digest({"budget_seconds": PROOF_BUDGET_SECONDS}),
        environment_digest=None,
        bound_at=now,
    )


def _legs(
    repository: Path,
    batch: DeliveryBatch,
    tasks: Sequence[Task],
    *,
    head: str,
    tree: str,
    parent: str | None,
    now: datetime,
) -> tuple[list[_Leg], list[tuple[str, str]]]:
    """Return every criterion that names a command, as a leg, beside those that cannot run.

    A criterion whose test path the head does not hold, or whose command the gate argv
    policy refuses or that does not compile against its gate, cannot be proved, so it
    counts as unmet; each is returned beside why.
    """
    legs: list[_Leg] = []
    refused: list[tuple[str, str]] = []
    for task in tasks:
        for criterion in task.criteria:
            # a waived criterion is satisfied by its waiver and never runs its gate
            argv = None if criterion.waiver_reason is not None else verify_argv(criterion)
            if argv is None:
                continue
            missing = _missing_path(repository, head, argv)
            if missing is not None:
                refused.append((criterion.id, f"missing test path {missing}"))
                continue
            try:
                gate_id = criterion.gate_ids[0] if criterion.gate_ids else _DEFAULT_GATE_ID
                gate = GateSpec(
                    id=gate_id,
                    criterion_id=criterion.id,
                    kind=VERIFY_GATE_KIND,
                    args={"argv": argv},
                    policy="block",
                    cadence="manual",
                    timeout_s=PROOF_BUDGET_SECONDS,
                )
                authored = criterion.model_copy(update={"gate_ids": [gate_id]})
                contracts = compile_execution_contracts(
                    CriteriaAuthoring(scope_id=task.key, criteria=(authored,), gates=(gate,))
                )
            except ValueError as error:
                logger.warning(f"batch_proof refused criterion={criterion.id} error={error!s}")
                refused.append((criterion.id, "command refused"))
                continue
            binding = _binding(batch, criterion, head=head, tree=tree, parent=parent, now=now)
            facts = daemon_proof_facts(contracts)
            for contract in contracts.contracts:
                leg = build_verification_leg(contract, revision_binding=binding, facts=facts)
                legs.append(_Leg(task=task, criterion_id=criterion.id, leg=leg))
    return legs, refused


def _shortfall(receipt: ProofReceipt) -> ProofState:
    """Return how a failing receipt fell short: over its budget, crashed, or plainly unmet.

    A command that exited carries its status; one that did not either ran out its
    budget or never ran to an exit at all.
    """
    if receipt.exit_status is not None:
        return "unmet"
    spent = receipt.ended_at - receipt.started_at
    return "over budget" if spent >= timedelta(seconds=PROOF_BUDGET_SECONDS) else "crashed"


def _receipt_urn(batch: DeliveryBatch, receipt_id: str) -> str:
    """Return the URN a Task's completion cites one receipt by."""
    return format_qualified_urn(
        workspace_key=batch.urn.workspace_key,
        project_key=batch.urn.project_key,
        repository_key=batch.urn.repository_key,
        kind=EntityKind.RECEIPT,
        entity_key=receipt_id,
    )


def _note(context: Epoch2RootContext, key: tuple[Path, str], outcome: ProofOutcome) -> ProofOutcome:
    """Keep the console's note of *outcome* for the Batch *key* names, and return it.

    A note that changed is announced on the tree's bus, so a console holding the Batch
    reads it again at once rather than on its next refresh.
    """
    note = None if outcome.state in {None, "proved"} else outcome.reason
    with _LOCK:
        before = _NOTES.pop(key, None)
        if note is not None:
            _NOTES[key] = note
    if note != before:
        _announce(context, key[1], note)
    return outcome


def _announce(context: Epoch2RootContext, batch_ref: str, note: str | None) -> None:
    """Publish that the proof holding *batch_ref* moved, never failing the proof on it."""
    # imported here: the Run verbs import the supervisor, which closes Batches through here
    from eawf.runtime.daemon.methods.run import event_bus_of

    notice = BatchProofNotice.model_validate({"batch_ref": batch_ref, "note": note})
    bus: Any = event_bus_of(context)
    publish_projection(
        bus,
        Envelope(
            id=f"batch-proof-noted-{uuid.uuid4().hex}",
            kind=StoreKind.BATCH_PROOF_NOTED,
            scope_id=batch_ref,
            created_at=datetime.now(UTC),
            summary=f"batch_proof_noted batch={notice.batch_ref.entity_key} note={note!r}",
            payload=notice.model_dump(mode="json"),
        ),
    )


def judge_proof(
    context: Epoch2RootContext,
    batch: DeliveryBatch,
    tasks: Sequence[Task],
    *,
    head: str,
    tree: str,
    parent: str | None,
    rerun_failed: bool,
    now: datetime,
) -> ProofOutcome:
    """Return whether the ready *tasks* of *batch* are proved at *head*, starting a proof if not.

    Args:
        context: The native context of the tree the Batch lives in.
        batch: The Batch being closed.
        tasks: Its ready Tasks.
        head: The landed head the Batch closes on.
        tree: That head's tree.
        parent: That head's first parent, or ``None`` for a root commit.
        rerun_failed: Run a criterion again whose receipt at this key failed, as an
            operator's ask does; otherwise that receipt keeps the Batch open.
        now: When the proof is bound.

    Returns:
        How the proof stands; a Batch moves only on ``None`` or ``proved``.
    """
    key = (context.identity.tree_root.resolve(), str(batch.urn))
    short = head[:12]
    with _LOCK:
        running = key in _RUNNING
        standing_note = _NOTES.get(key)
        failed = _FAILED.get(key)
    if running:
        reason = standing_note or f"proving criteria at {short}"
        return _note(context, key, ProofOutcome(state="proving", reason=reason))
    if failed is not None and failed[0] == head and not rerun_failed:
        return _note(context, key, ProofOutcome(state="unmet", reason=f"proof failed: {failed[1]}"))
    repository = context.identity.tree_root.parent
    legs, refused = _legs(repository, batch, tasks, head=head, tree=tree, parent=parent, now=now)
    if not legs and not refused:
        return _note(context, key, ProofOutcome(state=None))
    with context.session([str(batch.urn)]) as session:
        standing = {
            item.receipt.freshness_key: item.receipt
            for task in {leg.task.key: leg.task for leg in legs}.values()
            for item in filed_proofs(session, task.urn)
        }
    shortfalls: list[tuple[str, ProofState, str]] = [
        (criterion, "unmet", f": {why}") for criterion, why in refused
    ]
    citations: dict[str, list[str]] = {}
    pending: list[_Leg] = []
    for item in legs:
        receipt = standing.get(item.leg.expected.digest())
        if receipt is not None and receipt.result is GateReceiptResult.PASS:
            cited = citations.setdefault(item.task.key, [])
            cited.append(_receipt_urn(batch, receipt.id))
        elif receipt is not None and not rerun_failed:
            shortfalls.append((item.criterion_id, _shortfall(receipt), ""))
        else:
            pending.append(item)
    if shortfalls:
        criterion, state, why = sorted(shortfalls)[0]
        reason = f"{criterion} {state} at {short}{why}"
        return _note(context, key, ProofOutcome(state=state, reason=reason))
    if pending:
        _start(context, key, pending, head=head)
        reason = f"proving {len(pending)} criteria at {short}"
        return _note(context, key, ProofOutcome(state="proving", reason=reason))
    proved = {task: tuple(urns) for task, urns in citations.items()}
    return _note(context, key, ProofOutcome(state="proved", citations=proved))


def _start(
    context: Epoch2RootContext, key: tuple[Path, str], legs: Sequence[_Leg], *, head: str
) -> None:
    """Prove *legs* on a background thread, unless a proof of the Batch already runs."""
    with _LOCK:
        if key in _RUNNING:
            return
        _FAILED.pop(key, None)
        thread = threading.Thread(
            target=_prove, args=(context, key, tuple(legs), head), name="batch-proof", daemon=True
        )
        _RUNNING[key] = thread
    logger.info(f"batch_proof started batch={key[1]} head={head} criteria={len(legs)}")
    thread.start()


def _attempt(item: _Leg, *, cwd: Path, gate_context: GateExecutionContext) -> CheckResult:
    """Run one leg; a gate that crashed comes back failed with no exit status, not raised.

    A crash is a result like any other, so its siblings still file and its receipt keeps
    the sweep from running it again until an operator asks.
    """
    started = datetime.now(UTC)
    try:
        return run_leg(item.leg, cwd=cwd, gate_context=gate_context)
    except (DaemonValidationError, OSError) as error:
        logger.warning(f"batch_proof crashed criterion={item.criterion_id} error={error!s}")
        return CheckResult(
            name=item.leg.contract.gate_id,
            kind=VERIFY_GATE_KIND,
            passed=False,
            status="blocked",
            details=f"crashed: {error!s}"[:500],
            exit_status=None,
            started_at=started,
            ended_at=datetime.now(UTC),
        )


def _git_quietly(repository: Path, *args: str) -> bool:
    """Run one git command in *repository* and return whether it exited zero."""
    done = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        check=False,
        **no_window_kwargs(),
    )
    return done.returncode == 0


def _remove(repository: Path, held: _Checkout) -> None:
    """Remove one checkout, worktree registration and scratch directory both."""
    _git_quietly(repository, "worktree", "remove", "--force", str(held.cwd))
    shutil.rmtree(held.scratch, ignore_errors=True)


def _prepare(cwd: Path) -> None:
    """Build the checkout's environment once, so its criteria do not each build it.

    A uv project is synced from its lockfile, with the operator's cache; any other
    checkout is left as it is. A failed sync is logged rather than raised: a criterion
    that needs the environment then fails on its own and names itself.
    """
    uv = shutil.which("uv")
    if uv is None or not (cwd / "uv.lock").is_file():
        return
    env = {name: value for name, value in os.environ.items() if name != "VIRTUAL_ENV"}
    started = time.monotonic()
    try:
        done = subprocess.run(
            [uv, "sync", "--frozen", "--quiet"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=PROOF_BUDGET_SECONDS,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"batch_proof sync timed out cwd={cwd}")
        return
    took = time.monotonic() - started
    if done.returncode != 0:
        logger.warning(f"batch_proof sync failed took={took:.2f}s stderr={done.stderr[-300:]!r}")
        return
    logger.info(f"batch_proof synced took={took:.2f}s")


def _checkout_for(context: Epoch2RootContext, key: tuple[Path, str], head: str) -> Path:
    """Return the prepared checkout of *head* for the Batch *key* names, building it once.

    A checkout of an earlier head is removed first, since the Batch's head moved past it.

    Raises:
        DaemonValidationError: The repository cannot check *head* out.
    """
    repository = context.identity.tree_root.parent
    with _LOCK:
        held = _CHECKOUTS.get(key)
    if held is not None and held.head == head and held.cwd.is_dir():
        return held.cwd
    if held is not None:
        discard_checkout(context, key[1])
    held = _Checkout(head=head, scratch=Path(tempfile.mkdtemp(prefix="eawf-proof-")))
    (held.scratch / _OWNER).write_text(str(os.getpid()), encoding="utf-8")
    if not _git_quietly(repository, "worktree", "add", "--detach", str(held.cwd), head):
        shutil.rmtree(held.scratch, ignore_errors=True)
        raise DaemonValidationError(
            f"validation_failed: proof_checkout_failed: the repository cannot check out {head}"
        )
    with _LOCK:
        _CHECKOUTS[key] = held
    _prepare(held.cwd)
    return held.cwd


def discard_checkout(context: Epoch2RootContext, batch_ref: str) -> None:
    """Remove the checkout a Batch's proofs ran in, once the Batch no longer needs it.

    Args:
        context: The native context of the tree the Batch lives in.
        batch_ref: The Batch's URN.
    """
    key = (context.identity.tree_root.resolve(), batch_ref)
    with _LOCK:
        held = _CHECKOUTS.pop(key, None)
    if held is not None:
        _remove(context.identity.tree_root.parent, held)


def prune_proofs(context: Epoch2RootContext, eligible: frozenset[str]) -> None:
    """Forget every proof of the tree whose Batch is no longer proof-eligible.

    A Batch that left eligibility -- a Task was added or reopened, it closed or was
    cancelled, its Milestone left ACTIVE -- keeps no note on the console, no failure
    mark and no checkout. A checkout a proof still runs in is left to it and removed on
    a later sweep.

    Args:
        context: The native context of the tree swept.
        eligible: The URNs of the Batches whose Tasks all let them close.
    """
    root = context.identity.tree_root.resolve()
    with _LOCK:
        cleared = [key for key in _NOTES if key[0] == root and key[1] not in eligible]
        for key in cleared:
            del _NOTES[key]
        for key in [key for key in _FAILED if key[0] == root and key[1] not in eligible]:
            del _FAILED[key]
        dropped = [
            _CHECKOUTS.pop(key)
            for key in list(_CHECKOUTS)
            if key[0] == root and key[1] not in eligible and key not in _RUNNING
        ]
    for key in cleared:
        _announce(context, key[1], None)
    for held in dropped:
        _remove(context.identity.tree_root.parent, held)


def prune_orphan_checkouts(repository: Path) -> int:
    """Remove every proof checkout of *repository* whose owning daemon is gone.

    A checkout lives as long as its daemon remembers it, so one a stopped daemon left
    behind is never reused or removed. Its git worktree registration goes with it, and
    a registration whose directory was already deleted is pruned.

    Args:
        repository: The repository whose worktrees are read.

    Returns:
        How many checkouts were removed.
    """
    listed = subprocess.run(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    removed = 0
    for line in listed.stdout.splitlines():
        path = Path(line.removeprefix("worktree ")) if line.startswith("worktree ") else None
        if (
            path is None
            or path.name != "checkout"
            or not path.parent.name.startswith("eawf-proof-")
        ):
            continue
        if _owned(path.parent):
            continue
        _remove(repository, _Checkout(head="", scratch=path.parent))
        removed += 1
    _git_quietly(repository, "worktree", "prune")
    if removed:
        logger.info(f"batch_proof pruned orphaned checkouts={removed}")
    return removed


def _owned(scratch: Path) -> bool:
    """Return whether a live daemon process owns the checkout under *scratch*."""
    try:
        owner = int((scratch / _OWNER).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return owner == os.getpid() or pid_alive(owner)


def _prove(
    context: Epoch2RootContext, key: tuple[Path, str], legs: tuple[_Leg, ...], head: str
) -> None:
    """Run *legs* at once in the Batch head's checkout and file a receipt for each."""
    claims = Path(tempfile.mkdtemp(prefix="eawf-proof-claims-"))
    try:
        cwd = _checkout_for(context, key, head)
        gate_context = GateExecutionContext(
            state_path=claims / "state.json", attempt_id=uuid.uuid4().hex
        )
        with ThreadPoolExecutor(max_workers=PROVERS) as pool:
            done = list(
                pool.map(lambda item: _attempt(item, cwd=cwd, gate_context=gate_context), legs)
            )
        by_task: dict[str, tuple[Task, list[tuple[VerificationLeg, CheckResult]]]] = {}
        for item, result in zip(legs, done, strict=True):
            by_task.setdefault(item.task.key, (item.task, []))[1].append((item.leg, result))
        for task, ran in by_task.values():
            file_proofs(context, task.urn, ran, now=datetime.now(UTC))
        logger.info(f"batch_proof settled batch={key[1]} head={head} criteria={len(legs)}")
    except Exception as error:
        # remembered against the head, so the sweep this wakes does not prove it again
        logger.exception(f"batch_proof failed batch={key[1]} head={head}")
        with _LOCK:
            _FAILED[key] = (head, f"{error!s}"[:300] or type(error).__name__)
    finally:
        shutil.rmtree(claims, ignore_errors=True)
        with _LOCK:
            _RUNNING.pop(key, None)
        SWEEP_WAKE.set()


__all__ = [
    "PROOF_BUDGET_SECONDS",
    "PROVERS",
    "VERIFY_GATE_KIND",
    "ProofOutcome",
    "ProofState",
    "discard_checkout",
    "judge_proof",
    "proof_notes",
    "prune_orphan_checkouts",
    "prune_proofs",
    "verify_argv",
]
