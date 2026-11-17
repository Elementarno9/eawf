"""Head checks compare a recorded commit through the committed record of rewrite pairs.

A history rewrite in place gives every commit a new hash; its rebind records
each applied old-to-new pair in ``.ea/store/rewrite_map.jsonl``. The document
keeps old hashes by design, so each head check resolves a recorded commit to
the one it became before comparing: a pinned head whose pair names the
observed head passes, and an unpaired head that differs still fails with its
own code.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.batch import BatchStatus, DeliveryBatch
from eawf.kernel.state.epoch2.plan_revision import PlanRevision
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.transitions import LifecycleEntity
from eawf.runtime.daemon import batch_close
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
    delivery_acceptance,
    delivery_completion,
    delivery_landed,
    delivery_rebind,
    domain_guards,
)
from eawf.runtime.daemon.methods.delivery_acceptance import MergeReconcileParams
from eawf.runtime.daemon.methods.delivery_completion import (
    COMPLETION_COMMIT_MISMATCH,
    completion_binding,
)
from eawf.runtime.daemon.methods.delivery_landed import ReadBackParams, read_back_merge
from eawf.runtime.daemon.methods.domain_guards import GuardInputs
from eawf.workflow.integration import rebind
from eawf.workflow.integration.rebind import (
    REWRITE_MAP_INVALID,
    RewriteMapError,
    RewritePair,
    append_rewrite_pairs,
    resolve_rewritten,
    rewrite_map_path,
)
from eawf.workflow.integration.reconcile import (
    HostMergeObservation,
    MergeOutcome,
    reconcile_merge,
)
from eawf.workflow.planning.revision import (
    ObservedPlanWorld,
    PlanRefusalCode,
    detect_plan_drift,
)
from tests.unit.kernel.state._epoch2_transition_fixtures import seed_record

OLD, MID, NEW, OTHER = "1" * 40, "2" * 40, "3" * 40, "4" * 40
AT = datetime(2026, 11, 1, tzinfo=UTC)


def _record(tree_root: Path, *pairs: tuple[str, str]) -> None:
    append_rewrite_pairs(
        rewrite_map_path(tree_root), (RewritePair(old=o, new=n, applied_at=AT) for o, n in pairs)
    )


@pytest.fixture
def tree_root(tmp_path: Path) -> Path:
    """Return a ``.ea`` directory holding no record yet."""
    root = tmp_path / ".ea"
    root.mkdir()
    return root


# --- resolve_rewritten --------------------------------------------------------


def test_resolve_returns_the_input_without_a_record(tree_root: Path) -> None:
    assert resolve_rewritten(tree_root, OLD) == OLD


def test_resolve_returns_the_input_for_an_empty_record(tree_root: Path) -> None:
    path = rewrite_map_path(tree_root)
    path.parent.mkdir()
    path.write_bytes(b"")
    assert resolve_rewritten(tree_root, OLD) == OLD


def test_resolve_maps_a_single_pair_and_leaves_other_commits(tree_root: Path) -> None:
    _record(tree_root, (OLD, NEW))
    assert resolve_rewritten(tree_root, OLD) == NEW
    assert resolve_rewritten(tree_root, NEW) == NEW
    assert resolve_rewritten(tree_root, OTHER) == OTHER


@pytest.mark.parametrize(
    "pairs", [((OLD, MID), (MID, NEW)), ((MID, NEW), (OLD, MID))], ids=["in-order", "reversed"]
)
def test_resolve_follows_a_chain_to_its_end(
    tree_root: Path, pairs: tuple[tuple[str, str], ...]
) -> None:
    _record(tree_root, *pairs)
    assert resolve_rewritten(tree_root, OLD) == NEW
    assert resolve_rewritten(tree_root, MID) == NEW


def test_resolve_maps_many_old_commits_to_one(tree_root: Path) -> None:
    _record(tree_root, (OLD, NEW), (MID, NEW))
    assert {resolve_rewritten(tree_root, sha) for sha in (OLD, MID)} == {NEW}


def test_resolve_takes_the_later_of_two_pairs_for_one_old_commit(tree_root: Path) -> None:
    _record(tree_root, (OLD, MID), (OLD, NEW))
    assert resolve_rewritten(tree_root, OLD) == NEW


@pytest.mark.parametrize(
    ("pairs", "line"),
    [
        (((OLD, OLD),), 1),
        (((OLD, MID), (MID, OLD)), 1),
        (((OTHER, OLD), (OLD, MID), (MID, OLD)), 2),
    ],
    ids=["self", "two-cycle", "tail-into-cycle"],
)
def test_resolve_refuses_a_cycle(
    tree_root: Path, pairs: tuple[tuple[str, str], ...], line: int
) -> None:
    _record(tree_root, *pairs)
    with pytest.raises(RewriteMapError, match=REWRITE_MAP_INVALID) as caught:
        resolve_rewritten(tree_root, OTHER)
    assert caught.value.line == line


_STAMP = '"applied_at": "2026-11-01T00:00:00Z"'
_GOOD_LINE = f'{{"old": "{MID}", "new": "{NEW}", {_STAMP}}}\n'


@pytest.mark.parametrize(
    "line",
    [
        f'{{"old": "{OLD}", "new": "{NEW}"}}\n'.encode(),
        f'{{"old": "{OLD}", "new": "{NEW}", {_STAMP}, "x": 1}}\n'.encode(),
        f'{{"old": "{OLD[:39]}", "new": "{NEW}", {_STAMP}}}\n'.encode(),
        b"not json\n",
        f'{{"old": "{OLD}", "new'.encode(),
        b'{"old": "\xff"}\n',
    ],
    ids=["missing-key", "extra-key", "short-sha", "not-json", "torn", "not-utf8"],
)
def test_resolve_refuses_a_malformed_line_naming_it(tree_root: Path, line: bytes) -> None:
    path = rewrite_map_path(tree_root)
    path.parent.mkdir()
    path.write_bytes(_GOOD_LINE.encode() + line)
    with pytest.raises(RewriteMapError, match=f"{REWRITE_MAP_INVALID}: line 2") as caught:
        resolve_rewritten(tree_root, OLD)
    assert caught.value.line == 2
    assert isinstance(caught.value.__cause__, ValidationError)


# --- plan approval: head-binding freshness ------------------------------------

_REPOSITORY = "eawf://EAWF/repository/EAWF"


def _approved(head: str) -> PlanRevision:
    """Return an approved revision pinning *head*, drift checked only from the heads on."""
    binding = SimpleNamespace(repository_ref=_REPOSITORY, head_sha=head)
    return cast(
        PlanRevision,
        SimpleNamespace(
            approval=object(),
            base_state_revision=3,
            policy_revision=1,
            head_bindings=(binding,),
            revision=5,
            key="PRV-0001",
            status=SimpleNamespace(value="APPROVED"),
        ),
    )


def _world(head: str) -> ObservedPlanWorld:
    return ObservedPlanWorld(
        track_revision=3,
        track_status="ACTIVE",
        policy_revision=1,
        heads={_REPOSITORY: head},
        claimed_keys=(),
    )


@pytest.fixture
def content_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the content binding hold, so the head binding decides."""
    monkeypatch.setattr("eawf.workflow.planning.revision.approval_binds_content", lambda *_a: None)


@pytest.mark.usefixtures("content_bound")
def test_freshness_passes_a_pinned_head_whose_pair_is_the_observed_head(tree_root: Path) -> None:
    _record(tree_root, (OLD, NEW))
    resolve = lambda sha: resolve_rewritten(tree_root, sha)  # noqa: E731
    assert detect_plan_drift(_approved(OLD), world=_world(NEW), resolve_head=resolve) is None


@pytest.mark.usefixtures("content_bound")
@pytest.mark.parametrize("pairs", [(), ((OLD, OTHER),)], ids=["unpaired", "paired-elsewhere"])
def test_freshness_still_refuses_a_moved_head_with_proof_stale(
    tree_root: Path, pairs: tuple[tuple[str, str], ...]
) -> None:
    _record(tree_root, *pairs)
    resolve = lambda sha: resolve_rewritten(tree_root, sha)  # noqa: E731
    refusal = detect_plan_drift(_approved(OLD), world=_world(NEW), resolve_head=resolve)
    assert refusal is not None
    assert (refusal.code, refusal.guard) == (
        PlanRefusalCode.PROOF_STALE,
        "base_head_bindings_pinned",
    )


@pytest.mark.usefixtures("content_bound")
def test_freshness_without_a_resolver_compares_heads_as_recorded() -> None:
    refusal = detect_plan_drift(_approved(OLD), world=_world(NEW))
    assert refusal is not None and refusal.code is PlanRefusalCode.PROOF_STALE
    assert detect_plan_drift(_approved(OLD), world=_world(OLD)) is None


@pytest.mark.usefixtures("content_bound")
def test_freshness_refuses_an_unobserved_head(tree_root: Path) -> None:
    world = ObservedPlanWorld(
        track_revision=3, track_status="ACTIVE", policy_revision=1, heads={}, claimed_keys=()
    )
    resolve = lambda sha: resolve_rewritten(tree_root, sha)  # noqa: E731
    refusal = detect_plan_drift(_approved(OLD), world=world, resolve_head=resolve)
    assert refusal is not None and refusal.code is PlanRefusalCode.PROOF_STALE


# --- delivery completion: the named commit ------------------------------------


def _judged(monkeypatch: pytest.MonkeyPatch, head: str) -> None:
    """Stand a completable judgment on a generation whose head is *head*."""
    judgment = SimpleNamespace(
        head=SimpleNamespace(
            integrated_revision=SimpleNamespace(head_sha=head, tree_sha="b" * 40),
            batch_ref=SimpleNamespace(entity_key="BAT-0001"),
            generation=2,
        ),
        decision=SimpleNamespace(plan=SimpleNamespace(reused=())),
        filed=frozenset(),
        contracts=SimpleNamespace(model_dump=lambda **_k: {}),
        task=SimpleNamespace(contract_revision=1, urn=SimpleNamespace(entity_key="EAWF-0001")),
    )
    monkeypatch.setattr(delivery_completion, "_judge_completion", lambda *_a: judgment)
    monkeypatch.setattr(
        delivery_completion, "_completion_answer", lambda _j: SimpleNamespace(completable=True)
    )


def _context(tree_root: Path) -> Any:
    return SimpleNamespace(identity=SimpleNamespace(tree_root=tree_root))


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record each read of the record of rewrite pairs the daemon verbs make."""
    reads: list[Path] = []
    real = rebind.rewritten_commits

    def counted(tree_root: Path) -> dict[str, str]:
        reads.append(tree_root)
        return real(tree_root)

    monkeypatch.setattr(delivery_rebind, "rewritten_commits", counted)
    return reads


def test_completion_binds_the_new_head_for_a_recorded_old_one(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    _judged(monkeypatch, OLD)
    args: Any = SimpleNamespace(receipts=())

    _answer, binding = completion_binding(_context(tree_root), args, integrated_commit=NEW)

    assert binding is not None and binding.head_sha == NEW


@pytest.mark.parametrize("pairs", [(), ((OLD, OTHER),)], ids=["unpaired", "paired-elsewhere"])
def test_completion_still_refuses_a_differing_commit(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch, pairs: tuple[tuple[str, str], ...]
) -> None:
    _record(tree_root, *pairs)
    _judged(monkeypatch, OLD)
    args: Any = SimpleNamespace(receipts=())

    with pytest.raises(DaemonValidationError, match=COMPLETION_COMMIT_MISMATCH):
        completion_binding(_context(tree_root), args, integrated_commit=NEW)


def test_completion_reads_the_record_once(tree_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _record(tree_root, (OLD, NEW))
    _judged(monkeypatch, OLD)
    reads = _count_reads(monkeypatch)
    args: Any = SimpleNamespace(receipts=())

    completion_binding(_context(tree_root), args, integrated_commit=NEW)

    assert reads == [tree_root]


def test_completion_refuses_a_cyclic_record_as_rewrite_map_invalid(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW), (NEW, OLD))
    _judged(monkeypatch, OLD)
    args: Any = SimpleNamespace(receipts=())

    with pytest.raises(
        DaemonValidationError, match="validation_failed: rewrite_map_invalid: line 1"
    ):
        completion_binding(_context(tree_root), args, integrated_commit=NEW)


def test_completion_without_a_record_binds_the_head_as_before(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _judged(monkeypatch, OLD)
    args: Any = SimpleNamespace(receipts=())

    _answer, binding = completion_binding(_context(tree_root), args, integrated_commit=OLD)

    assert binding is not None and binding.head_sha == OLD


# --- the matched-head guard ---------------------------------------------------


def _merging_batch(head: str) -> DeliveryBatch:
    batch = seed_record(LifecycleEntity.DELIVERY_BATCH, BatchStatus.MERGING)
    assert isinstance(batch, DeliveryBatch) and batch.current_head_binding is not None
    binding = batch.current_head_binding.model_copy(update={"head_sha": head})
    return batch.model_copy(update={"current_head_binding": binding})


def _guard_inputs(tree_root: Path, pinned: str) -> GuardInputs:
    return GuardInputs(
        document={},
        document_path=tree_root / "generations" / "gen-x" / "state.json",
        record=_merging_batch(pinned),
        updates={},
        reason_code=None,
        actor="EAWF-RUNTIME",
    )


def _landed(monkeypatch: pytest.MonkeyPatch, matched: str) -> None:
    monkeypatch.setattr(
        domain_guards,
        "_landed_reconciliation",
        lambda _i: SimpleNamespace(matched_head_sha=matched),
    )


def test_matched_head_guard_passes_a_pinned_head_whose_pair_landed(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    _landed(monkeypatch, NEW)
    assert domain_guards._reconciliation_matched(_guard_inputs(tree_root, OLD)) is True


@pytest.mark.parametrize("pairs", [(), ((OLD, OTHER),)], ids=["unpaired", "paired-elsewhere"])
def test_matched_head_guard_still_refuses_a_differing_head(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch, pairs: tuple[tuple[str, str], ...]
) -> None:
    _record(tree_root, *pairs)
    _landed(monkeypatch, NEW)
    assert domain_guards._reconciliation_matched(_guard_inputs(tree_root, OLD)) is False


def test_matched_head_guard_refuses_a_cyclic_record_as_rewrite_map_invalid(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW), (NEW, OLD))
    _landed(monkeypatch, NEW)
    with pytest.raises(DaemonValidationError, match="validation_failed: rewrite_map_invalid"):
        domain_guards._reconciliation_matched(_guard_inputs(tree_root, OLD))


def test_matched_head_guard_reads_the_record_once(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    _landed(monkeypatch, NEW)
    reads = _count_reads(monkeypatch)
    assert domain_guards._reconciliation_matched(_guard_inputs(tree_root, OLD)) is True
    assert reads == [tree_root]


def test_matched_head_guard_matches_an_equal_head_without_a_record(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _landed(monkeypatch, OLD)
    assert domain_guards._reconciliation_matched(_guard_inputs(tree_root, OLD)) is True


# --- the carried check of a read-back -----------------------------------------

TOP = "5" * 40


def _read_back(tree_root: Path, monkeypatch: pytest.MonkeyPatch, *, pinned: str) -> Any:
    """Read back a Batch pinning *pinned* over a branch at TOP whose history holds NEW."""
    observed: list[Any] = []
    monkeypatch.setattr(delivery_landed, "_batch_row", lambda *_a: _merging_batch(pinned))
    monkeypatch.setattr(delivery_landed, "_branch_head", lambda *_a: TOP)
    monkeypatch.setattr(delivery_landed, "_ancestor", lambda _repo, old, new: old in (NEW, TOP))
    monkeypatch.setattr(
        delivery_landed, "reconcile_batch_merge", lambda _c, args: observed.append(args)
    )
    params = ReadBackParams(
        urn=parse_qualified_urn("eawf://EAWF/batch/BAT-0001"),
        actor="EAWF-RUNTIME",
        idempotency_key="read-back-1",
    )
    read_back_merge(_session_context(tree_root), params, now=AT)
    (args,) = observed
    return args.observation


class _NullSession:
    """A session context that holds *document*."""

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document or {}

    def __enter__(self) -> _NullSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read_document(self) -> dict[str, Any]:
        return self.document

    def ledger_path(self, collection: Any) -> Path:
        return Path(f"{collection}.jsonl")


def _session_context(tree_root: Path, document: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        identity=SimpleNamespace(tree_root=tree_root),
        session=lambda _locks: _NullSession(document),
    )


def test_carried_check_observes_the_replacement_of_a_pinned_head(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    observation = _read_back(tree_root, monkeypatch, pinned=OLD)
    # The observation names the replacement, never the commit the rewrite replaced.
    assert observation.contained_shas == (TOP, NEW)


def test_carried_check_does_not_carry_an_unpaired_head(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = _read_back(tree_root, monkeypatch, pinned=OLD)
    assert observation.contained_shas == (TOP,)


def test_carried_check_refuses_a_cyclic_record_as_rewrite_map_invalid(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW), (NEW, OLD))
    with pytest.raises(DaemonValidationError, match="validation_failed: rewrite_map_invalid"):
        _read_back(tree_root, monkeypatch, pinned=OLD)


# --- the reconciliation the read-back files -----------------------------------


def _observation(*contained: str) -> HostMergeObservation:
    batch = _merging_batch(OLD)
    return HostMergeObservation(
        batch_ref=batch.urn,
        target_branch=cast(str, batch.target_branch),
        observed_at=AT,
        target_head_sha=TOP,
        contained_shas=(TOP, *contained),
    )


def test_reconcile_records_the_replacement_of_a_pinned_head() -> None:
    decision = reconcile_merge(_merging_batch(OLD), _observation(NEW), rewritten={OLD: NEW})
    assert decision.outcome is MergeOutcome.LANDED
    assert decision.matched_head_sha == NEW


def test_reconcile_awaits_an_unpaired_head_as_recorded() -> None:
    decision = reconcile_merge(_merging_batch(OLD), _observation(NEW), rewritten={})
    assert decision.outcome is MergeOutcome.UNKNOWN
    assert decision.awaited_head_sha == OLD


def test_reconcile_verb_passes_the_recorded_map(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    filed: list[Any] = []
    monkeypatch.setattr(delivery_acceptance, "_batch_of", lambda *_a: _merging_batch(OLD))
    monkeypatch.setattr(
        delivery_acceptance, "_append_reconciliation", lambda _s, decision: filed.append(decision)
    )
    params = MergeReconcileParams(
        urn=_merging_batch(OLD).urn,
        actor="EAWF-RUNTIME",
        idempotency_key="reconcile-1",
        observation=_observation(NEW),
    )

    delivery_acceptance.reconcile_batch_merge(_session_context(tree_root), params)

    (decision,) = filed
    assert decision.matched_head_sha == NEW


# --- adoption: the head a Batch already stands on -----------------------------


def test_adoption_compares_against_the_replacement_of_the_standing_head(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    active = seed_record(LifecycleEntity.DELIVERY_BATCH, BatchStatus.ACTIVE)
    standing = SimpleNamespace(
        head=SimpleNamespace(integrated_revision=SimpleNamespace(head_sha=OLD), generation=2)
    )
    seen: list[str | None] = []

    def read_repository(_repo: Path, **kwargs: Any) -> Any:
        seen.append(kwargs["head_of"])
        raise LookupError("stop after the repository read")

    monkeypatch.setattr(delivery_landed, "keyed_answer", lambda *_a, **_k: None)
    monkeypatch.setattr(delivery_landed, "_batch_row", lambda *_a: active)
    monkeypatch.setattr(delivery_landed, "_adopted_tasks", lambda *_a: ())
    monkeypatch.setattr(delivery_landed, "_unresolved_evidence", lambda *_a: [])
    monkeypatch.setattr(delivery_landed, "read_generation_ledger", lambda *_a: standing)
    monkeypatch.setattr(delivery_landed, "_read_repository", read_repository)
    args: Any = SimpleNamespace(
        model_dump=lambda **_k: {},
        idempotency_key="adopt-1",
        urn=active.urn,
        task_refs=(),
        evidence_refs=(),
    )

    with pytest.raises(LookupError):
        delivery_landed.adopt_landed(_session_context(tree_root), args, now=AT)

    assert seen == [NEW]


# --- a Batch close completes its Tasks on the resolved head -------------------


def test_batch_close_binds_tasks_to_the_replacement_of_the_pinned_head(
    tree_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(tree_root, (OLD, NEW))
    task = seed_record(LifecycleEntity.TASK, TaskStatus.READY_TO_INTEGRATE)
    moves: list[Mapping[str, Any]] = []

    def move(*_a: Any, updates: Mapping[str, Any], **_k: Any) -> None:
        moves.append(updates)

    monkeypatch.setattr(batch_close, "_move", move)
    document = {"task": {task.key: task.model_dump(mode="json")}}

    batch_close._complete_tasks(
        _session_context(tree_root, document),
        _merging_batch(OLD),
        landed={task.key: None},
        citations={},
        progress=batch_close._Progress(),
        actor="EAWF-RUNTIME",
        now=AT,
    )

    (updates,) = moves
    assert updates["integrated_binding"]["head_sha"] == NEW
