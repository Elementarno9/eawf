"""Host fan-out keys: where a runtime's own scheduler reads the concurrency plan.

A written orchestration protocol does not make a host fan out. Measured,
the host with no fan-out section averaged 2.3 children per session against
3.6 on the configured one while carrying the protocol the whole time, so an
instruction with no host key behind it is a defect in the surface rather
than a contract. Where a runtime exposes fan-out configuration, the plugin
installers write the plan into it; where one exposes none, the absence is
recorded here and the dispatcher -- the in-flight governor and the lease
scheduler -- enforces the contract instead.

Key names are read from the installed binaries, never from documentation:
Codex's from its configuration deserializer, Claude Code's from the
environment variables its binary reads. A key is written only when a
fact sets its value: the Run ceiling of the in-flight governor sets the
thread count, the orchestration contract's one level of delegation sets
the depth, and the coordinator's own reasoning effort, stepped down one
rung, sets the fan-out effort. The coordinator's effort is read and never
written, so only fan-out work is stepped down. The default subagent model
and the job runtime ceiling have no such fact yet, so they are not
written.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from eawf.kernel.economics.governor import InFlightGovernor
from eawf.observability.telemetry.models import RuntimeName

#: A fan-out setting, named by what it controls rather than by any host.
FanOutSetting = Literal[
    "max_concurrent_threads",
    "max_depth",
    "subagent_model",
    "subagent_effort",
    "job_max_runtime",
]

#: The settings the concurrency plan itself determines. A runtime that
#: exposes one of them and leaves it unwritten fails preflight.
PLAN_SETTINGS: Final[tuple[FanOutSetting, ...]] = ("max_concurrent_threads", "max_depth")

#: How many levels of delegation the orchestration contract admits: the
#: coordinator dispatches Task Runs, and a Task Run executes rather than
#: coordinating in turn.
FAN_OUT_DEPTH: Final = 1

#: Codex's reasoning-effort rungs, lowest first, as its deserializer
#: spells them.
CODEX_EFFORT_LADDER: Final[tuple[str, ...]] = ("minimal", "low", "medium", "high", "xhigh")


@dataclass(frozen=True, slots=True, kw_only=True)
class HostFanOutKeys:
    """The fan-out keys one runtime exposes, or why it exposes none.

    Attributes:
        runtime: The runtime described.
        keys: The host spelling of each setting it exposes, as a dotted
            path into its configuration document.
        source: Where the key names were read.
        absent_reason: Why the runtime exposes no key; set exactly when
            ``keys`` is empty.
    """

    runtime: RuntimeName
    keys: Mapping[FanOutSetting, str]
    source: str
    absent_reason: str | None = None

    def __post_init__(self) -> None:
        """Refuse a record that neither names keys nor explains their absence.

        Raises:
            ValueError: ``keys`` is empty with no reason, or non-empty
                with one.
        """
        if bool(self.keys) == (self.absent_reason is not None):
            raise ValueError(
                f"{self.runtime} fan-out record names keys or the reason it has none, not both"
            )


#: Every supported runtime's fan-out surface.
HOST_FAN_OUT: Final[Mapping[RuntimeName, HostFanOutKeys]] = MappingProxyType(
    {
        "codex": HostFanOutKeys(
            runtime="codex",
            keys=MappingProxyType(
                {
                    "max_concurrent_threads": "agents.max_concurrent_threads_per_session",
                    "max_depth": "agents.max_depth",
                    "subagent_model": "agents.default_subagent_model",
                    "subagent_effort": "agents.default_subagent_reasoning_effort",
                    "job_max_runtime": "agents.job_max_runtime_seconds",
                }
            ),
            source="codex-cli 0.154.0 configuration deserializer",
        ),
        "claude": HostFanOutKeys(
            runtime="claude",
            keys=MappingProxyType(
                {
                    "max_concurrent_threads": "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
                    "max_depth": "env.CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
                    "subagent_model": "env.CLAUDE_CODE_SUBAGENT_MODEL",
                }
            ),
            source="claude 2.1.285 binary environment reads",
        ),
        "opencode": HostFanOutKeys(
            runtime="opencode",
            keys=MappingProxyType({}),
            source="no binary inspected",
            absent_reason=(
                "no fan-out key has been read from an installed opencode binary, so the "
                "dispatcher enforces the orchestration contract on its own"
            ),
        ),
    }
)


def step_down_effort(effort: str) -> str:
    """Return the Codex reasoning effort one rung below *effort*.

    Args:
        effort: The coordinator's configured effort.

    Returns:
        The rung below it; the lowest rung steps down to itself.

    Raises:
        ValueError: *effort* is not a Codex effort rung.
    """
    if effort not in CODEX_EFFORT_LADDER:
        raise ValueError(f"{effort!r} is not a reasoning effort: {CODEX_EFFORT_LADDER}")
    return CODEX_EFFORT_LADDER[max(CODEX_EFFORT_LADDER.index(effort) - 1, 0)]


def fan_out_values(
    runtime: RuntimeName,
    *,
    governor: InFlightGovernor,
    coordinator_effort: str | None = None,
) -> dict[str, str | int]:
    """Return each fan-out key *runtime* should carry, keyed by its host path.

    Args:
        runtime: The runtime whose configuration is written.
        governor: The in-flight governor whose Run ceiling bounds every
            stage of every derived plan.
        coordinator_effort: The coordinator's configured reasoning effort,
            or ``None`` when it sets none, in which case no fan-out effort
            is written because there is nothing to step down from.

    Returns:
        Host path to value; empty for a runtime that exposes no key.

    Raises:
        ValueError: *coordinator_effort* is not a Codex effort rung.
    """
    keys = HOST_FAN_OUT[runtime].keys
    facts: dict[FanOutSetting, str | int] = {
        "max_concurrent_threads": governor.max_concurrent_runs,
        "max_depth": FAN_OUT_DEPTH,
    }
    if coordinator_effort is not None:
        facts["subagent_effort"] = step_down_effort(coordinator_effort)
    return {keys[setting]: value for setting, value in facts.items() if setting in keys}


def unwritten_plan_keys(runtime: RuntimeName, document: Mapping[str, object]) -> tuple[str, ...]:
    """Return the plan keys *runtime* exposes that *document* leaves unset.

    Args:
        runtime: The runtime whose configuration is checked.
        document: Its parsed configuration document.

    Returns:
        The dotted host paths of every unset plan key, in plan order;
        empty for a runtime that exposes none.
    """
    keys = HOST_FAN_OUT[runtime].keys
    missing: list[str] = []
    for setting in PLAN_SETTINGS:
        path = keys.get(setting)
        if path is None:
            continue
        table, _, name = path.rpartition(".")
        section = document.get(table)
        if not isinstance(section, Mapping) or name not in section:
            missing.append(path)
    return tuple(missing)


__all__ = [
    "CODEX_EFFORT_LADDER",
    "FAN_OUT_DEPTH",
    "HOST_FAN_OUT",
    "PLAN_SETTINGS",
    "FanOutSetting",
    "HostFanOutKeys",
    "fan_out_values",
    "step_down_effort",
    "unwritten_plan_keys",
]
