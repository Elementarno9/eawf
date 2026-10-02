"""A daemon start records what its crash recovery repaired, and what that cost.

The crash is real: the in-flight cap verb is killed inside a ledger append, so
the next boot through ``run`` finds a torn tail to cut. The record is written by
that boot, at the moment its recovery passes finish, and read back by the verb
the console's Recovery frame asks.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from eawf.kernel.runtime.boot_recovery import RecoveryPass
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_recovery import boot_recovery_path, read_boot_recovery
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.console_records import BOOT_RECOVERY_READ_METHOD
from tests.integration.runtime.daemon.test_epoch2_torn_tail_boot import (
    _boot,
    _killed,
    _sweep_wal,
    _wal_dir,
    canary,
    ctx,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="the boot stub binds a POSIX socket"),
]

__all__ = ["canary", "ctx"]


def test_a_boot_after_a_crash_records_the_repair_it_made(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed(ctx, canary, monkeypatch, 3)
    _sweep_wal(ctx)

    _boot(ctx, canary, monkeypatch)

    record = read_boot_recovery(_wal_dir(ctx))
    assert record is not None
    assert record.passes() == (RecoveryPass.TAIL_REPAIR,)
    assert record.truncated_ledgers == 1
    assert (record.finished_intents, record.abandoned_intents) == (0, 0)
    assert record.finished_at >= record.started_at


def test_a_second_boot_records_a_clean_start(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed(ctx, canary, monkeypatch, 3)
    _sweep_wal(ctx)
    _boot(ctx, canary, monkeypatch)
    first = read_boot_recovery(_wal_dir(ctx))

    _boot(ctx, canary, monkeypatch)

    second = read_boot_recovery(_wal_dir(ctx))
    assert first is not None and second is not None
    assert second.passes() == ()
    assert second.started_at >= first.finished_at


def test_a_pending_intent_is_recorded_as_a_replay(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed(ctx, canary, monkeypatch, 1)

    _boot(ctx, canary, monkeypatch)

    record = read_boot_recovery(_wal_dir(ctx))
    assert record is not None
    assert record.passes() == (RecoveryPass.TAIL_REPAIR, RecoveryPass.WAL_REPLAY)
    assert record.finished_intents == 1


def test_a_daemon_that_never_started_has_no_record(ctx: MethodContext) -> None:
    assert not boot_recovery_path(_wal_dir(ctx)).exists()
    assert read_boot_recovery(_wal_dir(ctx)) is None


def test_the_recovery_verb_answers_the_last_start(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    _killed(ctx, canary, monkeypatch, 3)
    _sweep_wal(ctx)
    _boot(ctx, canary, monkeypatch)
    # the boot stubs asyncio.run out of the daemon, so the verb runs after it is restored
    monkeypatch.undo()

    answer = asyncio.run(
        methods.dispatch(BOOT_RECOVERY_READ_METHOD, ctx, {"repo_root": str(canary.root)})
    )

    assert answer["last"]["truncated_ledgers"] == 1


def test_the_recovery_verb_refuses_an_unknown_parameter(
    ctx: MethodContext, canary: CanaryProvision
) -> None:
    with pytest.raises(methods.DaemonValidationError):
        asyncio.run(
            methods.dispatch(
                BOOT_RECOVERY_READ_METHOD, ctx, {"repo_root": str(canary.root), "route": "x"}
            )
        )
