"""A route that lists Milestones still shows one just compacted into its ledger.

``build_route_projection`` read only the live document, never the ledger a
terminal Milestone compacts into the instant ``domain.milestone.accept``
reaches a terminal status. A just-accepted Milestone vanished from every
route that lists Milestones -- ``scope.home`` among them -- on the same
commit that closed it, because nothing merged the ledger's freshly-closed
rows back in.

The walk that closes the Milestone is rebuilt here rather than a document
seeded by hand: reading its document straight after the walk is what
proves the row really left it, and reading the same tree's ``scope.home``
route through the daemon's own read verb is what proves the merge is
wired all the way through, not only true of ``build_route_projection`` in
isolation.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from unittest import mock

import pytest

from eawf.kernel.projection.compute import build_route_projection
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import CANONICAL_SEQUENCE_KEY
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
)
from tests.integration.workflow.release._canary_acceptance_walk import CanaryWalk, walk_canary

pytestmark = pytest.mark.integration

#: The canary Milestone the walk carries to acceptance.
MILESTONE_KEY: Final = "MLS-0001"


@pytest.fixture(scope="module")
def walked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CanaryWalk]:
    """Walk one canary Milestone to acceptance, once for the whole module."""
    base = tmp_path_factory.mktemp("terminal-rows-walk")
    scratch = base / "scratch"
    scratch.mkdir()
    with mock.patch.object(tempfile, "tempdir", str(scratch)):
        yield walk_canary(base / "repo", base / "runtime")


# ---------- the compaction this gate is about ----------


def test_the_accepted_milestone_leaves_the_live_document(walked: CanaryWalk) -> None:
    """Acceptance moves the terminal Milestone row out of the document."""
    document = read_document(document_path(walked.canary))
    assert MILESTONE_KEY not in document_rows(document, Epoch2Collection.MILESTONE)


def test_the_unmerged_document_only_read_omits_the_same_row(walked: CanaryWalk) -> None:
    """Without the merge, a projection of the same document reads it as gone."""
    document = read_document(document_path(walked.canary))

    projection = build_route_projection(
        route="scope.home",
        document=document,
        cursor=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        scope_id="canary",
        generated_at=datetime.now(UTC),
    )

    assert MILESTONE_KEY not in [row.key for row in projection.rows]


# ---------- the gate: the route wired to the fix shows it ----------


def test_scope_home_still_lists_the_just_accepted_milestone(
    walked: CanaryWalk, tmp_path: Path
) -> None:
    """The daemon's own read verb merges the ledger's terminal Milestone back in.

    The walk's own Milestone ledger already carries the acceptance-bundle
    revision it filed on the way to acceptance, under a different key
    grammar than the terminal Milestone row that follows it. A read that
    let that line through as if it were a Milestone row would fail
    outright -- a bundle payload states neither ``urn`` nor a Milestone
    ``status`` -- so this passing without error is itself proof the merge
    reads only the row it means to.
    """
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch("projection.scope.home.read", ctx, {"repo_root": str(walked.canary.root)})
    )

    milestones = {row["key"]: row for row in answer["rows"] if row["collection"] == "milestone"}
    assert MILESTONE_KEY in milestones
    assert milestones[MILESTONE_KEY]["urn"] == walked.milestone_urn
    assert milestones[MILESTONE_KEY]["status"]["value"] == "COMPLETED"


# ---------- the merge helper: boundary and error-path ----------


def test_build_route_projection_merges_a_ledger_row_the_document_lacks() -> None:
    """The empty boundary: no document row at all, filled entirely from the ledger."""
    document: dict[str, Any] = {Epoch2Collection.MILESTONE.value: {}}
    ledger_rows = {
        Epoch2Collection.MILESTONE: (
            {
                "key": "MLS-0001",
                "urn": "eawf://WSP-X/PRJ-X/REP-X/milestone/MLS-0001",
                "revision": 1,
                "status": "COMPLETED",
            },
        ),
    }

    projection = build_route_projection(
        route="scope.home",
        document=document,
        cursor=0,
        scope_id="canary",
        generated_at=datetime.now(UTC),
        ledger_rows=ledger_rows,
    )

    assert [row.key for row in projection.rows] == ["MLS-0001"]
    assert projection.rows[0].status.value == "COMPLETED"


def test_build_route_projection_never_lets_a_ledger_row_shadow_a_live_one() -> None:
    """The document always wins: a stale ledger snapshot never overrides it."""
    document: dict[str, Any] = {
        Epoch2Collection.MILESTONE.value: {
            "MLS-0002": {
                "key": "MLS-0002",
                "urn": "eawf://WSP-X/PRJ-X/REP-X/milestone/MLS-0002",
                "revision": 3,
                "status": "ACTIVE",
            },
        },
    }
    ledger_rows = {
        Epoch2Collection.MILESTONE: (
            {
                "key": "MLS-0002",
                "urn": "eawf://WSP-X/PRJ-X/REP-X/milestone/MLS-0002",
                "revision": 1,
                "status": "COMPLETED",
            },
        ),
    }

    projection = build_route_projection(
        route="scope.home",
        document=document,
        cursor=0,
        scope_id="canary",
        generated_at=datetime.now(UTC),
        ledger_rows=ledger_rows,
    )

    assert len(projection.rows) == 1
    assert projection.rows[0].revision == 3
    assert projection.rows[0].status.value == "ACTIVE"


def test_build_route_projection_with_no_ledger_rows_argument_reads_as_before() -> None:
    """The default is the unmerged read: an omitted argument changes nothing."""
    document: dict[str, Any] = {Epoch2Collection.MILESTONE.value: {}}

    projection = build_route_projection(
        route="scope.home",
        document=document,
        cursor=0,
        scope_id="canary",
        generated_at=datetime.now(UTC),
    )

    assert projection.rows == ()
