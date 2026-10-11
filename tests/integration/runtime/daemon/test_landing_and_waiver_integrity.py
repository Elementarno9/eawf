"""A Batch closes only on work that landed, and only an operator waives a criterion.

A ready Task no branch carries a trailered commit for counts as evidence-only only on
positive evidence that it produced no code: the workspace its Run was leased holds
nothing, or, with no lease, it claims no path to write. Edits a Run left uncommitted,
and a host Task readied before its commit exists, keep the Batch open.

``runtime.delivery.waive_criterion`` admits only an operator principal and honours
``verify.waiver_mode``: ``disabled`` refuses every waiver and ``C`` requires a standing
decision, which the waiver's event records beside its actor.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.delivery_waiver import (
    CRITERION_WAIVED_EVENT,
    WaiveCriterionAnswer,
    WaiveCriterionParams,
    waive_criterion,
)
from eawf.runtime.workspace.lease import (
    issue_lease,
    revoke_lease,
    root_leases,
    workspace_path,
    write_lease,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    TASK_URN,
    document_path,
    rekeyed,
    seed_row,
)
from tests.integration.runtime.daemon.test_batch_close import (
    ACTOR,
    BRANCH,
    batch,
    close,
    commit,
    context_of,
    ledger_status,
    live,
    make_tree,
    task,
)

pytestmark = pytest.mark.integration

_CONTAINER: Final = TASK_URN.rsplit("/", 2)[0]
RUN_URN: Final = f"{_CONTAINER}/run/RUN-00000010"
EARLIER_RUN_URN: Final = f"{_CONTAINER}/run/RUN-00000009"
NO_COMMIT: Final = f"EAWF-0042 has no commit on {BRANCH}"
REASON: Final = "the drift check this criterion covered was removed with its test file"


# ---- landing --------------------------------------------------------------------


def ready(*, write_claims: tuple[str, ...] = (), run: str | None = None) -> dict[str, Any]:
    """Return EAWF-0042 ready to integrate, claiming *write_claims*, readied by *run*."""
    row = task("EAWF-0042", "READY_TO_INTEGRATE")
    if write_claims:
        row["write_claims"] = list(write_claims)
    if run is not None:
        row["active_run_ref"] = run
    return row


def tree(tmp_path: Path, row: dict[str, Any]) -> tuple[CanaryProvision, Path]:
    """Provision BAT-0007 holding the one Task *row*."""
    return make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": row},
    )


def lease_for(canary: CanaryProvision, tmp_path: Path, run: str = RUN_URN) -> Path:
    """Lease *run* a workspace for EAWF-0042 and return the worktree it materialized."""
    context = context_of(canary, tmp_path)
    lease = issue_lease(
        context,
        run_ref=run,
        task_ref=TASK_URN,
        purpose=RunPurpose.IMPLEMENT,
        base=BRANCH,
        writable_roots=("src",),
        now=datetime.now(UTC),
        ttl=timedelta(hours=1),
    )
    return workspace_path(context, handle=lease.workspace_handle)


def remove_worktree(repo: Path, workspace: Path) -> None:
    """Remove the leased worktree at *workspace* from under its lease."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(workspace)], check=True
    )


def assert_kept(canary: CanaryProvision, tmp_path: Path) -> None:
    """Assert the Batch stays open on EAWF-0042 having no commit, the Task still ready."""
    closure = close(canary, tmp_path)
    assert not closure.closed
    assert NO_COMMIT in closure.reason
    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"]["status"] == "READY_TO_INTEGRATE"
    assert ledger_status(canary, Epoch2Collection.BATCH, "BAT-0007") is None


def assert_closed(canary: CanaryProvision, tmp_path: Path) -> None:
    """Assert the Batch closes with EAWF-0042 completed."""
    closure = close(canary, tmp_path)
    assert closure.closed, closure.reason
    assert closure.completed_task_keys == ("EAWF-0042",)
    assert ledger_status(canary, Epoch2Collection.TASK, "EAWF-0042") == "COMPLETED"


def test_a_run_that_left_an_untracked_edit_has_not_landed(tmp_path: Path) -> None:
    canary, _repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    workspace = lease_for(canary, tmp_path)
    (workspace / "src" / "edited.py").write_text("print('never committed')\n", encoding="utf-8")

    assert_kept(canary, tmp_path)


def test_a_run_that_left_a_tracked_file_modified_has_not_landed(tmp_path: Path) -> None:
    canary, _repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    workspace = lease_for(canary, tmp_path)
    (workspace / "src" / "module.py").write_text("# changed\n", encoding="utf-8")

    assert_kept(canary, tmp_path)


def test_a_host_task_claiming_writes_with_no_commit_has_not_landed(tmp_path: Path) -> None:
    canary, _repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))

    assert_kept(canary, tmp_path)


def test_a_host_task_claiming_writes_lands_once_its_commit_does(tmp_path: Path) -> None:
    canary, repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    assert_kept(canary, tmp_path)

    commit(repo, "src/alpha.py", "feat: alpha\n\nTask: EAWF-0042")

    assert_closed(canary, tmp_path)


def test_a_run_that_left_its_workspace_clean_is_evidence_only(tmp_path: Path) -> None:
    canary, _repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    lease_for(canary, tmp_path)

    assert_closed(canary, tmp_path)


def test_a_task_claiming_no_writes_and_holding_no_lease_is_evidence_only(
    tmp_path: Path,
) -> None:
    canary, _repo = tree(tmp_path, ready())

    assert_closed(canary, tmp_path)


def test_a_released_lease_whose_workspace_is_gone_is_evidence_only(tmp_path: Path) -> None:
    canary, repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    workspace = lease_for(canary, tmp_path)
    context = context_of(canary, tmp_path)
    [lease] = root_leases(context)
    write_lease(context, lease.model_copy(update={"status": LeaseStatus.RELEASED}))
    remove_worktree(repo, workspace)

    assert_closed(canary, tmp_path)


def test_an_unreleased_lease_whose_workspace_is_gone_has_not_landed(tmp_path: Path) -> None:
    canary, repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    remove_worktree(repo, lease_for(canary, tmp_path))

    assert_kept(canary, tmp_path)


def test_only_the_lease_of_the_run_that_readied_the_task_is_judged(tmp_path: Path) -> None:
    canary, _repo = tree(tmp_path, ready(write_claims=("src",), run=RUN_URN))
    earlier = lease_for(canary, tmp_path, run=EARLIER_RUN_URN)
    (earlier / "src" / "abandoned.py").write_text("print('retried')\n", encoding="utf-8")
    context = context_of(canary, tmp_path)
    [abandoned] = root_leases(context)
    revoke_lease(context, lease_id=abandoned.lease_id, now=datetime.now(UTC))
    lease_for(canary, tmp_path)

    assert_closed(canary, tmp_path)


@pytest.mark.parametrize(
    "claims",
    [
        (".ea/local/rc2-evidence/**",),
        (".ea/local/rc2-evidence/**", ".ea/local/notes"),
        ("scratch",),
        ("scratch/runs/**", ".ea/local/rc2-evidence/**"),
    ],
)
def test_a_host_task_claiming_only_untrackable_paths_is_evidence_only(
    tmp_path: Path, claims: tuple[str, ...]
) -> None:
    canary, repo = tree(tmp_path, ready(write_claims=claims, run=RUN_URN))
    ignore_scratch(repo)

    assert_closed(canary, tmp_path)


@pytest.mark.parametrize(
    "claims",
    [
        (".ea/local/rc2-evidence/**", "src"),
        ("scratch", "docs/notes.md"),
        ("**",),
        (".ea/state.json",),
        ("scratchpad/**",),
    ],
)
def test_a_host_task_claiming_any_trackable_path_has_not_landed(
    tmp_path: Path, claims: tuple[str, ...]
) -> None:
    canary, repo = tree(tmp_path, ready(write_claims=claims, run=RUN_URN))
    ignore_scratch(repo)

    assert_kept(canary, tmp_path)


def ignore_scratch(repo: Path) -> None:
    """Commit a ``.gitignore`` that ignores the ``scratch/`` directory."""
    gitignore = repo / ".gitignore"
    held = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    gitignore.write_text(f"{held}scratch/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "chore: ignore scratch"], check=True
    )


# ---- waiver ---------------------------------------------------------------------


def waivable(tmp_path: Path, *, waiver_mode: str | None = None) -> CanaryProvision:
    """Provision EAWF-0042 PLANNED with criterion CR-01, under *waiver_mode* when named."""
    canary, repo = make_tree(
        tmp_path,
        batches={"BAT-0007": batch("BAT-0007", "PLANNED", ["EAWF-0042"])},
        tasks={"EAWF-0042": rekeyed(seed_row("task", "PLANNED"), key="EAWF-0042")},
    )
    if waiver_mode is not None:
        config = repo / ".ea" / "config.yaml"
        layer = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
        layer = layer or {}
        layer.setdefault("verify", {})["waiver_mode"] = waiver_mode
        config.write_text(yaml.safe_dump(layer), encoding="utf-8")
    return canary


def file_decision(canary: CanaryProvision, key: str) -> None:
    """Stand decision *key* on the tree's decision ledger."""
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.DECISION),
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key=key,
            status="active",
            recorded_at=AT,
            payload={"key": key},
        ),
    )


def waive(
    canary: CanaryProvision, tmp_path: Path, **overrides: Any
) -> tuple[WaiveCriterionAnswer, dict[str, Any] | None]:
    """Waive CR-01 of EAWF-0042 and return the answer beside the event's payload."""
    revision = live(canary, Epoch2Collection.TASK)["EAWF-0042"]["revision"]
    args = WaiveCriterionParams.model_validate(
        {
            "urn": TASK_URN,
            "criterion_id": "CR-01",
            "reason": REASON,
            "expected_revision": revision,
            "actor": ACTOR,
            "idempotency_key": f"waive-{uuid.uuid4().hex}",
            **overrides,
        }
    )
    answer, envelope = waive_criterion(context_of(canary, tmp_path), args, now=AT)
    return answer, None if envelope is None else envelope.payload


def waiver_of(canary: CanaryProvision) -> str | None:
    """Return CR-01's stored waiver reason."""
    reason: str | None = live(canary, Epoch2Collection.TASK)["EAWF-0042"]["criteria"][0][
        "waiver_reason"
    ]
    return reason


def assert_refused(canary: CanaryProvision, tmp_path: Path, code: str, **overrides: Any) -> None:
    """Assert the waiver is refused naming *code* and the Task is left as it stood."""
    before = live(canary, Epoch2Collection.TASK)["EAWF-0042"]
    with pytest.raises(TransactionRefusedError, match=code):
        waive(canary, tmp_path, **overrides)
    assert live(canary, Epoch2Collection.TASK)["EAWF-0042"] == before


@pytest.mark.parametrize("actor", ["DAEMON", "RUN-00000010", "AGENT", "OPERATOR", "OP"])
def test_a_waiver_by_a_non_operator_principal_is_refused(tmp_path: Path, actor: str) -> None:
    canary = waivable(tmp_path)

    assert_refused(canary, tmp_path, "waiver_operator_required", actor=actor)


def test_the_operator_refusal_names_the_principal_that_may_waive(tmp_path: Path) -> None:
    canary = waivable(tmp_path)

    with pytest.raises(TransactionRefusedError) as refusal:
        waive(canary, tmp_path, actor="AGENT")

    assert "--actor" in refusal.value.remediation
    assert "OP-" in refusal.value.remediation


def test_an_operator_waiver_is_recorded_with_its_actor(tmp_path: Path) -> None:
    canary = waivable(tmp_path)

    answer, event = waive(canary, tmp_path)

    assert answer.waived is True
    assert waiver_of(canary) == REASON
    assert event is not None
    assert CRITERION_WAIVED_EVENT in str(event)
    assert ACTOR in str(event)


def test_a_disabled_waiver_mode_refuses_even_the_operator(tmp_path: Path) -> None:
    canary = waivable(tmp_path, waiver_mode="disabled")
    file_decision(canary, "D86")

    assert_refused(canary, tmp_path, "waiver_mode_disabled", decision_ref="D86")


def test_waiver_mode_c_requires_a_decision(tmp_path: Path) -> None:
    canary = waivable(tmp_path, waiver_mode="C")

    assert_refused(canary, tmp_path, "waiver_decision_required")


def test_waiver_mode_c_refuses_a_decision_the_tree_does_not_hold(tmp_path: Path) -> None:
    canary = waivable(tmp_path, waiver_mode="C")
    file_decision(canary, "D85")

    assert_refused(canary, tmp_path, "holds no decision D86", decision_ref="D86")


def test_waiver_mode_c_admits_a_waiver_leaning_on_a_standing_decision(tmp_path: Path) -> None:
    canary = waivable(tmp_path, waiver_mode="C")
    file_decision(canary, "D86")

    answer, event = waive(canary, tmp_path, decision_ref="D86")

    assert answer.waived is True
    assert waiver_of(canary) == REASON
    assert event is not None
    assert "D86" in str(event)


@pytest.mark.parametrize("mode", ["A", "B"])
def test_waiver_modes_a_and_b_need_no_decision(tmp_path: Path, mode: str) -> None:
    canary = waivable(tmp_path, waiver_mode=mode)

    answer, _event = waive(canary, tmp_path)

    assert answer.waived is True


@pytest.mark.parametrize("decision_ref", ["", "86", "D8", "DEC-86", "d86"])
def test_a_malformed_decision_key_is_refused_before_anything_is_read(decision_ref: str) -> None:
    with pytest.raises(ValueError, match="decision_ref"):
        WaiveCriterionParams.model_validate(
            {
                "urn": TASK_URN,
                "criterion_id": "CR-01",
                "reason": REASON,
                "expected_revision": 1,
                "actor": ACTOR,
                "idempotency_key": "waive-1",
                "decision_ref": decision_ref,
            }
        )
