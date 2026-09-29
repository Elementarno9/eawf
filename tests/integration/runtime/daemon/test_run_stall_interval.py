"""A stall is measured against its runtime's interval and ends nothing.

The interval is ``runtime.<name>.stall_interval_s``, resolved for the
runtime the Run's accepted hello named, so one runtime's routine pause is
not read as another's hang. The read is driven through the real daemon
verb with the real clock; the threshold is crossed by moving the
configured interval rather than by waiting, so no assertion can flake.

A stalled Run that is still recorded as running is labelled ``lost``: its
outcome is unknown, which is neither a success nor a failure, and the
label moves no status. Only a principal's control ends the Run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.config.schema import DEFAULT_STALL_INTERVAL_SECONDS
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.state.epoch2.transitions import SUCCESS_TERMINALS, AmbiguityLabel, LifecycleEntity
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.run import (
    RUN_BIND_METHOD,
    RUN_CONTRACT_READ_METHOD,
    RUN_EVENT_APPEND_METHOD,
    RUN_EVENTS_READ_METHOD,
    RUN_WORKER_HELLO_METHOD,
)
from eawf.runtime.daemon.run_events import RunLiveness
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
SPEC_DIGEST: Final = f"sha256:{'d' * 64}"
CAPSULE_DIGEST: Final = f"sha256:{'e' * 64}"

#: An interval no test can outlast, so a Run measured against it is live.
LONG: Final = 86_400


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb the way the server does."""
    return asyncio.run(methods.dispatch(method, ctx, params))


def configure(canary: CanaryProvision, runtime: dict[str, Any]) -> None:
    """Write *runtime* as the repository layer's ``runtime`` block."""
    path = canary.root / ".ea" / "config.yaml"
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    document = existing if isinstance(existing, dict) else {}
    document.setdefault("runtime", {}).update(runtime)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def announce_as(ctx: MethodContext, canary: CanaryProvision, provider_id: str) -> None:
    """Bind the Run and have its worker announce itself as *provider_id*."""
    call(
        RUN_BIND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        compiled_spec_digest=SPEC_DIGEST,
        authority_capsule_digest=CAPSULE_DIGEST,
        route_policy_revision=3,
    )
    answer = call(
        RUN_WORKER_HELLO_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        actor=ACTOR,
        hello={
            "run_ref": RUN_URN,
            "provider_session_ref": "session-17",
            "driver_manifest_digest": f"sha256:{'1' * 64}",
            "provider_id": provider_id,
            "sdk_version": "1.2.3",
            "auth_kind": "subscription",
            "model_id": "model-a",
            "os_class": "macos",
            "worker_protocol_version": "1.0.0",
            "event_codec_version": "1.0.0",
            "compiled_spec_digest": SPEC_DIGEST,
            "authority_capsule_digest": CAPSULE_DIGEST,
            "capabilities_digest": f"sha256:{'2' * 64}",
            "hello_sequence": 1,
        },
    )
    assert answer["disposition"] == "accepted"


def act(ctx: MethodContext, canary: CanaryProvision) -> None:
    """Record one activity, so there is silence to measure from."""
    call(
        RUN_EVENT_APPEND_METHOD,
        ctx,
        repo_root=str(canary.root),
        urn=RUN_URN,
        event_ref="EVT-0000000a",
        run_sequence=1,
        event_kind="reasoning_started",
        payload={"payload_kind": "reasoning_summary", "phase": "started"},
        actor=ACTOR,
    )


def stall(ctx: MethodContext, canary: CanaryProvision, **overrides: Any) -> dict[str, Any]:
    """Read the Run's liveness through the daemon verb."""
    answer = call(RUN_EVENTS_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN, **overrides)
    return answer


# ---------------------------------------------------------------------------
# RUN-029: the interval is per runtime, and the stall carries its resume path
# ---------------------------------------------------------------------------


def test_run_029_an_unannounced_run_is_measured_against_the_default(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    act(ctx, canary)

    answer = stall(ctx, canary)["stall"]

    assert answer["interval_seconds"] == DEFAULT_STALL_INTERVAL_SECONDS
    assert answer["verdict"] == RunLiveness.LIVE.value


def test_run_029_the_announced_runtime_s_interval_is_the_one_applied(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}, "claude": {"stall_interval_s": LONG}})
    announce_as(ctx, canary, "codex")
    act(ctx, canary)

    answer = stall(ctx, canary)["stall"]

    assert answer["interval_seconds"] == 0
    assert answer["verdict"] == RunLiveness.STALLED.value


def test_run_029_another_runtime_s_interval_is_not_applied(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}, "claude": {"stall_interval_s": LONG}})
    announce_as(ctx, canary, "claude")
    act(ctx, canary)

    answer = stall(ctx, canary)["stall"]

    assert answer["interval_seconds"] == LONG
    assert answer["verdict"] == RunLiveness.LIVE.value


def test_run_029_a_runtime_with_no_block_falls_back_to_the_default(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    announce_as(ctx, canary, "opencode")
    act(ctx, canary)

    assert stall(ctx, canary)["stall"]["interval_seconds"] == DEFAULT_STALL_INTERVAL_SECONDS


def test_run_029_a_runtime_outside_the_catalog_is_measured_against_the_default(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "custom")
    act(ctx, canary)

    assert stall(ctx, canary)["stall"]["interval_seconds"] == DEFAULT_STALL_INTERVAL_SECONDS


def test_run_029_an_explicit_interval_overrides_the_configured_one(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "codex")
    act(ctx, canary)

    assert stall(ctx, canary, stall_interval_seconds=LONG)["stall"]["verdict"] == "live"


def test_run_029_a_stall_carries_the_last_activity_the_elapsed_interval_and_a_resume_path(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 0}})
    announce_as(ctx, canary, "codex")
    act(ctx, canary)

    answer = stall(ctx, canary)["stall"]

    assert answer["last_activity_kind"] == "reasoning_started"
    assert answer["last_activity_at"] is not None
    assert answer["elapsed_seconds"] >= 0.0
    assert (answer["resume_method"], answer["resume_control"]) == (
        "runtime.run.control.request",
        "resume",
    )


def test_run_029_a_malformed_runtime_block_is_refused_rather_than_defaulted(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    configure(canary, {"codex": {"stall_interval_s": 60, "stall_after": 5}})
    announce_as(ctx, canary, "codex")

    with pytest.raises(ValidationError, match="stall_after"):
        stall(ctx, canary)


def test_run_029_a_negative_explicit_interval_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="stall_interval_seconds"):
        stall(ctx, canary, stall_interval_seconds=-1)


# ---------------------------------------------------------------------------
# RUN-030: a stall labels the Run lost and ends nothing
# ---------------------------------------------------------------------------


def test_run_030_a_stalled_running_run_is_labelled_lost(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    act(ctx, canary)

    answer = stall(ctx, canary, stall_interval_seconds=0)

    assert answer["stall"]["ambiguity"] == AmbiguityLabel.LOST.value
    assert answer["run_status"] == "RUNNING"


def test_run_030_a_live_run_carries_no_lost_label(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    act(ctx, canary)

    assert stall(ctx, canary, stall_interval_seconds=LONG)["stall"]["ambiguity"] is None


def test_run_030_a_run_with_nothing_to_measure_is_not_labelled_lost(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    answer = stall(ctx, canary, stall_interval_seconds=0)["stall"]

    assert answer["verdict"] == RunLiveness.UNKNOWN.value
    assert answer["ambiguity"] is None


def test_run_030_a_stall_never_terminates_the_run(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    announce_as(ctx, canary, "codex")
    act(ctx, canary)

    for _ in range(3):
        assert stall(ctx, canary, stall_interval_seconds=0)["stall"]["verdict"] == "stalled"

    contract = call(RUN_CONTRACT_READ_METHOD, ctx, repo_root=str(canary.root), urn=RUN_URN)
    assert contract["status"] == "RUNNING"


def test_run_030_lost_is_neither_a_success_nor_a_terminal_status() -> None:
    lost = AmbiguityLabel.LOST.value.upper()

    assert lost not in SUCCESS_TERMINALS[LifecycleEntity.RUN]
    assert lost not in {status.value for status in TERMINAL_RUN_STATUSES}
