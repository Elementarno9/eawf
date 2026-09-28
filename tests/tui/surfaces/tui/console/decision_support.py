"""What the decision overlay and card suites share: their records, a view and a key path.

The console here holds the packaged chrome alone, so every row a frame draws comes from
the records or the read model a test hands it and never from a prototype register. The
frame is composed before each key, the way the app does, because a renderer publishes the
cursor bounds the key handler reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.projection.compute import RouteProjection, build_route_projection
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.surfaces.tui.console.app import compose_frame
from eawf.surfaces.tui.console.decisions import DecisionRecords
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.navigation import Ctx, open_overlay
from eawf.surfaces.tui.console.session import SIZES, Session
from eawf.workflow.delivery.acceptance import AcceptanceApproval
from eawf.workflow.projection.acceptance import build_acceptance_view

from .overlay_support import Host, Link, chrome

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/console"
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SCOPE = "EAWF"
CONTAINER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
MILESTONE_URN = f"{CONTAINER}/milestone/MLS-0030"
HEAD_SHA = "a" * 40
TREE_SHA = "b" * 40


def records(name: str) -> DecisionRecords:
    """Return the decision records of fixture ``name``, validated at the boundary."""
    return DecisionRecords.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))


def opened(
    route: str, overlay: str | None = None, subject: str | None = None, **fields: Any
) -> Session:
    """Return a session on ``route`` with ``overlay`` opened on ``subject``, or the route's own."""
    session = Session()
    session.route = route
    for name, value in fields.items():
        setattr(session, name, value)
    if overlay is not None:
        open_overlay(session, overlay, subject=subject)
    else:
        session.subj_id = subject
    return session


def frame(
    session: Session,
    *,
    decisions: DecisionRecords | None = None,
    projection: RouteReadModel | None = None,
    size: int = 1,
) -> list[str]:
    """Return the composed frame of ``session`` drawn from what the test holds."""
    w, h = SIZES[size]
    view = View(
        session=session, fixture=chrome(), w=w, h=h, projection=projection, decisions=decisions
    )
    return compose_frame(view)


def press(
    session: Session,
    *keys: str,
    decisions: DecisionRecords | None = None,
    projection: RouteReadModel | None = None,
    attention: RouteProjection | None = None,
    link: Link | None = None,
    size: int = 1,
) -> None:
    """Draw the frame, then dispatch each key with the context the app would build."""
    w, h = SIZES[size]
    for key in keys:
        frame(session, decisions=decisions, projection=projection, size=size)
        ctx = Ctx(
            session=session,
            fixture=chrome(),
            host=Host(),
            w=w,
            h=h,
            projection=projection,
            send=link,
            attention=attention,
            decisions=decisions,
        )
        dispatch(ctx, key, False)


def row(rows: list[str], label: str) -> str:
    """Return the one frame row whose label gutter is ``label``."""
    found = [r for r in rows if r.startswith(f" {label} ") or r.startswith(f" {label}  ")]
    assert len(found) == 1, f"{label!r} labels {len(found)} rows"
    return found[0].rstrip()


def keys(rows: list[str]) -> list[str]:
    """Return the keybar's pairs as it prints them."""
    return rows[-1].rstrip().split("   ")


def text(rows: list[str]) -> str:
    """Return the frame as one string."""
    return "\n".join(rows)


def projection(route: str, document: dict[str, Any], *, cursor: int = 41208) -> RouteProjection:
    """Return ``route``'s projection over ``document``."""
    return build_route_projection(
        route=route, document=document, cursor=cursor, scope_id=SCOPE, generated_at=AT
    )


def step(step_id: str, *, passed: bool, evidence: tuple[str, ...]) -> dict[str, Any]:
    """Return one acceptance journey step citing ``evidence``."""
    return {
        "step_id": step_id,
        "passed": passed,
        "observation": f"{step_id} was observed",
        "evidence_kinds": ["artifact", "audit"] if evidence else ["artifact"],
        "evidence_refs": [f"{CONTAINER}/evidence/{key}" for key in evidence],
    }


def bundle(*steps: dict[str, Any]) -> MilestoneAcceptanceBundle:
    """Return one sealed acceptance bundle over ``steps``."""
    return MilestoneAcceptanceBundle.model_validate(
        {
            "milestone_ref": MILESTONE_URN,
            "revision": 1,
            "accepted_binding": {
                "head_sha": HEAD_SHA,
                "tree_sha": TREE_SHA,
                "contract_digest": canonical_digest("contract"),
                "policy_revision": 1,
                "evidence_digest": canonical_digest("evidence"),
            },
            "steps": list(steps),
            "sealed_at": AT.isoformat(),
        }
    )


def approval(sealed: MilestoneAcceptanceBundle) -> AcceptanceApproval:
    """Return the approval given to ``sealed``'s own digest."""
    return AcceptanceApproval(
        milestone_ref=MILESTONE_URN,  # type: ignore[arg-type]
        bundle_revision=sealed.revision,
        approved_digest=sealed.digest(),
        resolved_by={"principal_kind": "human", "principal_id": "OP-0001"},  # type: ignore[arg-type]
        receipt_ref=f"{CONTAINER}/evidence/EVD-0001",  # type: ignore[arg-type]
        accepted_binding=sealed.accepted_binding,
        approved_at=AT,
    )


def milestone_view(sealed: MilestoneAcceptanceBundle | None) -> Any:
    """Return the Milestone route's read model holding ``sealed``."""
    document = {
        "milestone": {
            "MLS-0030": {
                "urn": f"urn:eawf:{SCOPE}:milestone:MLS-0030",
                "revision": 4,
                "status": "ACTIVE",
            }
        }
    }
    return build_acceptance_view(projection("milestone", document), bundle=sealed)


def release_view(status: str, *, approved: bool = True) -> Any:
    """Return the Release route's read model for a release in ``status``."""
    document = {
        "release": {
            "REL-0001": {
                "urn": f"urn:eawf:{SCOPE}:release:REL-0001",
                "revision": 1,
                "status": status,
            }
        },
        "milestone": {
            "MLS-0030": {
                "urn": f"urn:eawf:{SCOPE}:milestone:MLS-0030",
                "revision": 4,
                "status": "COMPLETED",
            }
        },
    }
    sealed = bundle(step("AS-01", passed=True, evidence=("EVD-0002",)))
    return build_acceptance_view(
        projection("release", document),
        bundle=sealed,
        approval=approval(sealed) if approved else None,
    )
