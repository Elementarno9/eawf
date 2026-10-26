"""The budget meter verb weighs cache reads as native dispatch does.

Dispatch meters a Run with the cache-read weight of the governor it loaded,
so a long session re-reading a large cached prefix stays under its cap. The
``runtime.run.budget.meter`` verb meters the same Run, and must answer the
same total and the same verdict, or it would report a Run over its cap that
dispatch is still letting run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
import yaml

from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, native_dispatch
from eawf.runtime.daemon.admission import load_economics
from eawf.runtime.daemon.budget_interlock import guard_in_flight_budget
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, run_budget
from eawf.runtime.daemon.methods.run_budget import RUN_BUDGET_METER_METHOD
from eawf.runtime.runtimes.metering import UsageSample
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
GOVERNOR: Final = {
    "max_concurrent_runs": 8,
    "max_in_flight_tokens": 4_000_000,
    "admission": "queue",
}

#: A weight off the 0.1 default, so a verb that fell back to the default
#: or to one for one would land on a different total.
WEIGHT: Final = 0.25
CAP: Final = 200_000
SAMPLE: Final = {"input_tokens": 10_000, "output_tokens": 5_000, "cache_read_input_tokens": 500_000}


def declare(canary: CanaryProvision, **economics: Any) -> None:
    """Write the repository layer's ``economics`` table."""
    path = canary.root / ".ea" / "config.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    path.write_text(yaml.safe_dump({**(document or {}), "economics": economics}), encoding="utf-8")


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


def meter_verb(ctx: MethodContext, canary: CanaryProvision) -> dict[str, Any]:
    """Drive the meter verb over the one cache-heavy reading under the sealed cap."""
    params: dict[str, Any] = {
        "repo_root": str(canary.root),
        "urn": RUN_URN,
        "control_request_ref": "CTL-0000000a",
        "actor": ACTOR,
        "samples": [SAMPLE],
        "base_budget": CAP,
        "enforce": "hard",
        "multiplier": 1.0,
        "idempotency_key": "idem-budget",
    }
    return asyncio.run(methods.dispatch(RUN_BUDGET_METER_METHOD, ctx, params))


def dispatch_verdict(canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Meter the reading the way native dispatch does and return its outcome.

    The real guard runs; the spy only keeps the reading and the decision it
    produced. The reading stays under the cap, so the meter writes nothing
    and needs no live root context.
    """
    seen: list[Any] = []

    def spy(**kwargs: Any) -> Any:
        outcome = guard_in_flight_budget(**kwargs)
        seen.append(SimpleNamespace(reading=kwargs["reading"], outcome=outcome))
        return outcome

    monkeypatch.setattr(run_budget, "guard_in_flight_budget", spy)
    meter = native_dispatch.run_meter(
        cast(Any, None),
        cast(Any, SimpleNamespace(urn=RUN_URN, actor=ACTOR)),
        attempt=cast(Any, SimpleNamespace(attempt_ref="ATT-0001")),
        capsule=cast(Any, SimpleNamespace(budget=SimpleNamespace(tokens=CAP))),
        identity=cast(Any, None),
        cache_read_weight=load_economics(canary.root).governor.cache_read_weight,
    )
    assert meter is not None
    asyncio.run(meter.observe(UsageSample(**SAMPLE), None))
    monkeypatch.undo()
    (only,) = seen
    return only


def test_the_meter_verb_weights_cache_reads_like_dispatch(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """10k in, 5k out and 500k cache read at 0.25 meter 140k under a 200k cap."""
    declare(canary, governor={**GOVERNOR, "cache_read_weight": WEIGHT})
    assert load_economics(canary.root).governor.cache_read_weight == pytest.approx(WEIGHT)

    dispatched = dispatch_verdict(canary, monkeypatch)
    answer = meter_verb(ctx, canary)

    assert dispatched.reading.observed_tokens == 140_000
    assert answer["observed_tokens"] == dispatched.reading.observed_tokens
    assert answer["output_tokens"] == dispatched.reading.output_tokens
    assert answer["cap_tokens"] == dispatched.outcome.decision.cap == CAP
    assert answer["over_cap"] is dispatched.outcome.decision.over_cap is False
    assert answer["action"] == dispatched.outcome.decision.action.value
    assert answer["terminated"] is False
    assert answer["notice"] is None


def test_the_meter_verb_refuses_an_invalid_economics_table(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """A governor that breaks its policy refuses the verb instead of metering one for one."""
    declare(canary, governor={**GOVERNOR, "cache_read_weight": 1.5})

    with pytest.raises(DaemonValidationError, match="economics configuration is invalid"):
        meter_verb(ctx, canary)
