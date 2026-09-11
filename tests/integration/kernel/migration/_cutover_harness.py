"""Shared fixtures for the rollback and recovery suites.

Both suites need the same three things: a fresh copy of the pinned epoch-1
corpus, a target tree that has declared itself a disposable canary and
carries authority surfaces worth restoring, and a way to interrupt one
apply at a named durable write. They are here rather than duplicated
because the crash injector in particular is the kind of helper that drifts
apart the moment there are two of it.

The target tree is seeded with a document, a config and one ledger before
any apply runs. That matters: a restore that has no bytes to put back
proves nothing, and three of the five declared authority surfaces being
present and two absent is what makes "restore the full set" a claim about a
set rather than about a file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.migration.epoch2.apply import CutoverResult, Epoch2ApplyRequest, apply_cutover
from eawf.kernel.migration.epoch2.canary import CANARY_DECLARATION_FILENAME
from eawf.kernel.migration.epoch2.generation import generation_id_for
from eawf.kernel.migration.epoch2.plan_mode import Epoch2PlanRequest, MigrationPlan, plan_cutover

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"
FAULTS = FIXTURES / "cutover-faults"
CANARY_DECLARATION = FAULTS / "canary" / CANARY_DECLARATION_FILENAME
QUIESCENT_DOCUMENT = FAULTS / "quiescent" / "state.json"
REGISTRY = FAULTS / "registry" / "registry.json"
CRASH_POINTS = FAULTS / "crash-points.json"
CRASH_CHILD = Path(__file__).resolve().parent / "_crash_apply.py"

WORKSPACE_KEY = "WSP-DEFAULT"
PROJECT_KEY = "PRJ-DEMO"
REPOSITORY_KEY = "REP-DEMO"
SEALED_BY = "rollback-test"
APPLIED_AT = datetime(2026, 3, 3, tzinfo=UTC)
RECOVERED_AT = datetime(2026, 4, 4, tzinfo=UTC)
REAPPLIED_AT = datetime(2026, 5, 5, tzinfo=UTC)

#: The exit status the crash child reports when it was killed at the seam.
CRASH_EXIT = 7

#: The authority surfaces the harness seeds into every target tree, so a
#: restore has bytes to write back rather than only digests to compare.
SEEDED_SURFACES: tuple[str, ...] = ("config.yaml", "state.json", "store/audit.jsonl")

#: What each seeded surface holds. The document is the quiescent fixture,
#: because a target with a live session refuses the cutover outright.
_SEEDED_CONFIG = "epoch: 1\nseeded_by: cutover-harness\n"
_SEEDED_LEDGER = '{"id": "AUD-SEED", "kind": "audit"}\n'


@dataclass(frozen=True)
class CutoverTree:
    """One target tree an apply has been run into, finished or not.

    Attributes:
        corpus: The staged epoch-1 corpus the apply read.
        target_root: The tree the apply was writing into.
        generation_id: The generation that apply published, or would have.
        plan: The plan it ran under.
    """

    corpus: Path
    target_root: Path
    generation_id: str
    plan: MigrationPlan


def plan_request_for(corpus: Path) -> Epoch2PlanRequest:
    """Return the plan request both suites apply under."""
    return Epoch2PlanRequest(
        snapshot_root=str(corpus),
        allowlist_path=str(ALLOWLIST),
        workspace_key=WORKSPACE_KEY,
        project_key=PROJECT_KEY,
        repository_key=REPOSITORY_KEY,
        sealed_by=SEALED_BY,
    )


def plan_over(corpus: Path) -> MigrationPlan:
    """Return one sealed plan over ``corpus`` under the shared test seal."""
    return plan_cutover(plan_request_for(corpus), sealed_at=APPLIED_AT)


def staged_corpus(root: Path) -> Path:
    """Return a fresh copy of the pinned corpus under ``root``."""
    corpus = root / "staged"
    shutil.copytree(FULL_SNAPSHOT, corpus)
    return corpus


def declared_canary(root: Path) -> Path:
    """Create ``root``, declare it disposable, and seed its surfaces.

    Args:
        root: The target tree's root.

    Returns:
        The root, carrying the declaration plus three of the five declared
        authority surfaces.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANARY_DECLARATION, root / CANARY_DECLARATION_FILENAME)
    shutil.copyfile(QUIESCENT_DOCUMENT, root / "state.json")
    (root / "config.yaml").write_text(_SEEDED_CONFIG, encoding="utf-8")
    (root / "store").mkdir(exist_ok=True)
    (root / "store" / "audit.jsonl").write_text(_SEEDED_LEDGER, encoding="utf-8")
    return root


def apply_request_for(
    *, corpus: Path, target_root: Path, plan: MigrationPlan
) -> Epoch2ApplyRequest:
    """Return the apply request for one corpus, target and approved plan."""
    return Epoch2ApplyRequest(
        plan_request=plan_request_for(corpus),
        target_root=str(target_root),
        registry_path=str(REGISTRY),
        plan_digest=plan.approval_digest,
        accepted_unresolved_rows=tuple(row.address for row in plan.manifest.unresolved_rows),
    )


def apply_once(*, corpus: Path, target_root: Path, applied_at: datetime) -> CutoverResult:
    """Apply the pinned plan over ``corpus`` into ``target_root``."""
    plan = plan_over(corpus)
    return apply_cutover(
        apply_request_for(corpus=corpus, target_root=target_root, plan=plan),
        applied_at=applied_at,
    )


def applied_tree(root: Path) -> CutoverTree:
    """Return a tree whose cutover ran to completion, marker and all.

    Args:
        root: A directory to build the corpus and the target under.

    Returns:
        The finished tree, which is where the crossed-boundary cases start:
        the one-way door is only reachable once the activation is complete.
    """
    corpus = staged_corpus(root)
    target_root = declared_canary(root / ".ea")
    result = apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)
    return CutoverTree(
        corpus=corpus,
        target_root=target_root,
        generation_id=result.generation_id,
        plan=plan_over(corpus),
    )


def content_digests(root: Path) -> dict[str, str]:
    """Return a digest per content file under ``root``.

    Args:
        root: The tree to walk.

    Returns:
        One entry per regular file that is not a lock holder record, keyed
        by relative POSIX path. Lock files are excluded because they carry a
        pid and a heartbeat rather than content: every acquisition rewrites
        them.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def crash_points() -> tuple[dict[str, Any], ...]:
    """Return every declared crash point, in the order the apply reaches them.

    Returns:
        The rows of the cutover-faults crash-point table.

    Raises:
        json.JSONDecodeError: The table is not JSON.
    """
    payload = json.loads(CRASH_POINTS.read_text(encoding="utf-8"))
    return tuple(payload["crash_points"])


def crash_the_apply(*, root: Path, point: dict[str, Any]) -> CutoverTree:
    """Run one apply in a child that dies at ``point``, and return the wreckage.

    Args:
        root: A directory to build the corpus and the target under.
        point: One row of the crash-point table.

    Returns:
        The crashed tree.

    Raises:
        AssertionError: The child did not die at the seam, which makes the
            crash point vacuous -- either the seam was renamed or the apply
            no longer reaches it.
    """
    corpus = staged_corpus(root)
    target_root = declared_canary(root / ".ea")
    plan = plan_over(corpus)
    params = {
        "snapshot_root": str(corpus),
        "allowlist_path": str(ALLOWLIST),
        "workspace_key": WORKSPACE_KEY,
        "project_key": PROJECT_KEY,
        "repository_key": REPOSITORY_KEY,
        "sealed_by": SEALED_BY,
        "target_root": str(target_root),
        "registry_path": str(REGISTRY),
        "applied_at": APPLIED_AT.isoformat(),
        **point["seam"],
    }
    params_path = root / "crash-params.json"
    params_path.write_text(json.dumps(params, sort_keys=True), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(CRASH_CHILD), str(params_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == CRASH_EXIT, (
        f"crash point {point['id']} did not reach its seam "
        f"(exit {completed.returncode}): {completed.stderr[-2000:]}"
    )
    return CutoverTree(
        corpus=corpus,
        target_root=target_root,
        generation_id=generation_id_for(plan.manifest.manifest_digest),
        plan=plan,
    )
