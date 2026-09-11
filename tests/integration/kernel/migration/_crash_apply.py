"""Run one epoch-2 apply in a child interpreter and kill it at a named write.

An exception is not a crash. Raising inside the apply unwinds the stack,
runs every ``finally``, closes the maintenance window and releases the
authority locks -- which is exactly the cleanup a power cut does not do. So
the fault is injected in a child process that calls :func:`os._exit` at the
chosen seam: no unwinding, no ``finally``, no flush of anything still
buffered. What the parent then finds on disk is what a real interruption
leaves.

The seam is named rather than counted from the top: ``module`` and
``attribute`` address one callable the apply reaches, ``occurrence`` picks
which call of it, and ``when`` decides whether the process dies instead of
that call or immediately after it. "Immediately after call N" and "instead
of call N+1" are the same boundary from either side, which is why one
mechanism reaches every durable write the apply makes.

Invoked as ``python _crash_apply.py <params.json>``; see
``_cutover_harness.crash_the_apply`` for the parameter shape. Exit ``7``
means the seam was reached and the process was killed there; ``0`` means it
was never reached, which makes the crash point vacuous and is a test
failure rather than a pass.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from eawf.kernel.migration.epoch2.apply import Epoch2ApplyRequest, apply_cutover
from eawf.kernel.migration.epoch2.plan_mode import Epoch2PlanRequest, plan_cutover

#: The status a killed child exits with, distinct from any exit an
#: uninjected failure could produce.
CRASH_EXIT = 7


def install_fault(*, module_name: str, attribute: str, occurrence: int, when: str) -> None:
    """Replace one callable with a version that kills the process at a call.

    Args:
        module_name: The module holding the callable.
        attribute: The callable's name, dotted for a method on a class.
        occurrence: Which call of it kills the process, 1-based.
        when: ``"before"`` to die instead of the call, ``"after"`` to die
            once it has returned.

    Raises:
        AttributeError: The module carries no such attribute, which means
            the seam was renamed and the crash point addresses nothing.
        ValueError: ``when`` is neither ``"before"`` nor ``"after"``.
    """
    if when not in ("before", "after"):
        raise ValueError(f"{when!r} is not a crash position")
    holder: Any = importlib.import_module(module_name)
    parts = attribute.split(".")
    for part in parts[:-1]:
        holder = getattr(holder, part)
    name = parts[-1]
    original: Callable[..., Any] = getattr(holder, name)
    calls = 0

    def fault(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        hit = calls == occurrence
        if hit and when == "before":
            os._exit(CRASH_EXIT)
        result = original(*args, **kwargs)
        if hit and when == "after":
            os._exit(CRASH_EXIT)
        return result

    setattr(holder, name, fault)


def main(argv: list[str]) -> int:
    """Apply the pinned corpus into the pinned target, dying at the seam.

    Args:
        argv: ``[script, params_path]``.

    Returns:
        ``0`` when the apply completed, which means the seam was never
        reached.

    Raises:
        IndexError: No parameter file was passed.
    """
    params = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    applied_at = datetime.fromisoformat(params["applied_at"])
    plan_request = Epoch2PlanRequest(
        snapshot_root=params["snapshot_root"],
        allowlist_path=params["allowlist_path"],
        workspace_key=params["workspace_key"],
        project_key=params["project_key"],
        repository_key=params["repository_key"],
        sealed_by=params["sealed_by"],
    )
    plan = plan_cutover(plan_request, sealed_at=applied_at)
    request = Epoch2ApplyRequest(
        plan_request=plan_request,
        target_root=params["target_root"],
        registry_path=params["registry_path"],
        plan_digest=plan.approval_digest,
        accepted_unresolved_rows=tuple(row.address for row in plan.manifest.unresolved_rows),
    )
    install_fault(
        module_name=params["module"],
        attribute=params["attribute"],
        occurrence=params["occurrence"],
        when=params["when"],
    )
    apply_cutover(request, applied_at=applied_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
