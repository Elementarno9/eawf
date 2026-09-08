"""Crash-safe, out-of-process execution of deterministic close gates.

Claims live under daemon-local ``.ea/local`` state and are keyed by the full
freshness digest. A claim is written atomically before a subprocess starts.
Only a persisted terminal :class:`GateReceipt` completes it. Therefore a daemon
restart can reuse a terminal result, while an orphaned pre-execution claim is
reported indeterminate and never rerun.

Gate work itself runs in a CHILD interpreter (:func:`run_gate_out_of_process`),
never in the daemon: a check kind that hard-exits its interpreter would
otherwise take the daemon down with it, losing every other in-flight close.
The child writes its own claim before executing, so a child that dies mid-gate
leaves exactly the orphaned claim the recovery path reads as indeterminate.
This module doubles as that child's entry point (``python -m
eawf.runtime.daemon.gate_execution --run-gate <request> <response>``).

A child interpreter would otherwise INHERIT the daemon's runtime directory and
state path, so a gate suite that drives eawf's own RPCs would drive them
against the live ledger and the live dispatch loop. Every child therefore runs
against a throwaway sandbox (:func:`gate_sandbox`) seeded from a snapshot of
both, with the live daemon's transport handles deliberately left behind. Gate
reads still see a faithful copy of the ledger; gate writes land in the copy and
die with it. The daemon's OWN bookkeeping (claims, receipts) is unaffected
because it addresses the live state path explicitly rather than through the
environment.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.gate_receipt_hygiene import (
    diagnostic_log_path,
    load_gate_diagnostic,
    scrub_gate_receipt_store,
)
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.lock import portalock
from eawf.workflow.audit_dsl.models import (
    CheckResult,
    CheckSpec,
)

logger = logging.getLogger(__name__)


class GateExecutionClaim(BaseModel):
    """One durable freshness-key execution claim."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    freshness_key: str = Field(min_length=64, max_length=64)
    claimed_at: datetime
    receipt_id: str | None = None
    result_payload: dict[str, Any] | None = None
    completed_at: datetime | None = None


def gate_receipt_id(freshness_key: str) -> str:
    """Return the canonical receipt id for *freshness_key*."""
    return f"GR-{freshness_key[:32]}"


def claim_path(
    state_path: Path,
    *,
    attempt_id: str,
    freshness_key: str,
) -> Path:
    """Return the daemon-local claim path for one global freshness key."""
    del attempt_id
    return state_path.parent / "local" / "gate-claims" / f"{freshness_key}.json"


def _load_claim(path: Path) -> GateExecutionClaim | None:
    if not path.is_file():
        return None
    try:
        return GateExecutionClaim.model_validate(orjson.loads(path.read_bytes()))
    except (OSError, orjson.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"gate execution claim unreadable: {path.name!r}") from exc


def _load_receipt(state_path: Path, receipt_id: str) -> GateReceipt | None:
    scrub_gate_receipt_store(state_path)
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return None
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line:
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
            if envelope.id != receipt_id:
                continue
            return GateReceipt.model_validate(envelope.payload)
        except orjson.JSONDecodeError, ValueError:
            continue
    return None


def _receipt_status(
    receipt: GateReceipt,
) -> tuple[bool, Literal["pass", "fail", "blocked"]]:
    if receipt.result is GateReceiptResult.PASS:
        return True, "pass"
    if receipt.result in {
        GateReceiptResult.BLOCKED,
        GateReceiptResult.TIMEOUT,
        GateReceiptResult.ERROR,
        GateReceiptResult.CANCELLED,
    }:
        return False, "blocked"
    return False, "fail"


def _result_from_receipt(
    receipt: GateReceipt,
    *,
    state_path: Path,
    spec: CheckSpec,
    freshness_key: str,
) -> CheckResult:
    passed, status = _receipt_status(receipt)
    diagnostic = load_gate_diagnostic(state_path, receipt.id)
    local_log = diagnostic_log_path(state_path, receipt.id)
    log_ref: str | None = None
    if local_log.is_file():
        try:
            log_ref = local_log.relative_to(state_path.parent.parent).as_posix()
        except ValueError:
            log_ref = None
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=passed,
        status=status,
        details=(
            diagnostic.details if diagnostic is not None else f"reused gate receipt {receipt.id}"
        ),
        started_at=receipt.started_at,
        ended_at=receipt.ended_at,
        duration_ms=receipt.duration_ms,
        timeout_class=receipt.timeout_class,
        resolved_timeout_seconds=(
            int(receipt.resolved_timeout_seconds)
            if receipt.resolved_timeout_seconds is not None
            else None
        ),
        exit_status=receipt.exit_status,
        argv=diagnostic.argv if diagnostic is not None else None,
        command=diagnostic.command if diagnostic is not None else None,
        stdout_tail=diagnostic.stdout_tail if diagnostic is not None else None,
        stderr_tail=diagnostic.stderr_tail if diagnostic is not None else None,
        stdout_digest=receipt.stdout_digest,
        stderr_digest=receipt.stderr_digest,
        selected_file_digest=receipt.selected_file_digest,
        collected_nodeid_digest=receipt.collected_nodeid_digest,
        residual_manifest_digest=receipt.residual_manifest_digest,
        runner_fingerprint=receipt.runner_digest,
        environment_fingerprint=receipt.environment_digest,
        full_log_ref=log_ref,
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def _indeterminate_result(spec: CheckSpec, freshness_key: str) -> CheckResult:
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="blocked",
        details=(
            "indeterminate gate execution: freshness claim exists without "
            f"terminal receipt ({gate_receipt_id(freshness_key)})"
        ),
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def _receipt_collision_result(spec: CheckSpec, freshness_key: str) -> CheckResult:
    """Fail closed when a truncated receipt id resolves to another full key."""
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="blocked",
        details=(
            "indeterminate gate receipt: receipt id collision for full "
            f"freshness key {freshness_key}"
        ),
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def claim_gate_execution(
    state_path: Path,
    *,
    attempt_id: str,
    criterion_id: str,
    gate_id: str,
    spec: CheckSpec,
    freshness_key: str,
) -> CheckResult | None:
    """Claim one freshness key or return a reusable/indeterminate result.

    ``None`` means this caller wrote the first claim and may execute. A returned
    result means the subprocess must not start.
    """
    receipt_id = gate_receipt_id(freshness_key)
    receipt = _load_receipt(state_path, receipt_id)
    path = claim_path(
        state_path,
        attempt_id=attempt_id,
        freshness_key=freshness_key,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(path, timeout=5.0):
        claim = _load_claim(path)
        receipt = receipt or _load_receipt(state_path, receipt_id)
        if receipt is not None:
            if receipt.freshness_key != freshness_key:
                return _receipt_collision_result(spec, freshness_key)
            if (
                claim is not None
                and claim.receipt_id == receipt.id
                and claim.result_payload is not None
            ):
                return CheckResult.model_validate(claim.result_payload)
            return _result_from_receipt(
                receipt,
                state_path=state_path,
                spec=spec,
                freshness_key=freshness_key,
            )
        if claim is not None:
            return _indeterminate_result(spec, freshness_key)
        atomic_write_json_locked(
            path,
            GateExecutionClaim(
                attempt_id=attempt_id,
                criterion_id=criterion_id,
                gate_id=gate_id,
                freshness_key=freshness_key,
                claimed_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )
    return None


def complete_gate_execution(
    state_path: Path,
    *,
    attempt_id: str,
    freshness_key: str,
    receipt_id: str,
    result: CheckResult,
) -> None:
    """Complete a claim only after its terminal receipt is durable."""
    receipt = _load_receipt(state_path, receipt_id)
    if receipt is None:
        raise ValueError(f"gate receipt missing while completing claim: {receipt_id!r}")
    if receipt.freshness_key != freshness_key:
        raise ValueError("gate receipt freshness mismatch")
    path = claim_path(
        state_path,
        attempt_id=attempt_id,
        freshness_key=freshness_key,
    )
    if not path.is_file():
        return
    with portalock.acquire(path, timeout=5.0):
        claim = _load_claim(path)
        if claim is None:
            return
        if claim.freshness_key != freshness_key:
            raise ValueError("gate execution claim freshness mismatch")
        updated = claim.model_copy(
            update={
                "receipt_id": receipt_id,
                "result_payload": result.model_dump(mode="json"),
                "completed_at": datetime.now(UTC),
            }
        )
        atomic_write_json_locked(path, updated.model_dump(mode="json"))


class GateExecutionContext(BaseModel):
    """Durable-close identity one out-of-process gate claims under."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_path: Path
    attempt_id: str = Field(min_length=1)


#: Ambient durable-close identity for the gate runner. The oracle sits four
#: awaits below the close worker behind a signature the worker does not own,
#: so the identity travels as task-local context rather than as a parameter
#: threaded through every intermediate call. Unset means "not a durable close"
#: and the legacy in-process path stays byte-identical.
_GATE_CONTEXT: ContextVar[GateExecutionContext | None] = ContextVar(
    "eawf_gate_execution_context",
    default=None,
)


@contextmanager
def durable_gate_context(context: GateExecutionContext) -> Iterator[None]:
    """Bind *context* for the duration of one durable close attempt."""
    token = _GATE_CONTEXT.set(context)
    try:
        yield
    finally:
        _GATE_CONTEXT.reset(token)


def current_gate_context() -> GateExecutionContext | None:
    """Return the durable-close identity in force, or ``None``."""
    return _GATE_CONTEXT.get()


class GateChildCrashError(RuntimeError):
    """A gate runner child died without writing a terminal result.

    Deliberately not a :class:`ValueError`: the close worker types a
    ``ValueError`` out of ``state.mutate`` as the wave's own evidence being
    rejected, and a runner that crashed proved nothing about the wave. Staying
    outside that branch routes the crash to the harness-fault classification,
    which re-queues the attempt for resume instead of blocking it.
    """

    def __init__(self, message: str, *, result: CheckResult) -> None:
        super().__init__(message)
        self.result = result


class _GateChildRequest(BaseModel):
    """One gate handed to a child interpreter."""

    model_config = ConfigDict(extra="forbid")

    spec: CheckSpec
    cwd: Path
    state_path: Path
    attempt_id: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)


class _GateChildResponse(BaseModel):
    """What a child interpreter reports back for one gate."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    result: CheckResult | None = None
    error: str | None = None


_CHILD_FLAG = "--run-gate"
_TAIL_LIMIT = 4_096


def _crash_result(spec: CheckSpec, *, exit_status: int | None, stderr: str) -> CheckResult:
    """Describe a child that died before writing its terminal result.

    ``started_at`` stays unset on purpose: an incomplete observation is not a
    persistable receipt, so the caller cannot complete the child's claim with
    it and the claim stays orphaned for the recovery path to adjudicate.
    """
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="fail",
        details=(
            f"gate runner child crashed without a terminal result (exit_status={exit_status})"
        ),
        exit_status=exit_status,
        stderr_tail=stderr[-_TAIL_LIMIT:] or None,
    )


#: Runtime-dir entries a sandbox never inherits. The socket, PID file, and
#: lock files are handles onto the LIVE daemon: copying them would point a
#: sandboxed client straight back at the process that owns the live ledger and
#: the dispatch loop, which is the exposure the sandbox exists to close.
_LIVE_DAEMON_HANDLES: frozenset[str] = frozenset(
    {"eawfd.sock", "eawfd.pid", "eawfd.lock", "eawfd.spawn.lock"}
)

#: Per-file ceiling for the runtime-dir snapshot. The daemon log and its
#: rotations are unbounded (hundreds of megabytes) and hold nothing a gate
#: reads, so the snapshot skips anything larger rather than paying that copy
#: once per gate.
SNAPSHOT_FILE_BYTE_CAP: int = 1 << 20

#: State-dir entries seeded next to the ledger file: the layered config a
#: mutation validates against and the append-only stores it writes. Heavy
#: read-only trees (``artifacts``) are absent because a gate reads those
#: through the repo working tree, and daemon-local scratch (``local``) is
#: absent because the live daemon's claims and locks must stay its own.
_SEEDED_STATE_ENTRIES: tuple[str, ...] = (
    "config.yaml",
    "profile.yaml",
    "profiles",
    "store",
)


class GateSandbox(BaseModel):
    """One throwaway runtime directory + state ledger a gate child runs in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    runtime_dir: Path
    state_path: Path


def _snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    """Return the entries of *directory* the runtime snapshot must not copy.

    Args:
        directory: Directory being walked, as :func:`shutil.copytree` passes it.
        names: Entry names inside *directory*.

    Returns:
        The subset of *names* to skip: live-daemon handles, anything that is
        not a regular file or directory (a Unix socket cannot be copied at
        all), and regular files above :data:`SNAPSHOT_FILE_BYTE_CAP`.
    """
    base = Path(directory)
    skipped: set[str] = set()
    for name in names:
        if name in _LIVE_DAEMON_HANDLES:
            skipped.add(name)
            continue
        entry = base / name
        if entry.is_dir():
            continue
        if not entry.is_file():
            skipped.add(name)
            continue
        try:
            oversized = entry.stat().st_size > SNAPSHOT_FILE_BYTE_CAP
        except OSError:
            skipped.add(name)
            continue
        if oversized:
            skipped.add(name)
    return skipped


def _seed_runtime_dir(live_runtime_dir: Path, target: Path) -> None:
    """Copy the snapshot-eligible part of *live_runtime_dir* into *target*."""
    target.mkdir(parents=True, exist_ok=True)
    if not live_runtime_dir.is_dir():
        return
    shutil.copytree(
        live_runtime_dir,
        target,
        ignore=_snapshot_ignore,
        dirs_exist_ok=True,
    )


def _seed_state_dir(live_state_path: Path, target_state_path: Path) -> None:
    """Copy the ledger file and its config/store siblings into the sandbox.

    Size-uncapped on purpose: the ledger and its stores ARE the surface a
    sandboxed RPC reads and writes, so a truncated copy would answer reads
    with a lie rather than with the live tree's own content.
    """
    target_dir = target_state_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    if live_state_path.is_file():
        shutil.copy2(live_state_path, target_state_path)
    live_dir = live_state_path.parent
    for name in _SEEDED_STATE_ENTRIES:
        source = live_dir / name
        if source.is_dir():
            shutil.copytree(source, target_dir / name, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, target_dir / name)


def seed_gate_sandbox(
    *,
    root: Path,
    live_state_path: Path,
    live_runtime_dir: Path,
) -> GateSandbox:
    """Materialise an isolated runtime dir + state ledger under *root*.

    A live state file that does not exist yet is not an error: the sandbox is
    then an empty ledger directory, which still routes every gate write away
    from the live tree.

    Args:
        root: Directory the sandbox is built in. Created when absent.
        live_state_path: The live ``state.json`` the ledger is snapshotted
            from; its parent supplies the config and store siblings.
        live_runtime_dir: The live daemon runtime directory to snapshot.

    Returns:
        The seeded :class:`GateSandbox`.

    Raises:
        ValueError: *root* exists as a non-directory, or *live_state_path*
            exists but is not a regular file.
    """
    if root.exists() and not root.is_dir():
        raise ValueError(f"gate sandbox root is not a directory: {str(root)!r}")
    if live_state_path.exists() and not live_state_path.is_file():
        raise ValueError(f"live state path is not a file: {str(live_state_path)!r}")
    sandbox = GateSandbox(
        root=root,
        runtime_dir=root / "runtime",
        state_path=root / live_state_path.parent.name / live_state_path.name,
    )
    _seed_runtime_dir(live_runtime_dir, sandbox.runtime_dir)
    _seed_state_dir(live_state_path, sandbox.state_path)
    logger.debug(
        f"seed_gate_sandbox root={str(sandbox.root)!r} runtime_dir={str(sandbox.runtime_dir)!r}"
    )
    return sandbox


@contextmanager
def gate_sandbox(*, live_state_path: Path) -> Iterator[GateSandbox]:
    """Yield a sandbox seeded from the live runtime dir + ledger, then drop it.

    The root sits under the system temp directory rather than under the live
    state directory: a gate that walks upward from the sandbox must not find
    the live tree, and the 104-byte AF_UNIX path cap leaves no room for a deep
    in-repo path when a sandboxed client auto-spawns its own daemon.

    Args:
        live_state_path: The live ``state.json`` to snapshot.

    Yields:
        The seeded :class:`GateSandbox`, removed on exit even when the body
        raises.
    """
    root = Path(tempfile.mkdtemp(prefix="eawf-gate-"))
    try:
        yield seed_gate_sandbox(
            root=root,
            live_state_path=live_state_path,
            live_runtime_dir=runtime_dir(),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def gate_child_env(sandbox: GateSandbox) -> dict[str, str]:
    """Return the child environment with every live-tree seam repointed.

    ``EAWF_RUNTIME_DIR`` and ``EA_STATE`` are the two overrides both resolvers
    consult first, so pinning them denies a gate suite the live daemon socket
    and the live ledger in one move. The spec-cache override is dropped rather
    than pinned: unset, it derives from the sandbox runtime dir, while an
    inherited absolute value would leak a live path back in.

    Args:
        sandbox: The sandbox the child must run against.

    Returns:
        A copy of the current environment with the sandbox bindings applied.
    """
    env = dict(os.environ)
    env["EAWF_RUNTIME_DIR"] = str(sandbox.runtime_dir)
    env["EA_STATE"] = str(sandbox.state_path)
    env.pop("EAWF_SPEC_CACHE_DIR", None)
    return env


def run_gate_out_of_process(
    spec: CheckSpec,
    *,
    cwd: Path,
    context: GateExecutionContext,
    criterion_id: str,
    gate_id: str,
) -> CheckResult:
    """Run one deterministic gate in a child interpreter and return its receipt.

    The child claims its own freshness key before executing, so the durable
    at-most-once property holds across the process boundary.

    Raises:
        GateChildCrashError: The child died without a terminal result.
        ValueError: The child reported a typed gate failure, matching what
            in-process execution raises for the same input.
    """
    workdir = context.state_path.parent / "local" / "gate-children"
    workdir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    request_path = workdir / f"{token}.request.json"
    response_path = workdir / f"{token}.response.json"
    request = _GateChildRequest(
        spec=spec,
        cwd=cwd,
        state_path=context.state_path,
        attempt_id=context.attempt_id,
        criterion_id=criterion_id,
        gate_id=gate_id,
    )
    try:
        request_path.write_bytes(orjson.dumps(request.model_dump(mode="json")))
        with gate_sandbox(live_state_path=context.state_path) as sandbox:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "eawf.runtime.daemon.gate_execution",
                    _CHILD_FLAG,
                    str(request_path),
                    str(response_path),
                ],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                env=gate_child_env(sandbox),
            )
        response = _read_child_response(response_path)
        if response is None:
            result = _crash_result(
                spec,
                exit_status=completed.returncode,
                stderr=completed.stderr or "",
            )
            raise GateChildCrashError(
                f"gate runner child crashed: gate={gate_id!r} exit_status={completed.returncode}",
                result=result,
            )
        if not response.ok or response.result is None:
            raise ValueError(response.error or "gate child reported no result")
        return response.result
    finally:
        for path in (request_path, response_path):
            if path.is_file():
                path.unlink()


def _read_child_response(response_path: Path) -> _GateChildResponse | None:
    """Return the child's typed response, or ``None`` when it never landed."""
    if not response_path.is_file():
        return None
    try:
        return _GateChildResponse.model_validate(orjson.loads(response_path.read_bytes()))
    except OSError, orjson.JSONDecodeError, ValueError:
        return None


def _execute_child_request(request_path: Path, response_path: Path) -> None:
    """Claim, run, and report one gate from inside the child interpreter."""
    from eawf.workflow.audit_dsl.runner import run_checks

    request = _GateChildRequest.model_validate(orjson.loads(request_path.read_bytes()))

    def _before_execute(spec: CheckSpec, freshness_key: str) -> CheckResult | None:
        return claim_gate_execution(
            request.state_path,
            attempt_id=request.attempt_id,
            criterion_id=request.criterion_id,
            gate_id=request.gate_id,
            spec=spec,
            freshness_key=freshness_key,
        )

    try:
        result = run_checks(
            [request.spec],
            cwd=request.cwd,
            before_execute=_before_execute,
        )[0]
    except Exception as exc:
        response = _GateChildResponse(ok=False, error=f"{type(exc).__name__}: {exc!s}")
    else:
        response = _GateChildResponse(ok=True, result=result)
    staging = response_path.with_suffix(".partial")
    staging.write_bytes(orjson.dumps(response.model_dump(mode="json")))
    os.replace(staging, response_path)


def main(argv: Sequence[str] | None = None) -> int:
    """Child entry point: run one gate named by a request file."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != _CHILD_FLAG:
        print(
            f"usage: python -m eawf.runtime.daemon.gate_execution {_CHILD_FLAG} "
            "<request-path> <response-path>",
            file=sys.stderr,
        )
        return 2
    _execute_child_request(Path(args[1]), Path(args[2]))
    return 0


__all__ = [
    "GateChildCrashError",
    "GateExecutionClaim",
    "GateExecutionContext",
    "GateSandbox",
    "claim_gate_execution",
    "claim_path",
    "complete_gate_execution",
    "current_gate_context",
    "durable_gate_context",
    "gate_child_env",
    "gate_receipt_id",
    "gate_sandbox",
    "run_gate_out_of_process",
    "seed_gate_sandbox",
]


if __name__ == "__main__":
    raise SystemExit(main())
