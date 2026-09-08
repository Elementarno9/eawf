"""Out-of-process, sandboxed execution of the readiness checks.

The close-readiness projection runs deterministic falsifiers -- the
profile-fed floor pack and the advisory single-gate path -- while it is
being driven from inside the daemon. Those falsifiers are routinely whole
test suites, and a suite that exercises eawf's own RPCs drives whichever
runtime directory and state ledger its process points at. Run in the
daemon, that is the LIVE pair: the suite then writes the live ledger,
flips the live dispatch pause flag, and drives the production dispatch
path for real.

This module denies the readiness checks that reach. Every check runs in a
child interpreter whose ``EAWF_RUNTIME_DIR`` and ``EA_STATE`` are pinned
to a throwaway sandbox seeded from a snapshot of the live pair
(:func:`eawf.runtime.daemon.gate_execution.gate_sandbox`). Reads still see
a faithful copy of the ledger; writes land in the copy and die with it.
The module doubles as that child's entry point (``python -m
eawf.workflow.verify.sandboxed_checks --run-checks <request> <response>``).

Execution here deliberately carries no durable-close identity, which is
why it is a sibling of
:func:`eawf.runtime.daemon.gate_execution.run_gate_out_of_process` rather
than a caller of it: readiness is a read-only projection, so it must claim
no freshness key, write no receipt, and leave no scratch file anywhere in
the live state directory. It also runs a whole batch of checks per child,
because a floor pack is compiled and scored as one list.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec

logger = logging.getLogger(__name__)

_CHILD_FLAG = "--run-checks"

#: Ceiling on the child stderr carried onto a blocked result. Matches the
#: gate runner's tail budget so an operator reading either surface sees the
#: same amount of context.
_TAIL_LIMIT = 4_096


class _ChildRequest(BaseModel):
    """One batch of checks handed to a child interpreter."""

    model_config = ConfigDict(extra="forbid")

    specs: list[CheckSpec]
    cwd: Path


class _ChildResponse(BaseModel):
    """What a child interpreter reports back for one batch."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    results: list[CheckResult] | None = None
    error: str | None = None


def _blocked_results(
    specs: Sequence[CheckSpec],
    *,
    detail: str,
    exit_status: int | None,
    stderr: str,
) -> list[CheckResult]:
    """Return one non-passing result per spec for a child that proved nothing.

    ``started_at`` stays unset: an incomplete observation is not a receipt,
    so no caller can persist it as one.

    Args:
        specs: The batch the child was handed, in declaration order.
        detail: One-line operator-facing note naming what went wrong.
        exit_status: The child's exit status, or ``None`` when unknown.
        stderr: The child's stderr; only its tail is carried.

    Returns:
        One ``status="blocked"`` :class:`CheckResult` per input spec.
    """
    tail = stderr[-_TAIL_LIMIT:] or None
    return [
        CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="blocked",
            details=detail,
            exit_status=exit_status,
            stderr_tail=tail,
        )
        for spec in specs
    ]


def _empty_ledger_seed(workdir: Path) -> Path:
    """Return a ledger path under *workdir* that deliberately does not exist.

    A caller that cannot name the ledger it is scoring gets an EMPTY sandbox
    rather than one guessed from ``EA_STATE`` / the process cwd. Guessing is
    the failure mode this module exists to close: the ambient chain resolves
    to the live tree, so a wrong guess would hand the child a faithful copy
    of a ledger nobody asked for -- and, on a resolver that returns the
    caller's own repository, a copy of the very tree under test.

    Args:
        workdir: Scratch directory that dies with the call.

    Returns:
        A non-existent ``state.json`` path whose parent names a state
        directory, which seeds an empty ledger with no config or stores.
    """
    return workdir / ".ea" / "state.json"


def run_checks_out_of_process(
    specs: Sequence[CheckSpec],
    *,
    cwd: Path,
    live_state_path: Path | None = None,
) -> list[CheckResult]:
    """Run *specs* in a child interpreter pinned to a throwaway sandbox.

    Args:
        specs: Already-compiled check specs, executed in declaration order.
            An empty sequence returns ``[]`` without spawning anything.
        cwd: Working directory the checks run against -- the repository
            root, so the runner's git diff-base and scope resolution behave
            exactly as they do in process.
        live_state_path: The live ``state.json`` the sandbox ledger is
            snapshotted from, so the checks read a faithful copy. ``None``
            seeds an EMPTY ledger instead of guessing one from the process
            environment -- fail-safe, because a guess resolves to the live
            tree the sandbox exists to deny.

    Returns:
        One :class:`CheckResult` per input spec, in declaration order. A
        child that crashed, reported a typed fault, or answered with the
        wrong number of results yields one ``blocked`` result per spec
        instead of raising: readiness is a read-only projection, and a
        non-result must never project as a pass.

    Raises:
        ValueError: *cwd* is not an existing directory. The checks resolve
            every relative path against it, so a bad cwd is a caller bug
            that would otherwise surface as a pile of unexplained failures.
    """
    ordered = list(specs)
    if not ordered:
        return []
    if not cwd.is_dir():
        raise ValueError(f"readiness check cwd is not a directory: {str(cwd)!r}")

    from eawf.runtime.daemon.gate_execution import gate_child_env, gate_sandbox

    request = _ChildRequest(specs=ordered, cwd=cwd)
    with tempfile.TemporaryDirectory(prefix="eawf-readiness-") as raw_workdir:
        workdir = Path(raw_workdir)
        state_path = _empty_ledger_seed(workdir) if live_state_path is None else live_state_path
        request_path = workdir / "request.json"
        response_path = workdir / "response.json"
        request_path.write_bytes(orjson.dumps(request.model_dump(mode="json")))
        with gate_sandbox(live_state_path=state_path) as sandbox:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "eawf.workflow.verify.sandboxed_checks",
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
        return _blocked_results(
            ordered,
            detail=(
                "readiness check child crashed without a terminal result "
                f"(exit_status={completed.returncode})"
            ),
            exit_status=completed.returncode,
            stderr=completed.stderr or "",
        )
    if not response.ok or response.results is None:
        return _blocked_results(
            ordered,
            detail=f"readiness check child reported a fault: {response.error or 'no result'}",
            exit_status=completed.returncode,
            stderr=completed.stderr or "",
        )
    if len(response.results) != len(ordered):
        return _blocked_results(
            ordered,
            detail=(
                f"readiness check child returned {len(response.results)} results "
                f"for {len(ordered)} checks"
            ),
            exit_status=completed.returncode,
            stderr=completed.stderr or "",
        )
    logger.debug(
        f"run_checks_out_of_process checks={len(ordered)} "
        f"exit_status={completed.returncode} runner='out-of-process'"
    )
    return list(response.results)


def _read_child_response(response_path: Path) -> _ChildResponse | None:
    """Return the child's typed response, or ``None`` when it never landed."""
    if not response_path.is_file():
        return None
    try:
        return _ChildResponse.model_validate(orjson.loads(response_path.read_bytes()))
    except (orjson.JSONDecodeError, ValidationError) as exc:
        logger.warning(f"_read_child_response status='unreadable' detail={exc!s}")
        return None


def _execute_child_request(request_path: Path, response_path: Path) -> None:
    """Run one batch of checks from inside the child interpreter.

    The response is staged then renamed so the parent never reads a partial
    document and mistakes it for a terminal answer.

    Args:
        request_path: Typed :class:`_ChildRequest` document written by the
            parent.
        response_path: Where the typed :class:`_ChildResponse` is placed.
    """
    from eawf.workflow.audit_dsl.runner import run_checks

    request = _ChildRequest.model_validate(orjson.loads(request_path.read_bytes()))
    try:
        results = run_checks(list(request.specs), cwd=request.cwd)
    except Exception as exc:
        response = _ChildResponse(ok=False, error=f"{type(exc).__name__}: {exc!s}")
    else:
        response = _ChildResponse(ok=True, results=results)
    staging = response_path.with_suffix(".partial")
    staging.write_bytes(orjson.dumps(response.model_dump(mode="json")))
    os.replace(staging, response_path)


def main(argv: Sequence[str] | None = None) -> int:
    """Child entry point: run the batch of checks named by a request file.

    Args:
        argv: Argument list without the program name. Defaults to
            :data:`sys.argv` minus the program name.

    Returns:
        ``0`` once the response document is written, ``2`` on a usage error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != _CHILD_FLAG:
        print(
            f"usage: python -m eawf.workflow.verify.sandboxed_checks {_CHILD_FLAG} "
            "<request-path> <response-path>",
            file=sys.stderr,
        )
        return 2
    _execute_child_request(Path(args[1]), Path(args[2]))
    return 0


__all__ = [
    "main",
    "run_checks_out_of_process",
]


if __name__ == "__main__":
    raise SystemExit(main())
