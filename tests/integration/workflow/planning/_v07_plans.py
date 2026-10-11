"""Drive the committed v0.7 roadmap proposals through the real plan verbs.

The rc1, rc2 and stable proposals under ``docs/roadmap/v0.7`` are the files
the operator submits live, beside the Track and repository create documents
a submit needs first. Both suites that prove them read the same files and
walk them through the daemon's own create, submit, approve and apply
methods, in the order the operator runs them, so the walk lives here once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

from eawf.kernel.identity import EntityKind
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.domain_create import (
    DOMAIN_CREATE_METHODS,
    REPOSITORY_CREATE_METHOD,
)
from eawf.runtime.daemon.methods.planning import (
    PLAN_APPLY_METHOD,
    PLAN_APPROVE_METHOD,
    PLAN_SUBMIT_METHOD,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.integration.workflow.planning._plan_approval import seal_plan_approval

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
ROADMAP: Final = REPO_ROOT / "docs" / "roadmap" / "v0.7"

#: The workspace, project and repository the live epoch-2 cutover addressed
#: this repository as. A create does not check the slot of the URN it is
#: handed, so a committed file spelled under another slot would be recorded
#: under it permanently.
LIVE_SLOT: Final = "eawf://EAWF/EAWF/EAWF"

#: Every committed file the operator passes to a live verb.
COMMITTED_FILES: Final = ("track", "repository", "rc1", "rc2", "stable")

#: The three Milestones of the remaining release train, in apply order.
PLAN_NAMES: Final = ("rc1", "rc2", "stable")

#: The applied revision each re-planning proposal names as its parent, kept
#: as it was filed so the canary can land it before the child rewrites it.
PARENTS: Final = {"rc2": REPO_ROOT / "tests" / "fixtures" / "planning" / "v07" / "rc2-prv0201.json"}

#: The human who approves; a plan approval admits no other principal kind.
OPERATOR: Final = {"principal_kind": "operator", "principal_id": "OP-0001"}
ACTOR: Final = "OP-0001"


def load_proposal(name: str) -> dict[str, Any]:
    """Return one committed proposal or create document, parsed."""
    return json.loads((ROADMAP / f"{name}.json").read_text(encoding="utf-8"))


def foreign_urns(document: Any) -> list[str]:
    """Return every URN in *document* spelled outside :data:`LIVE_SLOT`."""
    if isinstance(document, dict):
        return [urn for value in document.values() for urn in foreign_urns(value)]
    if isinstance(document, list):
        return [urn for value in document for urn in foreign_urns(value)]
    if isinstance(document, str) and document.startswith("eawf://"):
        return [] if document.startswith(f"{LIVE_SLOT}/") else [document]
    return []


def tree_cursor(repo_root: Path) -> int:
    """Return the canonical sequence a create against *repo_root* must expect."""
    authority = require_native_authority(repo_root / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return int(read_document(path).get("canonical_sequence", 0))


def create_containers(repo_root: Path, runtime: Path) -> None:
    """Create the Track and the repository row every plan binds, from the committed specs.

    The URNs are read off the rc1 proposal, so the containers cannot drift
    from what the plans name.
    """
    body = load_proposal(PLAN_NAMES[0])["body"]
    for method, urn, spec in (
        (
            DOMAIN_CREATE_METHODS[EntityKind.TRACK],
            body["milestone"]["primary_track_ref"],
            "track",
        ),
        (REPOSITORY_CREATE_METHOD, body["batches"][0]["repository_ref"], "repository"),
    ):
        answer = dispatch(
            method,
            repo_root,
            runtime,
            {
                "urn": urn,
                "expected_revision": tree_cursor(repo_root),
                "idempotency_key": f"create-{spec}",
                "actor": ACTOR,
                "spec": load_proposal(spec),
            },
        )
        assert answer["status"] == "ok", (spec, answer["errors"])


def dispatch(method: str, repo_root: Path, runtime: Path, params: dict[str, Any]) -> dict[str, Any]:
    """Drive one planning verb against the tree at *repo_root* and return its envelope."""
    context = method_context(runtime)
    return asyncio.run(methods.dispatch(method, context, {"repo_root": str(repo_root), **params}))


def approve_params(
    proposal: dict[str, Any], approved_by: dict[str, str], *, action_ref: str
) -> dict[str, Any]:
    """Return the approve request for a freshly submitted *proposal*."""
    return {
        "key": proposal["key"],
        "expected_revision": 2,
        "action_ref": action_ref,
        "approved_by": approved_by,
        "actor": ACTOR,
        "idempotency_key": f"approve-{proposal['key']}",
    }


def submit(proposal: dict[str, Any], repo_root: Path, runtime: Path) -> dict[str, Any]:
    """Submit *proposal* and return the envelope."""
    return dispatch(
        PLAN_SUBMIT_METHOD,
        repo_root,
        runtime,
        {"proposal": proposal, "actor": ACTOR, "idempotency_key": f"submit-{proposal['key']}"},
    )


def approve(
    proposal: dict[str, Any], repo_root: Path, runtime: Path, approved_by: dict[str, str]
) -> dict[str, Any]:
    """Seal *approved_by*'s approval onto the submitted *proposal*.

    The operator answers the plan's approval question first, through the
    same verbs the ``/plan`` skill drives, so the approval cites a sealed
    PendingAction the tree holds.
    """
    action_ref = seal_plan_approval(
        repo_root, runtime, subject=proposal["body"]["milestone_urn"], tag=proposal["key"]
    )
    params = approve_params(proposal, approved_by, action_ref=action_ref)
    return dispatch(PLAN_APPROVE_METHOD, repo_root, runtime, params)


def apply(proposal: dict[str, Any], repo_root: Path, runtime: Path) -> dict[str, Any]:
    """Apply the approved *proposal* and return the envelope."""
    return dispatch(
        PLAN_APPLY_METHOD,
        repo_root,
        runtime,
        {
            "key": proposal["key"],
            "expected_revision": 3,
            "actor": ACTOR,
            "idempotency_key": f"apply-{proposal['key']}",
        },
    )


def load_parent(name: str) -> dict[str, Any]:
    """Return the applied revision the committed proposal *name* re-plans."""
    return json.loads(PARENTS[name].read_text(encoding="utf-8"))


def land(name: str, repo_root: Path, runtime: Path) -> None:
    """Submit, approve and apply one committed proposal, asserting each step lands.

    A proposal that re-plans an earlier revision has that parent landed
    first, so the canary holds the Milestone the child rewrites.
    """
    if name in PARENTS:
        _land(load_parent(name), f"{name} parent", repo_root, runtime)
    _land(load_proposal(name), name, repo_root, runtime)


def _land(proposal: dict[str, Any], name: str, repo_root: Path, runtime: Path) -> None:
    for step in (
        lambda: submit(proposal, repo_root, runtime),
        lambda: approve(proposal, repo_root, runtime, OPERATOR),
        lambda: apply(proposal, repo_root, runtime),
    ):
        answer = step()
        assert answer["status"] == "ok", (name, answer["errors"])
