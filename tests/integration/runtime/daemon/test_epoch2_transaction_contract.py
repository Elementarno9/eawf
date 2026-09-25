"""An accepted native mutation writes four things; a denied one writes none.

The contract of the seven-step path is countable. One accepted transition
leaves exactly one WAL record, exactly one rewrite of the generation
document, exactly one firehose row, and that row carries exactly one
``domain.<entity>.<verb>`` event name drawn from the closed vocabulary. A
denied edge leaves none of them: the denial is decided against the
re-read record, before the first byte is written.

The lock files a session takes are not counted as writes. They are the
mechanism that makes the count trustworthy and they live outside the
generation on purpose, so what is asserted here is the three artifacts and
the document bytes themselves.

A ledger-only mutation is counted the same way: one WAL record, one
sequence bump, one ledger line and one firehose row. The census at the end
is what keeps that true, by refusing any ledger append outside the store
and the transaction module.

A second census does the same for the generation document itself: outside
the store, the session, the commit paths and the two writers that build a
generation before it has authority, nothing names the document writer. The
create path is where it is proved to red, since a create that wrote its
record straight into the document is the shortcut the canary rehearsal
once had to take.
"""

from __future__ import annotations

import ast
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.epoch2.domain_events import DOMAIN_EVENT_NAMES
from eawf.kernel.state.epoch2.transitions import ObservedFact
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records, render_ledger_line
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection, StorageTier, tier_for
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.epoch2_recovery import LEDGER_LINE_KEY
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    LEDGER_EVENT_NAMES,
    TransactionRefusalCode,
    TransactionRefusedError,
    TransitionRequest,
    commit_ledger_append,
    run_transaction,
)
from eawf.runtime.daemon.methods.domain_envelope import (
    DOMAIN_TRANSITION_METHOD,
    ENTITY_REF_WIDTH,
    UNNAMED_SUBJECT,
    DomainErrorCode,
)
from eawf.runtime.daemon.wal import WalStatus, list_records, read_record
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    BATCH_URN,
    MILESTONE_URN,
    TASK_URN,
    document_path,
    firehose_path,
    legacy_task_row,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)

ACTOR = "OP-0001"
KEY = "req-0001"


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one planned Milestone."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"milestone": {"MLS-0030": seed_row("milestone", "PLANNED")}})
    return provisioned


@pytest.fixture
def context(canary: CanaryProvision, tmp_path: Path) -> Epoch2RootContext:
    """The native context of the canary, with a WAL directory of its own."""
    return root_context(canary, tmp_path / "runtime")


def _request(**overrides: Any) -> TransitionRequest:
    payload: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "to_status": "ACTIVE",
        "expected_revision": 1,
        "idempotency_key": KEY,
        "actor": ACTOR,
    }
    payload.update(overrides)
    return TransitionRequest.model_validate(payload)


def _firehose_rows(canary: CanaryProvision) -> list[dict[str, Any]]:
    path = firehose_path(canary)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_accepted_transition_writes_one_wal_record(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)

    records = list_records(context.wal_dir)
    assert len(records) == 1
    assert records[0].name.endswith(f".{WalStatus.FSYNCED.value}.json")
    assert read_record(records[0]).idempotency_key == KEY


def test_accepted_transition_mutates_the_document_once(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    document = read_document(document_path(canary))
    row = document["milestone"]["MLS-0030"]
    assert (row["status"], row["revision"]) == ("ACTIVE", 2)
    assert document[CANONICAL_SEQUENCE_KEY] == committed.receipt.canonical_sequence
    assert list(document["milestone"]) == ["MLS-0030"]


def test_accepted_transition_appends_one_firehose_row(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    committed = run_transaction(context=context, request=_request(), now=AT)

    rows = _firehose_rows(canary)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["name"] == "domain.milestone.activated"
    assert payload["name"] in DOMAIN_EVENT_NAMES
    assert payload["entity_ref"] == MILESTONE_URN
    assert (payload["from_status"], payload["to_status"]) == ("PLANNED", "ACTIVE")
    assert (payload["revision_before"], payload["revision_after"]) == (1, 2)
    assert payload["actor_ref"] == ACTOR
    assert payload["canonical_sequence"] == committed.receipt.canonical_sequence
    assert rows[0]["id"] == committed.receipt.event_id


def test_accepted_transition_returns_a_publishable_envelope(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """The envelope comes back unpublished so the caller publishes it unlocked."""
    committed = run_transaction(context=context, request=_request(), now=AT)

    assert isinstance(committed.envelope, Envelope)
    assert committed.receipt.event_name == "domain.milestone.activated"
    assert committed.receipt.revision_before == 1
    assert committed.receipt.revision_after == 2
    assert committed.receipt.canonical_sequence == 1


def test_denied_illegal_edge_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(to_status="COMPLETED"), now=AT)

    assert caught.value.code is TransactionRefusalCode.ILLEGAL_TRANSITION
    assert caught.value.revision == 1
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []


def test_denied_guard_writes_nothing(tmp_path: Path) -> None:
    """A merging Batch stays shut until the host merge is observed."""
    canary = provision(tmp_path / "batchrepo", code="BATCH")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "MERGING")}})
    context = root_context(canary, tmp_path / "runtime")
    before = document_path(canary).read_bytes()

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(urn=BATCH_URN, to_status="MERGED_PENDING_RECONCILIATION"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.TRANSITION_GUARD_FAILED
    assert caught.value.guard == "host_merge_observed"
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _firehose_rows(canary) == []


def test_presented_observation_admits_the_guarded_edge(tmp_path: Path) -> None:
    canary = provision(tmp_path / "batchrepo", code="BATCH")
    seed(canary, {"batch": {"BAT-0007": seed_row("batch", "MERGING")}})
    context = root_context(canary, tmp_path / "runtime")

    committed = run_transaction(
        context=context,
        request=_request(
            urn=BATCH_URN,
            to_status="MERGED_PENDING_RECONCILIATION",
            observations=(ObservedFact.HOST_MERGE_OBSERVED.value,),
        ),
        now=AT,
    )

    assert committed.receipt.event_name == "domain.batch.merge_observed"
    assert committed.receipt.event_name in DOMAIN_EVENT_NAMES
    assert len(_firehose_rows(canary)) == 1


def test_absent_record_is_refused_as_identity_not_found(tmp_path: Path) -> None:
    """A document holding no rows at all is the empty boundary of the re-read."""
    canary = provision(tmp_path / "bare", code="BARE")
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(), now=AT)

    assert caught.value.code is TransactionRefusalCode.IDENTITY_NOT_FOUND
    assert caught.value.revision is None
    assert list_records(context.wal_dir) == []


def test_stale_revision_is_refused_as_revision_conflict(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    """Off by one against the stored revision is a conflict, not an overwrite."""
    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(expected_revision=2), now=AT)

    assert caught.value.code is TransactionRefusalCode.REVISION_CONFLICT
    assert caught.value.revision == 1
    assert list_records(context.wal_dir) == []


def test_legacy_origin_record_is_refused_read_only(tmp_path: Path) -> None:
    canary = provision(tmp_path / "legacyrepo", code="LEGACY")
    seed(canary, {"task": {"EAWF-0042": legacy_task_row()}})
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(urn=TASK_URN, to_status="CLAIMED"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.LEGACY_IDENTITY_READ_ONLY
    assert list_records(context.wal_dir) == []


def test_missing_required_update_is_refused(tmp_path: Path) -> None:
    """A target status that makes a field a fact refuses a request without it."""
    canary = provision(tmp_path / "activerepo", code="ACTIVE")
    seed(canary, {"milestone": {"MLS-0030": seed_row("milestone", "ACTIVE")}})
    context = root_context(canary, tmp_path / "runtime")

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(
            context=context,
            request=_request(to_status="ACCEPTANCE_REVIEW"),
            now=AT,
        )

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert "acceptance_bundle_revision" in str(caught.value)
    assert list_records(context.wal_dir) == []


def test_unknown_status_token_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(to_status="SHIPPED"), now=AT)

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert list_records(context.wal_dir) == []


def test_entity_kind_without_a_machine_is_refused(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    evidence = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"

    with pytest.raises(TransactionRefusedError) as caught:
        run_transaction(context=context, request=_request(urn=evidence), now=AT)

    assert caught.value.code is TransactionRefusalCode.IDENTITY_KIND_MISMATCH
    assert list_records(context.wal_dir) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"urn": "not-a-urn"}, id="unparsable-urn"),
        pytest.param({"expected_revision": 0}, id="revision-below-one"),
        pytest.param({"idempotency_key": ""}, id="empty-idempotency-key"),
        pytest.param({"actor": "not a principal"}, id="free-text-actor"),
        pytest.param({"unknown": "field"}, id="unknown-field"),
    ],
)
def test_request_parameters_are_strict(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _request(**overrides)


def test_rpc_commits_and_publishes_after_the_commit(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    published: list[Envelope] = []
    ctx.bus.publish = published.append  # type: ignore[method-assign]

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "ACTIVE",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["status"] == "ok"
    assert answer["errors"] == []
    assert answer["result"]["event_name"] == "domain.milestone.activated"
    assert (answer["revision_before"], answer["revision_after"]) == (1, 2)
    assert [envelope.payload["name"] for envelope in published] == ["domain.milestone.activated"]
    assert len(_firehose_rows(canary)) == 1


def test_rpc_returns_an_error_envelope_for_a_denied_edge(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")
    before = document_path(canary).read_bytes()

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": MILESTONE_URN,
                "to_status": "COMPLETED",
                "expected_revision": 1,
                "idempotency_key": KEY,
                "actor": ACTOR,
            },
        )
    )

    assert answer["status"] == "error"
    assert answer["result"] is None
    assert answer["errors"][0]["code"] == DomainErrorCode.ILLEGAL_TRANSITION.value
    assert answer["errors"][0]["entity_ref"] == MILESTONE_URN
    assert answer["revision_before"] == answer["revision_after"] == 1
    assert document_path(canary).read_bytes() == before
    assert _firehose_rows(canary) == []


def test_rpc_refuses_an_unparsable_request_without_leaking_values(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    ctx = method_context(tmp_path / "runtime")

    answer = asyncio.run(
        methods.dispatch(
            DOMAIN_TRANSITION_METHOD,
            ctx,
            {"repo_root": str(canary.root), "urn": MILESTONE_URN, "to_status": "ACTIVE"},
        )
    )

    assert answer["status"] == "error"
    row = answer["errors"][0]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert "expected_revision" in row["message"]
    assert str(tmp_path) not in row["message"]


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        pytest.param(None, UNNAMED_SUBJECT, id="no-urn"),
        pytest.param("", UNNAMED_SUBJECT, id="empty-urn"),
        pytest.param(7, UNNAMED_SUBJECT, id="non-string-urn"),
        pytest.param("x" * (ENTITY_REF_WIDTH + 1), "x" * ENTITY_REF_WIDTH, id="oversized-urn"),
    ],
)
def test_rpc_bounds_the_subject_of_an_unparsable_request(
    canary: CanaryProvision, tmp_path: Path, named: Any, expected: str
) -> None:
    """An oversized or missing subject must not make the refusal unbuildable."""
    ctx = method_context(tmp_path / "runtime")
    params: dict[str, Any] = {"repo_root": str(canary.root)}
    if named is not None:
        params["urn"] = named

    answer = asyncio.run(methods.dispatch(DOMAIN_TRANSITION_METHOD, ctx, params))

    assert answer["status"] == "error"
    assert answer["errors"][0]["entity_ref"] == expected


def test_the_transition_method_is_registered_on_the_server() -> None:
    """Importing the server registers the native transition verb."""
    import eawf.runtime.daemon.server  # noqa: F401

    assert DOMAIN_TRANSITION_METHOD in methods.registered_methods()


# ---- ledger-only mutations ----------------------------------------------------

#: The package the census walks.
PACKAGE_ROOT: Final = Path(__file__).resolve().parents[4] / "src" / "eawf"

#: The ledger-line writers the census forbids. ``guarded_ledger_write`` is
#: not one of them: it replaces a whole append-only file and is the
#: migration journal's writer, not a native mutation's.
LEDGER_WRITERS: Final = frozenset(
    {"append_ledger_record", "append_ledger_record_once", "append_correction"}
)

#: Where a writer may be named outside the store, relative to the package.
#: The transaction is the one commit path; the replay finishes a line the
#: transaction journalled, and only through the idempotent writer.
ALLOWED_WRITERS: Final = {
    "runtime/daemon/epoch2_transaction.py": frozenset({"append_ledger_record"}),
    "runtime/daemon/epoch2_recovery.py": frozenset({"append_ledger_record_once"}),
}

#: The store package, which owns the writers and may name them freely.
STORE_PACKAGE: Final = "kernel/store/"

RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"


def direct_ledger_appends(package_root: Path) -> list[str]:
    """Return every place under *package_root* that names a ledger writer.

    A name is flagged wherever it appears -- imported, called, aliased or
    passed along as a value -- because each of those is a route to an
    append that skips the transaction.

    Returns:
        ``<relative path>:<line> <name>`` for each finding, in path order.
    """
    findings: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        if relative.startswith(STORE_PACKAGE):
            continue
        allowed = ALLOWED_WRITERS.get(relative, frozenset())
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            for name in names:
                if name in LEDGER_WRITERS and name not in allowed:
                    findings.append(f"{relative}:{node.lineno} {name}")
    return findings


def _seeded_package(tmp_path: Path, *, addition: str) -> Path:
    """Copy the real delivery verb module into a scratch package and append to it."""
    relative = Path("runtime/daemon/methods/delivery.py")
    source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
    target = tmp_path / "eawf" / relative
    target.parent.mkdir(parents=True)
    target.write_text(f"{source}\n{addition}", encoding="utf-8")
    return tmp_path / "eawf"


def _run_line(key: str = "HLO-1", **payload: Any) -> LedgerRecord:
    return LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=key,
        status="accepted",
        recorded_at=AT,
        payload={"payload_kind": "probe", **payload},
    )


def _run_ledger_lines(canary: CanaryProvision) -> tuple[LedgerRecord, ...]:
    return read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))


def test_census_finds_no_direct_ledger_append() -> None:
    assert PACKAGE_ROOT.is_dir()
    assert direct_ledger_appends(PACKAGE_ROOT) == []


def test_census_passes_the_unseeded_delivery_module(tmp_path: Path) -> None:
    """The seed below is what reds the census, not the module it lands in."""
    assert direct_ledger_appends(_seeded_package(tmp_path, addition="")) == []


@pytest.mark.parametrize(
    "addition",
    [
        pytest.param(
            "from eawf.kernel.store.ledger import append_ledger_record\n\n\n"
            "def _seeded(path, record):\n    append_ledger_record(path, record)\n",
            id="direct-call",
        ),
        pytest.param(
            "from eawf.kernel.store.ledger import append_ledger_record as put\n",
            id="aliased-import",
        ),
        pytest.param(
            "from eawf.kernel.store import ledger\n\n\n"
            "def _seeded(path, record):\n    ledger.append_correction(path, record)\n",
            id="module-attribute",
        ),
    ],
)
def test_census_reds_on_a_seeded_delivery_append(tmp_path: Path, addition: str) -> None:
    findings = direct_ledger_appends(_seeded_package(tmp_path, addition=addition))

    assert findings
    assert all(item.startswith("runtime/daemon/methods/delivery.py:") for item in findings)


def test_ledger_append_writes_one_intent_one_line_and_one_row(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    record = _run_line()

    with context.session([RUN_URN]) as session:
        envelope = commit_ledger_append(session, record)

    records = list_records(context.wal_dir)
    assert [path.name.endswith(f".{WalStatus.FSYNCED.value}.json") for path in records] == [True]
    assert _run_ledger_lines(canary) == (record,)
    rows = _firehose_rows(canary)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["name"] == "ledger.run.appended"
    assert payload["name"] in LEDGER_EVENT_NAMES
    assert payload["name"] not in DOMAIN_EVENT_NAMES
    assert payload[LEDGER_LINE_KEY] == render_ledger_line(record)
    assert payload["entity_refs"] == [RUN_URN]
    assert rows[0]["id"] == envelope.id
    assert read_record(records[0]).envelope.id == envelope.id
    assert read_document(document_path(canary))[CANONICAL_SEQUENCE_KEY] == 1


def test_ledger_append_shares_the_sequence_with_transitions(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    run_transaction(context=context, request=_request(), now=AT)
    with context.session([RUN_URN]) as session:
        envelope = commit_ledger_append(session, _run_line())
    committed = run_transaction(
        context=context,
        request=_request(to_status="CANCELLED", expected_revision=2, idempotency_key="req-2"),
        now=AT,
    )

    assert envelope.payload["canonical_sequence"] == 2
    assert committed.receipt.canonical_sequence == 3
    assert [row["payload"]["canonical_sequence"] for row in _firehose_rows(canary)] == [1, 2, 3]


def test_leaky_ledger_append_writes_nothing(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    before = document_path(canary).read_bytes()

    with context.session([RUN_URN]) as session, pytest.raises(TransactionRefusedError) as caught:
        commit_ledger_append(session, _run_line(note="ghp_" + "a" * 36))

    assert caught.value.code is TransactionRefusalCode.SCHEMA_VALIDATION_FAILED
    assert "ghp_" not in caught.value.detail
    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []
    assert _run_ledger_lines(canary) == ()
    assert _firehose_rows(canary) == []


def test_ledger_append_refuses_a_collection_without_a_ledger(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    unledgered = next(
        collection
        for collection in Epoch2Collection
        if tier_for(collection) is not StorageTier.LEDGER
    )
    record = LedgerRecord.model_construct(
        collection=unledgered,
        record_key="KEY-1",
        status="noted",
        recorded_at=AT,
        payload={},
    )
    before = document_path(canary).read_bytes()

    with context.session([RUN_URN]) as session, pytest.raises(ValueError, match="no ledger"):
        commit_ledger_append(session, record)

    assert document_path(canary).read_bytes() == before
    assert list_records(context.wal_dir) == []


def test_ledger_append_refuses_a_closed_session(
    context: Epoch2RootContext, canary: CanaryProvision
) -> None:
    with context.session([RUN_URN]) as session:
        pass

    with pytest.raises(RuntimeError, match="closed"):
        commit_ledger_append(session, _run_line())

    assert list_records(context.wal_dir) == []
    assert _run_ledger_lines(canary) == ()


# ---- generation-document writes ---------------------------------------------

#: The document writer the census forbids: the store's own function and the
#: session method that wraps it share the name, so one spelling covers both.
DOCUMENT_WRITERS: Final = frozenset({"write_document"})

#: Where a document writer may be named outside the store, and why.
ALLOWED_DOCUMENT_WRITERS: Final = frozenset(
    {
        # The session method every native commit writes through.
        "runtime/daemon/epoch2_root.py",
        # The one commit path of a transition, a ledger line and a create.
        "runtime/daemon/epoch2_transaction.py",
        # The plan-revision commit, which journals its own WAL intent.
        "workflow/planning/apply.py",
        # A canary's empty born generation, written before it has authority.
        "platform/install/canary.py",
        # The cutover's projection of the epoch-1 corpus into a new generation.
        "kernel/migration/epoch2/cutover.py",
    }
)

#: The create path the seeded write lands in.
CREATE_MODULE: Final = Path("runtime/daemon/epoch2_create.py")


def direct_document_writes(package_root: Path) -> list[str]:
    """Return every place under *package_root* that names a document writer.

    A create that wrote the generation document itself would skip the WAL
    intent, the receipt, the sequence and the event, which is exactly the
    shortcut the canary rehearsal once had to take.

    Returns:
        ``<relative path>:<line> <name>`` for each finding, in path order.
    """
    findings: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        if relative.startswith(STORE_PACKAGE) or relative in ALLOWED_DOCUMENT_WRITERS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            findings.extend(
                f"{relative}:{node.lineno} {name}" for name in names if name in DOCUMENT_WRITERS
            )
    return findings


def _seeded_create_package(tmp_path: Path, *, addition: str) -> Path:
    """Copy the real create module into a scratch package and append to it."""
    source = (PACKAGE_ROOT / CREATE_MODULE).read_text(encoding="utf-8")
    target = tmp_path / "eawf" / CREATE_MODULE
    target.parent.mkdir(parents=True)
    target.write_text(f"{source}\n{addition}", encoding="utf-8")
    return tmp_path / "eawf"


def test_census_finds_no_direct_document_write() -> None:
    assert (PACKAGE_ROOT / CREATE_MODULE).is_file()
    assert direct_document_writes(PACKAGE_ROOT) == []


def test_census_passes_the_unseeded_create_module(tmp_path: Path) -> None:
    """The seed below is what reds the census, not the module it lands in."""
    assert direct_document_writes(_seeded_create_package(tmp_path, addition="")) == []


@pytest.mark.parametrize(
    "addition",
    [
        pytest.param(
            "from eawf.kernel.store.compaction import write_document\n\n\n"
            "def _seeded(path, document):\n    write_document(path, document)\n",
            id="store-writer",
        ),
        pytest.param(
            "def _seeded(session, document):\n    session.write_document(document)\n",
            id="session-writer",
        ),
        pytest.param(
            "from eawf.kernel.store import compaction\n\n\n_WRITE = compaction.write_document\n",
            id="writer-as-value",
        ),
    ],
)
def test_census_reds_on_a_direct_write_seeded_into_the_create_path(
    tmp_path: Path, addition: str
) -> None:
    findings = direct_document_writes(_seeded_create_package(tmp_path, addition=addition))

    assert findings
    assert all(item.startswith(f"{CREATE_MODULE.as_posix()}:") for item in findings)
