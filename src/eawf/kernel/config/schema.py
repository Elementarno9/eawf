"""Strict typed models for layered configuration sections."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from eawf.kernel.state.enums import AgentSessionRole

CommitSubjectStyle = Literal["bracket", "trailer"]

#: A runtime adapter ``runtime.preference`` and ``runtime.adapters`` may name.
RuntimeAdapterId = Literal["claude-code", "codex", "opencode"]
#: Every runtime adapter id, in declaration order: the set the runtime lists are ticked from.
RUNTIME_ADAPTER_IDS: Final[tuple[str, ...]] = get_args(RuntimeAdapterId)

#: The unit of one verified delivery commit: under ``batch`` the daemon
#: squashes a Batch's sealed candidates at integration; ``task`` keeps one
#: delivery commit per Task.
IntegrationCommitUnit = Literal["batch", "task"]
#: How a delivery commit names its Task: a ``Task:`` trailer, an in-subject
#: identifier, or nothing beyond the provenance manifest.
TaskReference = Literal["trailer", "subject", "none"]
ReleaseCadence = Literal["manual", "per-phase"]
VerifyWaiverMode = Literal["A", "B", "C", "disabled"]
#: How many toasts a console raises for state changes nobody keyed (``ui.toasts``).
ToastVerbosity = Literal["off", "important", "all"]

#: Wildcard key under ``agents.extra_tools`` whose grant applies to every role.
ALL_ROLES: str = "*"


class AutoChoose(StrEnum):
    """Whether an ``AskUserQuestion`` auto-picks its recommended option.

    The ladder, loosest last:

    - :attr:`OFF` — never auto-pick; always surface the question (default).
    - :attr:`RECOMMENDED` — auto-pick only when the surface marks one
      option as recommended; otherwise surface the question.
    - :attr:`ALWAYS` — auto-pick the recommended option whenever one
      exists, surfacing nothing.
    """

    OFF = "off"
    RECOMMENDED = "recommended"
    ALWAYS = "always"


class EuBasis(StrEnum):
    """Captured quantity used to convert runtime counters into elapsed EU."""

    API_DURATION = "api_duration"
    TOKENS = "tokens"
    WALL_CLOCK = "wall_clock"


class VcsReleaseConventionsConfig(BaseModel):
    """Release cadence conventions under ``vcs.conventions``."""

    model_config = ConfigDict(extra="forbid")

    cadence: ReleaseCadence = "manual"


class VcsConventionsConfig(BaseModel):
    """VCS commit-message convention preferences."""

    model_config = ConfigDict(extra="forbid")

    subject_style: CommitSubjectStyle = "trailer"
    wave_trailer: str = Field(default="Eawf-Wave", min_length=1)
    release: VcsReleaseConventionsConfig = Field(default_factory=VcsReleaseConventionsConfig)


class EstimationConfig(BaseModel):
    """Strict typed model for the ``estimation`` config section."""

    model_config = ConfigDict(extra="forbid")

    eu_minutes: float = Field(default=30.0, gt=0.0)
    eu_basis: EuBasis = EuBasis.API_DURATION


class PreferencesConfig(BaseModel):
    """Strict typed model for the ``preferences`` config section.

    The AskUserQuestion auto-pick default. The field is a closed enum so an
    unknown value fails validation at the loader boundary.
    """

    model_config = ConfigDict(extra="forbid")

    auto_choose: AutoChoose = AutoChoose.OFF


class VerifyConfig(BaseModel):
    """Strict layered-config contract for the ``verify`` section.

    Gate booleans are tighten-only when profile and repo values compose. The
    profile resolver owns that cross-layer fold; this model owns value shape
    and rejects misspelled waiver modes before lifecycle code sees them.
    """

    model_config = ConfigDict(extra="forbid")

    odr_blocking: bool = False
    require_iter_audit_accepted: bool = False
    waiver_mode: VerifyWaiverMode = "B"
    juror_wall_clock_seconds: float = Field(default=600.0, gt=0.0)
    retyped_rule_threshold: int = Field(default=3, ge=1)


class AgentsConfig(BaseModel):
    """Strict typed model for the ``agents`` config section.

    Mounts :attr:`extra_tools`, the per-role grant of runtime tool names the
    renderer appends to each subagent's built-in allowlist. The mechanism is
    deliberately tool-agnostic — eawf names no specific tool or MCP server, so
    a workstation grants whatever its own tool installation provides without a
    source edit, and the grant travels in whichever config layer suits its
    blast radius (machine-wide grants belong in the global layer, not a repo's
    committed ``.ea/``).

    Attributes:
        extra_tools: Role key to extra tool names. Each key is an
            :class:`~eawf.kernel.state.enums.AgentSessionRole` value, or
            :data:`ALL_ROLES` for tools granted to every role. Defaults to
            empty so an unconfigured repo renders exactly the built-in
            allowlists.
    """

    model_config = ConfigDict(extra="forbid")

    extra_tools: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("extra_tools")
    @classmethod
    def _validate_extra_tools(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Reject an unknown role key or a blank tool name.

        A misspelled role would silently grant nothing, and a blank name
        would render an empty slot into the frontmatter tool list; both fail
        here rather than at agent-spawn time.
        """
        allowed = {ALL_ROLES} | {role.value for role in AgentSessionRole}
        unknown = sorted(key for key in value if key not in allowed)
        if unknown:
            raise ValueError(
                f"unknown agents.extra_tools role key(s): {unknown!r}; "
                f"expected one of {sorted(allowed)!r}"
            )
        for role, tools in value.items():
            if any(not tool.strip() for tool in tools):
                raise ValueError(f"agents.extra_tools[{role!r}] holds a blank tool name")
        return value


class RuntimeModelsConfig(BaseModel):
    """Strict typed model for the ``runtime.models`` config block.

    Per-runtime override of the dispatch routing tier ladder. The built-in
    ladder (:data:`eawf.workflow.dispatch.routing._RUNTIME_TIER_MODEL`) pins a
    ``(cheap, mid, top)`` model triple per runtime; this block lets a repo
    swap that triple for a runtime whose account serves different model ids.
    The motivating case: a ChatGPT-account codex rejects the API-key-only
    ``gpt-5-mini`` / ``gpt-5`` / ``gpt-5-codex`` defaults, so the repo pins the
    ids that account actually serves (``gpt-5.3-codex-spark`` / ``gpt-5.5``).

    Each field is the 3-tier ladder (cheapest, mid, top) for that runtime; a
    runtime left ``None`` falls back to the built-in default ladder, so a
    partial override (e.g. only ``codex``) is honoured. Every id must be a key
    the cost ledger can price (exact or longest-prefix) so a spawn meters
    honestly.

    Attributes:
        claude: Optional ``(cheap, mid, top)`` model-id triple for the claude
            lane; ``None`` keeps the built-in haiku/sonnet/opus ladder.
        codex: Optional ``(cheap, mid, top)`` model-id triple for the codex
            lane; ``None`` keeps the built-in OpenAI ladder.
        opencode: Optional ``(cheap, mid, top)`` model-id triple for the
            opencode lane; ``None`` keeps the built-in ``provider/model`` ladder.
    """

    model_config = ConfigDict(extra="forbid")

    claude: tuple[str, str, str] | None = None
    codex: tuple[str, str, str] | None = None
    opencode: tuple[str, str, str] | None = None

    def as_override(self) -> dict[str, tuple[str, str, str]]:
        """Project the set lanes into a ``runtime -> (cheap, mid, top)`` map.

        Returns:
            A mapping with one entry per runtime whose ladder is overridden;
            runtimes left ``None`` are omitted so the routing resolver falls
            back to the built-in ladder for them. Empty when nothing is set.
        """
        out: dict[str, tuple[str, str, str]] = {}
        for runtime in ("claude", "codex", "opencode"):
            ladder = getattr(self, runtime)
            if ladder is not None:
                out[runtime] = ladder
        return out


#: How long a Run may produce nothing before it is flagged stalled. Six
#: hundred seconds is the measured boundary of the unrecovered stalls
#: that motivated the check, not a chosen round number.
DEFAULT_STALL_INTERVAL_SECONDS: Final = 600

#: The runtimes a ``runtime.<name>`` liveness block may be written for.
STALL_INTERVAL_RUNTIMES: Final[tuple[str, ...]] = ("claude", "codex", "opencode")

#: How long the host's permission hook waits for a decision recorded here before
#: it answers nothing and leaves the call to the host's own prompt.
DEFAULT_PERMISSION_WAIT_SECONDS: Final = 20

#: The longest that wait may be set to. Claude Code kills a command hook after 60
#: seconds unless its entry sets a timeout, and the installed entry sets none, so
#: the cap leaves the hook's own start-up and the answer room inside that window;
#: a hook killed mid-wait would hand the host nothing at all.
MAX_PERMISSION_WAIT_SECONDS: Final = 50


class RuntimeLivenessConfig(BaseModel):
    """Strict typed model for one ``runtime.<name>`` liveness block.

    The interval is per runtime because a pause that is routine on one
    runtime is pathological on another.

    Attributes:
        stall_interval_s: The silence a Run on this runtime is allowed
            before it is flagged stalled.
        permission_wait_s: How long the host's permission hook waits for a
            principal's decision before leaving the call to the host's own
            prompt; ``0`` records the call and never waits.
    """

    model_config = ConfigDict(extra="forbid")

    stall_interval_s: StrictInt = Field(default=DEFAULT_STALL_INTERVAL_SECONDS, ge=0, le=86_400)
    permission_wait_s: StrictInt = Field(
        default=DEFAULT_PERMISSION_WAIT_SECONDS, ge=0, le=MAX_PERMISSION_WAIT_SECONDS
    )


__all__ = [
    "ALL_ROLES",
    "DEFAULT_PERMISSION_WAIT_SECONDS",
    "DEFAULT_STALL_INTERVAL_SECONDS",
    "MAX_PERMISSION_WAIT_SECONDS",
    "RUNTIME_ADAPTER_IDS",
    "STALL_INTERVAL_RUNTIMES",
    "AgentsConfig",
    "AutoChoose",
    "CommitSubjectStyle",
    "EstimationConfig",
    "EuBasis",
    "IntegrationCommitUnit",
    "PreferencesConfig",
    "ReleaseCadence",
    "RuntimeAdapterId",
    "RuntimeLivenessConfig",
    "RuntimeModelsConfig",
    "TaskReference",
    "ToastVerbosity",
    "VcsConventionsConfig",
    "VcsReleaseConventionsConfig",
    "VerifyConfig",
    "VerifyWaiverMode",
]
