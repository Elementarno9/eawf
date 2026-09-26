"""A route lists the Milestones, Batches and Tasks the epoch-2 cutover imported.

The cutover wraps each epoch-1 record in a payload and states no ``urn`` and no
``revision`` beside it, so ``build_route_projection`` refused a whole route the
moment one imported row was in it -- ``scope.home`` on a freshly cut-over
repository drew nothing at all. The rows here come from a real cutover over a
corpus caught with work in flight, not from a hand-written document, so the
wrapper under test is exactly the one the importer writes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.compute import ProjectionRow, build_route_projection
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import MethodContext
from tests.integration.kernel.migration._cutover_harness import CutoverTree
from tests.integration.kernel.migration._legacy_continuation import (
    BACKLOG_ROW,
    ITER,
    PHASE,
    advance,
    document_path,
    method_context,
    seeded_applied_tree,
)

pytestmark = pytest.mark.integration

#: When every projection here is stamped; the rows do not depend on it.
AT: Final = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def tree(tmp_path: Path) -> CutoverTree:
    """Return a fresh cutover caught with work in flight."""
    return seeded_applied_tree(tmp_path / "repo")


def _document(tree: CutoverTree) -> dict[str, Any]:
    return read_document(document_path(tree))


def _row(route: str, document: dict[str, Any], key: str) -> ProjectionRow:
    projection = build_route_projection(
        route=route, document=document, cursor=0, scope_id="EAWF", generated_at=AT
    )
    (found,) = [row for row in projection.rows if row.key == key]
    return found


@pytest.mark.parametrize(
    ("route", "collection", "key"),
    [
        ("scope.home", Epoch2Collection.MILESTONE, PHASE),
        ("batch.detail", Epoch2Collection.BATCH, ITER),
        ("backlog", Epoch2Collection.TASK, BACKLOG_ROW),
    ],
    ids=["milestone", "batch", "task"],
)
def test_an_imported_row_projects_under_its_legacy_ref(
    tree: CutoverTree, route: str, collection: Epoch2Collection, key: str
) -> None:
    document = _document(tree)
    stored = document_rows(document, collection)[key]
    assert "urn" not in stored and "revision" not in stored, "the import states neither"

    row = _row(route, document, key)

    assert row.urn == f"legacy:{collection.value}/{key}"
    assert row.revision == 1
    assert row.status.value == stored["status"]


def test_a_continuation_move_advances_the_imported_revision(
    tree: CutoverTree, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    ctx: MethodContext = method_context(runtime)

    answer = advance(tree, ctx, key=BACKLOG_ROW, collection="task", to="DROPPED")
    assert answer["status"] == "ok", answer

    row = _row("backlog", _document(tree), BACKLOG_ROW)
    assert row.revision == 2
    assert row.status.value == "DROPPED"


def test_an_import_wrapper_that_does_not_validate_is_refused(tree: CutoverTree) -> None:
    document = _document(tree)
    document_rows(document, Epoch2Collection.MILESTONE)[PHASE]["payload"] = {"origin": "broken"}

    with pytest.raises(ValueError, match=f"legacy:milestone/{PHASE}"):
        build_route_projection(
            route="scope.home", document=document, cursor=0, scope_id="EAWF", generated_at=AT
        )


def test_a_native_row_stating_no_urn_is_still_refused(tree: CutoverTree) -> None:
    document = _document(tree)
    document_rows(document, Epoch2Collection.MILESTONE)[PHASE] = {"revision": 1, "status": "ACTIVE"}

    with pytest.raises(ValueError, match="states no urn"):
        build_route_projection(
            route="scope.home", document=document, cursor=0, scope_id="EAWF", generated_at=AT
        )
