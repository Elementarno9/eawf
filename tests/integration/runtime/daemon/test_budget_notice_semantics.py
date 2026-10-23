"""Budget notices, driven through the producer, the ledger and the daemon verbs.

Each case runs the code an operator's session runs: a Run's budget
termination, the notice ledger on disk, the recipient verbs the daemon
registers and the pause projection the operator surface reads. A restart
is a fresh call over the same files, which is all a restart is to code
that keeps nothing in memory.
"""

from __future__ import annotations

import ast
import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.economics.governor import economics_policy_from
from eawf.kernel.economics.notice_policy import DEFAULT_NOTICE_POLICY
from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.budget.legacy_notices import PAUSE_EVENT_TYPE, RESUME_EVENT_TYPE
from eawf.runtime.budget.notice_inbox import (
    NoticeDispositionError,
    StaleNoticeRevisionError,
    deliver_pending,
    dispose_notice,
    inbox_for,
)
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    UpsertOutcome,
    load_notice_ledger,
    notices_path,
    upsert_notice,
)
from eawf.runtime.budget.service import emit_termination_notice
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.admission import EconomicsPolicyError, load_economics
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.budget_notice import (
    dispose_budget_notice,
    list_budget_notices,
)
from eawf.runtime.daemon.pause_migration import open_legacy_pauses
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from tests.integration.runtime.daemon.test_native_dispatch import RUN_URN

pytestmark = pytest.mark.integration

WAVE = "P24-I02-W20"
OTHER_WAVE = "P24-I02-W21"
SESSION = "SES-claim-one"
CLAIMED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
ALICE = "ALICE"
BOB = "BOB"
_QUESTION = UserQuestion(
    question="over-budget advisory",
    options=[
        UserQuestionOption(label="keep", description="Keep it active."),
        UserQuestionOption(label="defer", description="Leave it for later."),
    ],
).model_dump_json()
SRC = Path(__file__).resolve().parents[4] / "src" / "eawf"


# ---- fixtures ---------------------------------------------------------------


def _wave(wave_id: str, *, status: str = "claimed") -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": "P24-I02",
        "title": "budget notice journey",
        "status": status,
        "claim_session_id": SESSION,
        "effort_bucket": "M",
        "opened_at": CLAIMED.isoformat(),
        "claimed_at": CLAIMED.isoformat(),
        "sessions": {},
    }


def _write_state(tmp_path: Path, waves: dict[str, dict[str, Any]]) -> Path:
    """Write an epoch-1 state holding *waves* and return its path."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": CLAIMED.isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR", "active_wave_ids": list(waves)},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def _append(state_path: Path, *, scope_id: str, payload: EventPayload) -> None:
    append_envelope(
        store_path(state_path, StoreKind.EVENT),
        Envelope(
            id=f"EV-{abs(hash((scope_id, payload.timestamp, payload.event_type))) % 10**12:012d}",
            kind=StoreKind.EVENT,
            scope_id=scope_id,
            created_at=payload.timestamp,
            updated_at=None,
            summary=payload.event_type,
            payload=payload.model_dump(mode="json"),
            blob_refs=[],
            artifact_ids=[],
        ),
    )


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-09-01T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=None,
        state_path=state_path,
        event_path=store_path(state_path, StoreKind.EVENT),
    )


def _rpc(
    handler: Callable[[MethodContext, dict[str, Any]], Awaitable[dict[str, Any]]],
    ctx: MethodContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Call one registered daemon verb to completion."""

    async def call() -> dict[str, Any]:
        return await handler(ctx, params)

    return asyncio.run(call())


def _legacy_pause(
    state_path: Path,
    *,
    wave_id: str,
    band: str | None,
    at: datetime,
    urn: str,
    session: str = SESSION,
) -> None:
    """Append one over-budget pause row as the retired detector wrote it."""
    extras: dict[str, str | int | float | bool] = {
        "pause_urn": urn,
        "session": session,
        "user_question": _QUESTION,
        "wave_id": wave_id,
        "elapsed_minutes": 22.2359,
        "budget_minutes": 27.0,
    }
    if band is not None:
        extras["advisory_band"] = band
    if band == "backstop":
        del extras["budget_minutes"]
    _append(
        state_path,
        scope_id="urn:eawf:v1:state:QR",
        payload=EventPayload(
            timestamp=at,
            event_type=PAUSE_EVENT_TYPE,
            event_kind="stale_wave_detected",
            actor="daemon",
            command="stale_wave.sweep",
            args_hash="1" * 16,
            status="needs_user",
            message="over-budget advisory",
            extras=extras,
        ),
    )


def _legacy_resume(state_path: Path, *, urn: str, choice: str, at: datetime) -> None:
    _append(
        state_path,
        scope_id="urn:eawf:v1:state:QR",
        payload=EventPayload(
            timestamp=at,
            event_type=RESUME_EVENT_TYPE,
            actor="operator",
            command="needs_user.resolve",
            args_hash="2" * 16,
            status="ok",
            message="resumed",
            extras={"pause_urn": urn, "scope_id": "urn:eawf:v1:state:QR", "choice": choice},
        ),
    )


# ---- PRX-033: no sub-unity band can load ------------------------------------


@pytest.mark.parametrize(
    "notice_policy",
    [
        {"estimated_time": {"notify_fraction": 0.8}},
        {"estimated_time": {"notify_fraction": "0.999"}},
        {"estimated_time": {"progress_fraction": 0.8}},
        {"progress_fraction": 0.8},
        {"warning_fraction": 0.75},
        {"axis_thresholds": {"tokens": {"warning_fraction": 0.75}}},
        {"estimated_time": {"notify_at": 0.8}},
    ],
)
def test_prx_033_a_policy_declaring_a_sub_unity_band_fails_to_load(
    notice_policy: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as caught:
        economics_policy_from({"economics": {"notice_policy": notice_policy}})
    assert "notice_policy" in str(caught.value)


def test_prx_033_the_repository_loader_refuses_a_sub_unity_band(tmp_path: Path) -> None:
    config = tmp_path / ".ea" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "economics:\n  notice_policy:\n    estimated_time:\n      notify_fraction: 0.8\n"
    )

    with pytest.raises(EconomicsPolicyError, match="notify_fraction"):
        load_economics(tmp_path)


def test_prx_033_an_empty_table_loads_the_shipped_policy() -> None:
    assert economics_policy_from({}).notice_policy == DEFAULT_NOTICE_POLICY


# ---- PRX-036: dispositions survive restart ---------------------------------


def _terminated(state_path: Path, *, now: datetime, audience: tuple[str, ...]) -> str:
    """Fold one Run's budget-termination receipt into the tree's notice ledger."""
    receipt = BudgetNotice.model_validate(
        {
            "run_ref": str(RUN_URN),
            "control_request_ref": "CTL-00000001",
            "observed_tokens": 1200,
            "cap_tokens": 1000,
            "noticed_at": now.isoformat(),
        }
    )
    result = emit_termination_notice(
        notices_path(state_path), receipt, contract_digest=None, audience=audience
    )
    assert result is not None
    return result.notice.notice_key


def _opened(tmp_path: Path) -> tuple[Path, str, datetime]:
    state_path = _write_state(tmp_path, {WAVE: _wave(WAVE)})
    now = CLAIMED + timedelta(hours=1)
    key = _terminated(state_path, now=now, audience=(LOCAL_OPERATOR,))
    return state_path, key, now


def _inbox_after_restart(state_path: Path) -> dict[str, Any]:
    """Read the inbox the way a restarted client does: through the daemon verb."""
    return _rpc(list_budget_notices, _ctx(state_path), {"principal": LOCAL_OPERATOR})


def test_prx_036_open_acknowledge_snooze_resolve_each_survive_restart(tmp_path: Path) -> None:
    state_path, key, _now = _opened(tmp_path)
    ctx = _ctx(state_path)

    def dispose(disposition: str, **extra: Any) -> dict[str, Any]:
        params = {
            "notice_key": key,
            "principal": LOCAL_OPERATOR,
            "disposition": disposition,
            "expected_revision": 1,
            **extra,
        }
        notice: dict[str, Any] = _rpc(dispose_budget_notice, ctx, params)["notice"]
        return notice

    dispose("open")
    seen = _inbox_after_restart(state_path)
    assert [n["recipients"][LOCAL_OPERATOR]["seen_revision"] for n in seen["active"]] == [1]
    assert seen["active"][0]["status"] == "OPEN"  # opening is not resolving

    dispose("acknowledge")
    acked = _inbox_after_restart(state_path)
    assert (len(acked["active"]), len(acked["acknowledged"])) == (0, 1)

    until = datetime.now(UTC) + timedelta(hours=1)
    dispose("snooze", snooze_until=until.isoformat())
    snoozed = _inbox_after_restart(state_path)
    assert snoozed == {"active": [], "acknowledged": [], "history": []}
    lapsed = inbox_for(
        load_notice_ledger(notices_path(state_path)),
        principal=LOCAL_OPERATOR,
        now=until + timedelta(seconds=1),
    )
    assert len(lapsed.acknowledged) == 1

    dispose("resolve")
    resolved = _inbox_after_restart(state_path)
    assert (resolved["active"], resolved["acknowledged"]) == ([], [])
    (record,) = resolved["history"]
    assert (record["status"], record["resolved_by"]) == ("RESOLVED", LOCAL_OPERATOR)
    assert [h["action"] for h in record["history"]] == [
        "seen",
        "acknowledged",
        "snoozed",
        "resolved",
    ]


def test_prx_036_a_stale_revision_is_refused_with_the_current_one(tmp_path: Path) -> None:
    state_path, key, now = _opened(tmp_path)
    before = notices_path(state_path).read_bytes()

    with pytest.raises(StaleNoticeRevisionError) as caught:
        dispose_notice(
            notices_path(state_path),
            notice_key=key,
            principal=LOCAL_OPERATOR,
            disposition="acknowledge",
            expected_revision=2,
            at=now,
        )

    assert caught.value.current_revision == 1
    assert notices_path(state_path).read_bytes() == before


@pytest.mark.parametrize(
    ("disposition", "snooze_until", "reason"),
    [
        ("snooze", None, "exactly when snoozing"),
        ("open", 60, "exactly when snoozing"),
        ("snooze", -60, "after the disposition"),
        ("snooze", 0, "after the disposition"),
    ],
)
def test_prx_036_a_misplaced_snooze_deadline_is_refused(
    tmp_path: Path, disposition: str, snooze_until: int | None, reason: str
) -> None:
    state_path, key, now = _opened(tmp_path)
    until = None if snooze_until is None else now + timedelta(seconds=snooze_until)

    with pytest.raises(NoticeDispositionError, match=reason):
        dispose_notice(
            notices_path(state_path),
            notice_key=key,
            principal=LOCAL_OPERATOR,
            disposition=disposition,  # type: ignore[arg-type]
            expected_revision=1,
            at=now,
            snooze_until=until,
        )


def test_prx_036_a_resolved_notice_takes_no_further_disposition(tmp_path: Path) -> None:
    state_path, key, now = _opened(tmp_path)
    args = {"notice_key": key, "principal": LOCAL_OPERATOR, "expected_revision": 1, "at": now}
    dispose_notice(notices_path(state_path), disposition="resolve", **args)  # type: ignore[arg-type]

    with pytest.raises(NoticeDispositionError, match="already RESOLVED"):
        dispose_notice(notices_path(state_path), disposition="acknowledge", **args)  # type: ignore[arg-type]
    with pytest.raises(NoticeDispositionError, match="no notice"):
        dispose_notice(
            notices_path(state_path),
            disposition="open",
            **{**args, "notice_key": "sha256:" + "0" * 64},  # type: ignore[arg-type]
        )


def test_prx_036_the_daemon_verb_maps_a_refusal_to_a_validation_error(tmp_path: Path) -> None:
    state_path, key, _now = _opened(tmp_path)
    params = {
        "notice_key": key,
        "principal": LOCAL_OPERATOR,
        "disposition": "acknowledge",
        "expected_revision": 7,
    }

    with pytest.raises(DaemonValidationError, match="revision 1, not 7"):
        _rpc(dispose_budget_notice, _ctx(state_path), params)
    with pytest.raises(ValidationError):
        _rpc(dispose_budget_notice, _ctx(state_path), {**params, "expected_revision": 0})


def test_the_daemon_verbs_address_the_tree_a_repo_root_names(tmp_path: Path) -> None:
    """A console attached to a repository names it; the daemon reads that tree's ledger."""
    state_path, key, _now = _opened(tmp_path)
    elsewhere = _ctx(tmp_path / "other" / ".ea" / "state.json")
    root = {"repo_root": str(state_path.parent.parent)}
    listed = _rpc(list_budget_notices, elsewhere, {"principal": LOCAL_OPERATOR, **root})
    assert [n["notice_key"] for n in listed["active"]] == [key]
    params = {
        "notice_key": key,
        "principal": LOCAL_OPERATOR,
        "disposition": "resolve",
        "expected_revision": 1,
        **root,
    }
    assert _rpc(dispose_budget_notice, elsewhere, params)["notice"]["status"] == "RESOLVED"
    assert _inbox_after_restart(state_path)["active"] == []


def test_the_daemon_verbs_without_a_repo_root_read_the_bound_tree(tmp_path: Path) -> None:
    state_path, key, _now = _opened(tmp_path)
    listed = _rpc(list_budget_notices, _ctx(state_path), {"principal": LOCAL_OPERATOR})
    assert [n["notice_key"] for n in listed["active"]] == [key]


# ---- PRX-037: one principal's notice touches no other principal -------------


def _alice_notice(tmp_path: Path) -> tuple[Path, str, datetime]:
    state_path = _write_state(tmp_path, {WAVE: _wave(WAVE)})
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return state_path, _terminated(state_path, now=now, audience=(ALICE,)), now


def test_prx_037_a_notice_for_one_principal_is_invisible_to_another(tmp_path: Path) -> None:
    state_path, key, now = _alice_notice(tmp_path)
    ledger_file = notices_path(state_path)
    before = ledger_file.read_bytes()

    bob_inbox = inbox_for(load_notice_ledger(ledger_file), principal=BOB, now=now)
    assert bob_inbox.active == bob_inbox.acknowledged == bob_inbox.history == ()
    assert deliver_pending(ledger_file, principal=BOB, now=now) == ()
    for disposition in ("open", "acknowledge", "resolve"):
        with pytest.raises(NoticeDispositionError, match=f"not addressed to {BOB}"):
            dispose_notice(
                ledger_file,
                notice_key=key,
                principal=BOB,
                disposition=disposition,
                expected_revision=1,
                at=now,
            )
    assert ledger_file.read_bytes() == before
    assert len(inbox_for(load_notice_ledger(ledger_file), principal=ALICE, now=now).active) == 1


def test_prx_037_alice_snoozing_leaves_bob_undelivered_and_active(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, {WAVE: _wave(WAVE)})
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    ledger_file = notices_path(state_path)
    upsert_notice(
        ledger_file,
        BudgetCrossing(
            scope_id=WAVE,
            basis="hard_limit",
            band="limit_reached",
            observed_value=10,
            budget_value=5,
            observed_at=now,
            audience=(ALICE, BOB),
        ),
    )
    (key,) = load_notice_ledger(ledger_file).notices
    deliver_pending(ledger_file, principal=ALICE, now=now)
    dispose_notice(
        ledger_file,
        notice_key=key,
        principal=ALICE,
        disposition="snooze",
        expected_revision=1,
        at=now,
        snooze_until=now + timedelta(hours=1),
    )

    notice = load_notice_ledger(ledger_file).notices[key]
    assert BOB not in notice.recipients
    assert len(inbox_for(load_notice_ledger(ledger_file), principal=BOB, now=now).active) == 1
    assert inbox_for(load_notice_ledger(ledger_file), principal=ALICE, now=now).active == ()
    assert len(deliver_pending(ledger_file, principal=BOB, now=now)) == 1


#: The modules that decide whether work is suspended, queued, denied,
#: interrupted, focused or counted. None of them may read a notice.
_GUARDS = (
    "runtime/daemon/admission.py",
    "kernel/economics/governor.py",
    "runtime/daemon/native_dispatch.py",
    "runtime/daemon/budget_interlock.py",
    "runtime/control/reducer.py",
    "kernel/projection/attention.py",
    "kernel/projection/registers.py",
    "surfaces/tui/console/app.py",
)
_NOTICE_READERS = ("eawf.runtime.budget.notices", "eawf.runtime.budget.notice_inbox")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("guard", _GUARDS)
def test_prx_037_no_guard_module_reads_a_notice(guard: str) -> None:
    assert not _imports(SRC / guard) & set(_NOTICE_READERS)


# ---- PRX-038: exhaustion stops the subject Run only ---------------------------


def test_prx_038_exact_exhaustion_stops_only_the_subject_run(tmp_path: Path) -> None:
    from eawf.kernel.store.compaction import read_document
    from eawf.runtime.daemon.native_dispatch import DispatchRefusal, DispatchStage
    from tests.integration.runtime.daemon._epoch2_transaction_fixtures import document_path
    from tests.integration.runtime.daemon.test_budget_notice_scope import over_cap, two_runs
    from tests.integration.runtime.daemon.test_native_dispatch import (
        SUCCESSOR_KEY,
        LedgerReadingLauncher,
        dispatch,
        dispatch_params,
        method_ctx,
        run_urn,
    )
    from tests.integration.runtime.test_run_meter_producer import run_status

    canary = two_runs(tmp_path / "repo")

    error = over_cap(canary, tmp_path)

    assert DispatchRefusal.BUDGET_EXHAUSTED.value in str(error)
    assert run_status(canary, tmp_path) == "CANCELLED"
    assert read_document(document_path(canary))["run"][SUCCESSOR_KEY]["status"] == "QUEUED"
    runtime = tmp_path / "runtime"
    answer = dispatch(
        method_ctx(runtime),
        canary,
        LedgerReadingLauncher(canary, runtime),
        params=dispatch_params(canary, key="dispatch-02", urn=str(run_urn(SUCCESSOR_KEY))),
        now=datetime.now(UTC),
    )
    assert answer["stage"] == DispatchStage.ANNOUNCED.value


# ---- PRX-039: concurrent producers ---------------------------------------------


def test_prx_039_concurrent_producers_yield_one_notice(tmp_path: Path) -> None:
    ledger_file = notices_path(_write_state(tmp_path, {WAVE: _wave(WAVE)}))
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    crossing = BudgetCrossing(
        scope_id=WAVE,
        axis="wall_seconds",
        basis="hard_limit",
        band="limit_reached",
        observed_value=7200,
        budget_value=3600,
        observed_at=now,
        audience=(ALICE, BOB),
    )

    outcomes: list[UpsertOutcome] = []
    start = threading.Barrier(2)

    def produce() -> None:
        start.wait()
        outcomes.append(upsert_notice(ledger_file, crossing).outcome)

    producers = [threading.Thread(target=produce) for _ in range(2)]
    for thread in producers:
        thread.start()
    for thread in producers:
        thread.join()

    assert sorted(outcomes) == sorted([UpsertOutcome.CREATED, UpsertOutcome.UNCHANGED])
    (notice,) = load_notice_ledger(ledger_file).notices.values()
    assert (notice.highest_band, notice.revision) == ("limit_reached", 1)

    delivered: dict[str, int] = {ALICE: 0, BOB: 0}
    lock = threading.Lock()
    gate = threading.Barrier(6)

    def receive(principal: str) -> None:
        gate.wait()
        got = deliver_pending(ledger_file, principal=principal, now=now)
        with lock:
            delivered[principal] += len(got)

    clients = [threading.Thread(target=receive, args=(p,)) for p in (ALICE, BOB) * 3]
    for thread in clients:
        thread.start()
    for thread in clients:
        thread.join()

    assert delivered == {ALICE: 1, BOB: 1}


# ---- PRX-040: legacy pauses stay listed ----------------------------------------


def _legacy_corpus(tmp_path: Path) -> Path:
    """Write a state and the legacy rows of four waves in four fates."""
    state_path = _write_state(
        tmp_path,
        {
            WAVE: _wave(WAVE),
            OTHER_WAVE: _wave(OTHER_WAVE, status="closed"),
            "P24-I02-W22": _wave("P24-I02-W22"),
        },
    )
    t = CLAIMED
    # An operator answered both of W20's pauses.
    _legacy_pause(state_path, wave_id=WAVE, band="warn", at=t, urn="urn:a:1")
    _legacy_resume(state_path, urn="urn:a:1", choice="keep", at=t + timedelta(minutes=1))
    _legacy_pause(state_path, wave_id=WAVE, band="err", at=t + timedelta(minutes=9), urn="urn:a:2")
    _legacy_resume(state_path, urn="urn:a:2", choice="keep", at=t + timedelta(minutes=10))
    # W21 closed and its pause was only retracted by the system.
    _legacy_pause(state_path, wave_id=OTHER_WAVE, band="backstop", at=t, urn="urn:b:1")
    _legacy_resume(state_path, urn="urn:b:1", choice="auto-resolved", at=t + timedelta(minutes=2))
    # W22 has an unanswered warn and a pre-band row in another claim: two open pauses.
    _legacy_pause(state_path, wave_id="P24-I02-W22", band="warn", at=t, urn="urn:c:1")
    _legacy_pause(
        state_path,
        wave_id="P24-I02-W22",
        band=None,
        at=t + timedelta(hours=2),
        urn="urn:c:2",
        session="SES-claim-two",
    )
    return state_path


def test_prx_040_a_corrupt_ledger_leaves_the_pauses_listed(tmp_path: Path) -> None:
    state_path = _legacy_corpus(tmp_path)
    notices_path(state_path).parent.mkdir(parents=True, exist_ok=True)
    notices_path(state_path).write_text("{not json")

    assert len(open_legacy_pauses(state_path)) == 2
