"""Strict typed models for layered configuration sections."""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from eawf.kernel.state.enums import AgentSessionRole

CommitSubjectStyle = Literal["bracket", "trailer"]

#: A runtime adapter ``runtime.preference`` and ``runtime.adapters`` may name.
RuntimeAdapterId = Literal["claude-code", "codex", "opencode"]

#: The unit of one verified delivery commit: under ``batch`` the daemon
#: squashes a Batch's sealed candidates at integration; ``task`` keeps one
#: delivery commit per Task.
IntegrationCommitUnit = Literal["batch", "task"]
#: How a delivery commit names its Task: a ``Task:`` trailer, an in-subject
#: identifier, or nothing beyond the provenance manifest.
TaskReference = Literal["trailer", "subject", "none"]
ReleaseCadence = Literal["manual", "per-phase"]
AgentDrivenReleasePolicy = Literal["manual", "per-phase"]
VerifyWaiverMode = Literal["A", "B", "C", "disabled"]

#: Wildcard key under ``agents.extra_tools`` whose grant applies to every role.
ALL_ROLES: str = "*"


class AutoChoose(StrEnum):
    """Whether an ``AskUserQuestion`` auto-picks its recommended option.

    Mirrors the closed ``ask | auto | never``-style ladders used by the
    other operator-gate preferences (e.g. ``vcs.auto_commit``):

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
    agent_driven: AgentDrivenReleasePolicy = "per-phase"


class VcsConventionsConfig(BaseModel):
    """VCS commit-message convention preferences."""

    model_config = ConfigDict(extra="forbid")

    subject_style: CommitSubjectStyle = "trailer"
    wave_trailer: str = Field(default="Eawf-Wave", min_length=1)
    release: VcsReleaseConventionsConfig = Field(default_factory=VcsReleaseConventionsConfig)


class EstimationDisplayConfig(BaseModel):
    """Display preferences under the ``estimation`` config section."""

    model_config = ConfigDict(extra="forbid")

    show_category: bool = False
    show_raw_eu: bool = True
    show_expected_time: bool = True
    show_pessimistic_time: bool = True
    eu_quantum: float = Field(default=0.25, gt=0.0)
    time_quantum_under_2h_minutes: int = Field(default=15, gt=0)
    time_quantum_over_2h_minutes: int = Field(default=30, gt=0)


class EstimationConfig(BaseModel):
    """Strict typed model for the ``estimation`` config section."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    eu_minutes: float = Field(default=30.0, gt=0.0)
    eu_basis: EuBasis = EuBasis.API_DURATION
    realtime_recalibration: bool = False
    calibration_profile: str = "eawf_v0_lockbox_2026_05"
    idle_policy: str = "D30_non_agent_gap"
    display: EstimationDisplayConfig = Field(default_factory=EstimationDisplayConfig)


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


class ProseLevel(StrEnum):
    """Strictness ladder for the doc-clarity prose stack, loosest to strictest.

    The doc-clarity prose lints run at one of three escalating
    strictness levels. The order is load-bearing: it is what the
    authority guard compares so a local repo layer may only *tighten*
    (move toward :attr:`STRICT`), never *loosen* below the baseline the
    CI profile sets.

    - :attr:`LOOSE` — the managed-repo default. Prose lints run
      advisory-only; nothing blocks on a clarity finding.
    - :attr:`STANDARD` — the neutral middle. The deterministic prose
      lints block; the heavier checks (Vale, the LLM clarity judge)
      stay advisory.
    - :attr:`STRICT` — the agent-driven default. Every prose lint
      blocks and the LLM clarity-judge gate is on.
    """

    LOOSE = "loose"
    STANDARD = "standard"
    STRICT = "strict"


#: Strictness rank per :class:`ProseLevel` (higher == stricter). The
#: authority guard compares ranks so "tighten" / "loosen" is a numeric
#: ``>=`` rather than a brittle string comparison. Kept beside the enum so
#: a new level forces a matching rank entry (a missing key raises ``KeyError``
#: in :func:`prose_level_rank`).
_PROSE_LEVEL_RANK: dict[ProseLevel, int] = {
    ProseLevel.LOOSE: 0,
    ProseLevel.STANDARD: 1,
    ProseLevel.STRICT: 2,
}


def prose_level_rank(level: ProseLevel) -> int:
    """Return the strictness rank of *level* (higher is stricter).

    Args:
        level: The prose strictness level to rank.

    Returns:
        The integer rank — ``0`` for :attr:`ProseLevel.LOOSE` up to ``2``
        for :attr:`ProseLevel.STRICT`.

    Raises:
        KeyError: when *level* has no rank registered in
            :data:`_PROSE_LEVEL_RANK` (a programming error introduced by
            adding an enum member without a matching rank row).
    """
    return _PROSE_LEVEL_RANK[level]


class ProseConfig(BaseModel):
    """Strict typed model for the ``prose`` config section (doc-clarity).

    Mounts the operator-tunable knobs for the doc-clarity prose-lint
    stack. The single load-bearing field is :attr:`level`; the boolean
    overrides let a layer toggle an individual gate *within* the floor
    its :attr:`level` already implies, but they can never drop below it
    (the authority guard at :func:`assert_prose_not_weaker_than` owns the
    cross-layer "tighten-only" invariant).

    The default is :attr:`ProseLevel.STANDARD` so a repo that declares no
    ``prose`` block still gets the deterministic lints blocking; the
    agent-driven profile raises the floor to :attr:`ProseLevel.STRICT`
    and the managed profile relaxes it to :attr:`ProseLevel.LOOSE`.

    Attributes:
        level: The strictness floor for the whole prose stack. The
            authority guard rejects a local layer that sets this below
            the baseline level (typically the profile / CI layer's value).
        clarity_judge: Whether the Layer-3 LLM clarity judge runs as a
            gate. ``None`` (default) defers to the level (on at
            :attr:`ProseLevel.STRICT`, off otherwise); an explicit bool
            opts the gate on or off within the level's floor.
        block_on_lint: Whether the deterministic prose lints block
            (vs advisory). ``None`` (default) defers to the level (block
            at :attr:`ProseLevel.STANDARD` and above).
    """

    model_config = ConfigDict(extra="forbid")

    level: ProseLevel = ProseLevel.STANDARD
    clarity_judge: bool | None = None
    block_on_lint: bool | None = None

    @property
    def rank(self) -> int:
        """Strictness rank of this config's :attr:`level` (higher is stricter)."""
        return prose_level_rank(self.level)

    def tightens_or_equals(self, baseline: ProseConfig) -> bool:
        """Return whether this config is at least as strict as *baseline*.

        The cross-layer authority invariant in one boolean: a local layer
        is allowed iff its :attr:`level` is not below the baseline's.

        Args:
            baseline: The baseline config a local layer may only tighten
                (typically the profile / CI layer's resolved value).

        Returns:
            ``True`` when this config's level rank is ``>=`` the
            baseline's, i.e. it tightens or matches the baseline.
        """
        return self.rank >= baseline.rank


def assert_prose_not_weaker_than(baseline: ProseConfig, candidate: ProseConfig) -> ProseConfig:
    """Reject a *candidate* prose config that loosens below *baseline*.

    The doc-clarity authority guard: a local repo / user layer may only
    *tighten* the prose baseline the CI-side profile sets (agent-driven =
    strict, managed = loose), never loosen it. Tightening (raising the
    level toward :attr:`ProseLevel.STRICT`) and matching the baseline are
    both accepted; loosening (dropping the level rank) is rejected.

    Args:
        baseline: The baseline a local layer may not drop below — the
            value resolved from the profile / CI layer.
        candidate: The local-layer value to validate against the baseline.

    Returns:
        *candidate* unchanged when it tightens or matches *baseline* (so
        the call site can use the return value inline).

    Raises:
        ValueError: when *candidate*'s level is strictly looser than
            *baseline*'s — the message names both levels so the operator
            sees the floor they tripped.
    """
    if not candidate.tightens_or_equals(baseline):
        raise ValueError(
            f"local prose level {candidate.level.value!r} loosens below the "
            f"baseline {baseline.level.value!r}; local config may only tighten "
            f"the prose baseline, never loosen it"
        )
    return candidate


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
    "STALL_INTERVAL_RUNTIMES",
    "AgentDrivenReleasePolicy",
    "AgentsConfig",
    "AutoChoose",
    "CommitSubjectStyle",
    "EstimationConfig",
    "EstimationDisplayConfig",
    "EuBasis",
    "IntegrationCommitUnit",
    "PreferencesConfig",
    "ProseConfig",
    "ProseLevel",
    "ReleaseCadence",
    "RuntimeAdapterId",
    "RuntimeLivenessConfig",
    "RuntimeModelsConfig",
    "TaskReference",
    "VcsConventionsConfig",
    "VcsReleaseConventionsConfig",
    "VerifyConfig",
    "VerifyWaiverMode",
    "assert_prose_not_weaker_than",
    "prose_level_rank",
]
