"""A route read model, computed daemon-side at the cursor the commit allocated.

A console that projects the document itself cannot say which cursor its answer
stands at, because the only workspace-global order is allocated inside the
committing transaction and never leaves the daemon. ``projection.<route>.read``
closes that: it reads the selected generation's document, renders the route's rows,
and heads them with a projection whose ``source_cursor`` is the committed
``canonical_sequence`` -- the same number the receipt of the last commit carried.

The suite drives the real registered verbs through the daemon dispatcher against a
provisioned canary, so what it reads is what a client on the socket reads. It pins
four things: the cursor agrees with the receipt, an unread workspace reads as cursor
zero rather than as an error, one cursor yields one digest however often it is read,
and a row the document cannot describe is refused rather than rendered as a blank.
"""

from __future__ import annotations

import asyncio
import copy
import tempfile
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.projection.compute import (
    MISSING_STATUS_REASON,
    ROUTE_COLLECTIONS,
    build_route_projection,
)
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.truth import Completeness, ConnectionState, TruthState
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    MethodContext,
    MethodNotFoundError,
    dispatch,
)
from eawf.runtime.daemon.methods.projection import ROUTE_READ_METHODS
from eawf.runtime.daemon.native_guard import NativeAuthorityRefusedError
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    rekeyed,
    root_context,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR = "OP-0001"

#: The route the suite reads. It renders two collections, so a projection of it
#: also proves the rows of several collections land in one read model.
ROADMAP_ROUTE = "roadmap"

#: How the roadmap route's read verb is spelled on the wire.
ROADMAP_READ = "projection.roadmap.read"

#: Milestone keys the canary is seeded with.
KEYS = ("MLS-0030", "MLS-0031", "MLS-0032")


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one planned Milestone per seeded key."""
    provisioned = provision(tmp_path / "repo", code="READ")
    planned = seed_row("milestone", "PLANNED")
    seed(provisioned, {"milestone": {key: rekeyed(planned, key=key) for key in KEYS}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def _urn_for(key: str) -> str:
    return f"{MILESTONE_URN.rsplit('/', 1)[0]}/{key}"


def _activate(canary: CanaryProvision, tmp_path: Path, key: str) -> int:
    """Commit one Milestone activation and return the ordinal it allocated."""
    committed = run_transaction(
        context=root_context(canary, tmp_path / "runtime"),
        request=TransitionRequest.model_validate(
            {
                "urn": _urn_for(key),
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": f"req-{key}",
                "actor": ACTOR,
            }
        ),
        now=AT,
    )
    return committed.receipt.canonical_sequence


def _read(ctx: MethodContext, canary: CanaryProvision, method: str = ROADMAP_READ) -> Any:
    """Dispatch one route read exactly as a client on the socket would."""
    return asyncio.run(dispatch(method, ctx, {"repo_root": str(canary.root)}))


def _document(canary: CanaryProvision) -> dict[str, Any]:
    return read_document(document_path(canary))


def test_read_source_cursor_equals_the_committed_canonical_sequence(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    """The header states the ordinal the last commit's receipt carried."""
    _activate(canary, tmp_path, KEYS[0])
    allocated = _activate(canary, tmp_path, KEYS[1])

    header = _read(ctx, canary)["header"]

    assert header["source_cursor"] == str(allocated)
    assert header["projection_kind"] == ReadModelKind.ROADMAP_VIEW.value
    assert header["connection_state"] == ConnectionState.LIVE.value
    assert header["completeness"] == Completeness.COMPLETE.value


def test_read_of_a_workspace_that_has_committed_nothing_is_cursor_zero(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The empty boundary reads as cursor zero, never as a refusal."""
    assert CANONICAL_SEQUENCE_KEY not in _document(canary)

    projection = _read(ctx, canary)

    assert projection["header"]["source_cursor"] == "0"
    assert projection["header"]["projection_revision"] == 1


def test_read_renders_every_row_of_the_routes_collections(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    """The roadmap route renders both of the collections it binds, keys sorted."""
    _activate(canary, tmp_path, KEYS[0])

    rows = _read(ctx, canary)["rows"]

    assert [row["key"] for row in rows] == list(KEYS)
    assert {row["collection"] for row in rows} == {Epoch2Collection.MILESTONE.value}
    assert rows[0]["status"]["value"] == "ACTIVE"
    assert rows[1]["status"]["value"] == "PLANNED"


def test_one_cursor_yields_one_digest(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    """Two reads at one cursor digest alike; a commit moves cursor and digest."""
    _activate(canary, tmp_path, KEYS[0])
    first, second = _read(ctx, canary), _read(ctx, canary)

    assert first["digest"] == second["digest"]
    assert first["header"]["generated_at"] != "" and first["digest"].startswith("sha256:")

    _activate(canary, tmp_path, KEYS[1])
    later = _read(ctx, canary)

    assert later["header"]["source_cursor"] != first["header"]["source_cursor"]
    assert later["digest"] != first["digest"]


def test_every_bound_route_reads_at_the_same_cursor(
    ctx: MethodContext, canary: CanaryProvision, tmp_path: Path
) -> None:
    """One cursor is workspace-global, so every route reports the same one."""
    allocated = _activate(canary, tmp_path, KEYS[0])

    cursors = {
        method: _read(ctx, canary, method)["header"]["source_cursor"]
        for method in ROUTE_READ_METHODS
    }

    assert set(cursors.values()) == {str(allocated)}
    assert len(cursors) == len(ROUTE_COLLECTIONS)


def test_a_row_that_states_no_status_renders_unknown_not_blank(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A missing value carries its reason; a blank would read as no status at all."""
    path = document_path(canary)
    document = read_document(path)
    del document["milestone"][KEYS[0]]["status"]
    write_document(path, document)

    row = _read(ctx, canary)["rows"][0]

    assert row["status"]["state"] == TruthState.UNKNOWN.value
    assert row["status"]["value"] is None
    assert row["status"]["missing_reason"] == MISSING_STATUS_REASON


def test_read_refuses_a_document_whose_high_water_mark_is_not_an_ordinal(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A cursor that is not a number cannot head a projection."""
    path = document_path(canary)
    document = read_document(path)
    document[CANONICAL_SEQUENCE_KEY] = "seven"
    write_document(path, document)

    with pytest.raises(DaemonValidationError, match="projection_unreadable"):
        _read(ctx, canary)


def test_read_refuses_a_document_row_it_cannot_address(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A row with no URN is a defect, not a row rendered with holes."""
    path = document_path(canary)
    document = read_document(path)
    del document["milestone"][KEYS[0]]["urn"]
    write_document(path, document)

    with pytest.raises(DaemonValidationError, match="states no urn"):
        _read(ctx, canary)


def test_read_refuses_a_tree_that_holds_no_native_authority(
    ctx: MethodContext, tmp_path: Path
) -> None:
    """An epoch-1 tree has no document to project, so the fence refuses first."""
    plain = tmp_path / "plain"
    (plain / ".ea").mkdir(parents=True)

    with pytest.raises(NativeAuthorityRefusedError):
        asyncio.run(dispatch(ROADMAP_READ, ctx, {"repo_root": str(plain)}))


def test_an_unbound_route_has_no_read_verb(ctx: MethodContext, canary: CanaryProvision) -> None:
    """A route with no document binding is not found, never answered empty."""
    assert "settings" not in ROUTE_COLLECTIONS

    with pytest.raises(MethodNotFoundError):
        _read(ctx, canary, "projection.settings.read")


def test_build_route_projection_refuses_an_unbound_route() -> None:
    with pytest.raises(ValueError, match="renders no epoch-2 collection"):
        build_route_projection(
            route="settings",
            document={},
            cursor=0,
            scope_id="root-0000000000000000",
            generated_at=AT,
        )


def test_build_route_projection_refuses_a_negative_cursor() -> None:
    """Off-by-one below the floor: zero is the unread workspace, minus one is not."""
    with pytest.raises(ValueError, match="never -1"):
        build_route_projection(
            route=ROADMAP_ROUTE,
            document={},
            cursor=-1,
            scope_id="root-0000000000000000",
            generated_at=AT,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: "not an object", "not an object"),
        (lambda row: {k: v for k, v in row.items() if k != "urn"}, "states no urn"),
        (lambda row: {**row, "revision": 0}, "states no positive revision"),
        (lambda row: {**row, "revision": "two"}, "states no positive revision"),
    ],
)
def test_build_route_projection_refuses_a_row_it_cannot_render(mutate: Any, message: str) -> None:
    """Each way a stored row fails to describe itself is refused by name."""
    row = copy.deepcopy(seed_row("milestone", "PLANNED"))

    with pytest.raises(ValueError, match=message):
        build_route_projection(
            route=ROADMAP_ROUTE,
            document={"milestone": {"MLS-0030": mutate(row)}},
            cursor=1,
            scope_id="root-0000000000000000",
            generated_at=AT,
        )


def test_build_route_projection_refuses_a_row_keyed_by_a_blank() -> None:
    """A blank key selects nothing, so a patch could never reach the row."""
    with pytest.raises(ValueError, match="keyed by a blank"):
        build_route_projection(
            route=ROADMAP_ROUTE,
            document={"milestone": {"  ": seed_row("milestone", "PLANNED")}},
            cursor=1,
            scope_id="root-0000000000000000",
            generated_at=AT,
        )


def test_build_route_projection_of_an_empty_document_is_an_empty_projection() -> None:
    """The empty boundary is a complete projection of zero rows, not a refusal."""
    projection = build_route_projection(
        route=ROADMAP_ROUTE,
        document={},
        cursor=0,
        scope_id="root-0000000000000000",
        generated_at=AT,
    )

    assert projection.rows == ()
    assert projection.header.source_cursor == "0"
    assert projection.header.completeness is Completeness.COMPLETE
