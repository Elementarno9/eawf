"""Operator-tunable config-key metadata registry for the ``eawf config`` menu.

This module hosts :data:`CONFIG_REGISTRY` — the operator-tunable subset
surfaced by the interactive ``eawf config`` menu (P20-W10) and the TUI
config hotkey (P20-W11). One :class:`ConfigKey` row per menu entry,
ordered alphabetical-by-key for diff hygiene.

Public API (menu surface — pre-P25):

- :class:`ConfigKeyType` — narrow Literal of supported value shapes.
- :class:`ConfigKey` — frozen Pydantic record describing one tunable key.
- :data:`CONFIG_REGISTRY` — the canonical ordered tuple of registry entries.
- :func:`tabs_sorted` — alphabetical tab names extracted from the registry.
- :func:`keys_for_tab` — alphabetical :class:`ConfigKey` list for one tab.
- :func:`registry_lookup` — locate an entry by dotted key.

Ordering policy (success criteria, P20-W10 wave brief):

* Tabs are returned alphabetical.
* Fields **within** a tab are returned alphabetical by dotted key.

The ordering policy is part of the menu's UX contract — the registry stores
entries in alphabetical-by-key order for self-documentation, and the
accessor helpers enforce the sort at the boundary so callers never need to
re-sort.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH, RESEARCH_DEPTH_VALUES

# Narrow Literal of value shapes the registry handles. ``str`` covers all
# free-text scalars (paths included — the menu does not validate path
# existence). ``choice`` and ``multichoice`` require ``choices`` to be set.
# Adding a new kind requires a matching branch in :func:`coerce_and_validate`
# and the questionary dispatcher in :mod:`eawf.surfaces.cli.commands.config`.
ConfigKeyType = Literal["bool", "int", "float", "str", "choice", "multichoice"]


class ConfigKey(BaseModel):
    """One tunable config key in the metadata registry.

    Attributes:
        tab: Tab grouping the key belongs to (one of the high-level config
            sections — ``runtime``, ``vcs``, ``ui``, etc.). Tabs are sorted
            alphabetically when surfaced.
        key: Dotted config key path (e.g. ``"runtime.default"``). Matches
            the form accepted by :func:`eawf.surfaces.cli.commands.config.config_get`.
        label: Short one-line human-readable label rendered as the prompt
            in the menu and as the field title in the TUI surface.
        type: Value shape — see :data:`ConfigKeyType`. Drives the questionary
            widget choice and the coercion in :func:`coerce_and_validate`.
        default: Default value for the key (string form for free-text;
            typed for bool / int / float; chosen literal for ``choice``).
            Surfaced in the menu as the pre-filled prompt value.
        description: Optional longer-form help text. Rendered as
            ``instruction`` below the prompt in questionary.
        choices: Allowed values for ``choice`` / ``multichoice``; ``None``
            for the other kinds. Each entry must be a string for the menu
            to render it. Validators reject empty choice lists.
        min_value: Inclusive lower bound for ``int`` / ``float``. ``None``
            disables the check.
        max_value: Inclusive upper bound for ``int`` / ``float``. ``None``
            disables the check.
        multiline: Hint that a ``str`` field may hold a multi-line value.
            The TUI config surface routes such a field to the popup
            single-field editor rather than the inline in-row input so the
            operator gets room to type; scalar ``str`` fields without the
            hint edit in place. Ignored for non-``str`` types.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tab: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    label: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    type: ConfigKeyType
    default: Any
    description: str = ""
    choices: tuple[str, ...] | None = None
    min_value: float | None = None
    max_value: float | None = None
    multiline: bool = False

    @field_validator("choices")
    @classmethod
    def _validate_choices(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Reject an empty choices tuple — a choice key with no options is unreachable."""
        if value is not None and not value:
            raise ValueError("choices must be non-empty when set")
        return value


# Canonical ordered registry. Stored alphabetical-by-key for diff hygiene;
# the accessor helpers re-sort on read so the menu UX never depends on file
# ordering. Tab strings are stable identifiers — renaming a tab is a
# breaking change that requires a parallel edit in the TUI surface.
CONFIG_REGISTRY: tuple[ConfigKey, ...] = (
    ConfigKey(
        tab="audit",
        key="audit.default_level",
        label="Default /audit check-plan breadth",
        type="choice",
        default="standard",
        description="quick = narrow smoke set; standard = the full default set; deep = widest.",
        choices=("quick", "standard", "deep"),
    ),
    ConfigKey(
        tab="audit",
        key="audit.fix_safe",
        label="Reserved: apply safe audit fixes",
        type="bool",
        default=False,
        description="Reserved for compatibility; /audit never mutates source automatically.",
    ),
    ConfigKey(
        tab="audit",
        key="audit.flaky_retry_count",
        label="Retries for flaky audit checks",
        type="int",
        default=1,
        description="Number of times to retry a check before recording it as failed.",
        min_value=0,
        max_value=5,
    ),
    ConfigKey(
        tab="daemon",
        key="daemon.idle_timeout_seconds",
        label="Daemon idle shutdown timeout (seconds)",
        type="int",
        default=300,
        description="Seconds the daemon stays alive after the last request before idle shutdown.",
        min_value=0,
    ),
    ConfigKey(
        tab="daemon",
        key="daemon.proxy_enabled",
        label="Route mutations through the daemon",
        type="bool",
        default=True,
        description="When False, mutations bypass the daemon and use portalocker direct-writes.",
    ),
    ConfigKey(
        tab="daemon",
        key="daemon.session_handle_ttl_seconds",
        label="Session-handle time-to-live (seconds)",
        type="int",
        default=86400,
        description="Seconds a daemon session handle remains valid before it expires.",
        min_value=0,
    ),
    ConfigKey(
        tab="planning",
        key="dispatch.role_tier_token_cap",
        label="Role-tier dispatch block token cap",
        type="int",
        default=2400,
        description="Token ceiling per injected role-tier block; over-cap raises, never truncates.",
        min_value=1,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.eu_quantum",
        label="EU rounding quantum for display",
        type="float",
        default=0.25,
        description="Estimation units are rounded to this quantum when rendered.",
        min_value=0.0,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.show_category",
        label="Show estimation category in displays",
        type="bool",
        default=False,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.show_expected_time",
        label="Show expected time in displays",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.show_pessimistic_time",
        label="Show pessimistic time in displays",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.show_raw_eu",
        label="Show raw EU values in displays",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.time_quantum_over_2h_minutes",
        label="Time rounding quantum over 2h (minutes)",
        type="int",
        default=30,
        description="Rendered times above 2 hours are rounded to this many minutes.",
        min_value=1,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.display.time_quantum_under_2h_minutes",
        label="Time rounding quantum under 2h (minutes)",
        type="int",
        default=15,
        description="Rendered times up to 2 hours are rounded to this many minutes.",
        min_value=1,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.enabled",
        label="Enable estimation calibration",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.eu_basis",
        label="Elapsed EU basis",
        type="choice",
        default="api_duration",
        description="Captured quantity used to convert runtime counters into elapsed EU.",
        choices=("api_duration", "tokens", "wall_clock"),
    ),
    ConfigKey(
        tab="estimation",
        key="estimation.eu_minutes",
        label="Minutes per estimation unit (EU)",
        type="int",
        default=30,
        description="Calibration anchor for estimation budgeting; project-specific.",
        min_value=5,
        max_value=240,
    ),
    ConfigKey(
        tab="flow",
        key="flow.ask_on_decisions",
        label="Surface a prompt at workflow decision points",
        type="bool",
        default=True,
        description="When True, the flow pauses for operator input at decision gates.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.audit",
        label="Auto-accept the /audit step",
        type="bool",
        default=False,
        description="When True, /audit proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.polish",
        label="Auto-accept the /polish step",
        type="bool",
        default=False,
        description="When True, /polish proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.prep",
        label="Auto-accept the /prep step",
        type="bool",
        default=False,
        description="When True, /prep proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.research",
        label="Auto-accept the /research step",
        type="bool",
        default=False,
        description="When True, /research proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.review",
        label="Auto-accept the /review step",
        type="bool",
        default=False,
        description="When True, /review proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.auto_accept.ship",
        label="Auto-accept the /ship step",
        type="bool",
        default=False,
        description="When True, /ship proceeds without an operator confirmation gate.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.budget.enforce",
        label="Token-budget enforcement mode",
        type="choice",
        default="soft",
        description="soft = warn and continue past the cap; hard = halt the wave at the cap.",
        choices=("soft", "hard"),
    ),
    ConfigKey(
        tab="flow",
        key="flow.budget.multiplier",
        label="Token-budget safety multiplier",
        type="float",
        default=1.5,
        description="Safety multiplier on a wave's base budget to derive the enforced cap.",
        min_value=1.0,
    ),
    ConfigKey(
        tab="flow",
        key="flow.max_repair_cycles",
        label="Reserved: max repair cycles per flow stage",
        type="int",
        default=3,
        description="Reserved for compatibility; this value does not bound repair execution.",
        min_value=0,
    ),
    ConfigKey(
        tab="planning",
        key="planning.approval",
        label="Reserved: approval mode for plan apply",
        type="choice",
        default="ask",
        description="Reserved for compatibility; roadmap approval remains explicit.",
        choices=("ask", "auto", "never"),
    ),
    ConfigKey(
        tab="planning",
        key="planning.auto_plan",
        label="Reserved: skip plan-mode proposal on /prep",
        type="bool",
        default=False,
        description="Reserved for compatibility; /prep always presents the plan gate.",
    ),
    ConfigKey(
        tab="planning",
        key="planning.max_parallel_waves",
        label="Maximum parallel waves",
        type="int",
        default=4,
        description="Repo-wide hard cap for waves in claimed or in-progress status.",
        min_value=1,
        max_value=16,
    ),
    ConfigKey(
        tab="planning",
        key="planning.require_research_for_unknowns",
        label="Require /research for residual unknowns",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="flow",
        key="polish.auto_apply_safe",
        label="Auto-apply safe polish groups",
        type="bool",
        default=False,
        description="When True, /polish applies safe finding groups without the report-only pause.",
    ),
    ConfigKey(
        tab="flow",
        key="polish.deletion_policy",
        label="Polish deletion policy",
        type="str",
        default="recoverable_with_reason",
        description="How /polish treats deletions; policy changes surface via AskUserQuestion.",
    ),
    ConfigKey(
        tab="preferences",
        key="preferences.auto_choose",
        label="AskUserQuestion auto-pick policy",
        type="choice",
        default="off",
        description=(
            "off = always surface the question (default); recommended = "
            "auto-pick only a recommended option; always = auto-pick when one exists."
        ),
        choices=("off", "recommended", "always"),
    ),
    ConfigKey(
        tab="preferences",
        key="preferences.scope_size",
        label="Preferred default effort bucket",
        type="choice",
        default="M",
        description="Default effort bucket the planner reaches for when sizing a wave.",
        choices=("XS", "S", "M", "L", "XL"),
    ),
    ConfigKey(
        tab="preferences",
        key="preferences.solution_bias",
        label="Planner solution-complexity bias",
        type="choice",
        default="balanced",
        description="simple = fewer/smaller waves; balanced (default); thorough = broader.",
        choices=("simple", "balanced", "thorough"),
    ),
    ConfigKey(
        tab="planning",
        key="prep.auto_resume",
        label="Emit `eawf dispatch resume` before each claim batch",
        type="bool",
        default=True,
        description="When True, /prep leads its claim actions with a dispatch-resume command.",
    ),
    ConfigKey(
        tab="prose",
        key="prose.level",
        label="Doc-clarity prose-lint strictness",
        type="choice",
        default="standard",
        description=(
            "loose = advisory only (managed default); standard = deterministic "
            "lints block; strict = every lint blocks + clarity judge on "
            "(agent-driven default). A local layer may only tighten this."
        ),
        choices=("loose", "standard", "strict"),
    ),
    ConfigKey(
        tab="research",
        key="research.agent_count",
        label="Default subagent count for /research",
        type="int",
        default=4,
        min_value=1,
        max_value=12,
    ),
    ConfigKey(
        tab="research",
        key="research.auto_save",
        label="Auto-save research drafts on close",
        type="bool",
        default=False,
    ),
    ConfigKey(
        tab="research",
        key="research.default_depth",
        label="Default research depth",
        type="choice",
        default=DEFAULT_RESEARCH_DEPTH.value,
        choices=RESEARCH_DEPTH_VALUES,
    ),
    ConfigKey(
        tab="research",
        key="research.default_sources",
        label="Default source mix",
        type="choice",
        default="both",
        choices=("docs", "web", "both"),
    ),
    ConfigKey(
        tab="runtime",
        key="runtime.adapter_catalog.claude.enabled",
        label="Enable the Claude runtime adapter",
        type="bool",
        default=True,
        description="When True, the Claude adapter is available for dispatch.",
    ),
    ConfigKey(
        tab="runtime",
        key="runtime.adapter_catalog.codex.enabled",
        label="Enable the Codex runtime adapter",
        type="bool",
        default=False,
        description="When True, the Codex adapter is available for dispatch.",
    ),
    ConfigKey(
        tab="runtime",
        key="runtime.adapter_catalog.opencode.enabled",
        label="Enable the opencode runtime adapter",
        type="bool",
        default=False,
        description="When True, the opencode adapter is available for dispatch.",
    ),
    ConfigKey(
        tab="runtime",
        key="runtime.default",
        label="Default runtime adapter",
        type="choice",
        default="claude",
        description="Selected when no per-command override is supplied.",
        choices=("claude", "codex", "opencode"),
    ),
    ConfigKey(
        tab="ship",
        key="ship.gauntlet",
        label="Ship gauntlet breadth",
        type="choice",
        default="full",
        description="full (default, mandatory for migration/iter-close) runs all gates; "
        "scoped is for re-runs only.",
        choices=("full", "scoped"),
    ),
    ConfigKey(
        tab="ship",
        key="ship.require_audit_pass",
        label="Reserved: require audit pass before ship",
        type="bool",
        default=True,
        description="Reserved for compatibility; this value does not gate ship.",
    ),
    ConfigKey(
        tab="ship",
        key="ship.require_memory_review",
        label="Block ship until memory review is recorded",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="statusline",
        key="statusline.color_mode",
        label="Statusline colour policy",
        type="choice",
        default="auto",
        description="auto = off on a no-color terminal; always = force ANSI; never = plain.",
        choices=("auto", "always", "never"),
    ),
    ConfigKey(
        tab="statusline",
        key="statusline.glyph_mode",
        label="Statusline glyph mode",
        type="choice",
        default="auto",
        description="auto = downgrade to ascii on a no-color terminal; ascii = plain; unicode.",
        choices=("auto", "ascii", "unicode"),
    ),
    ConfigKey(
        tab="statusline",
        key="statusline.rows",
        label="Statusline row count",
        type="int",
        default=1,
        description="Number of rows the multi-line statusline renderer emits (1..3).",
        min_value=1,
        max_value=3,
    ),
    ConfigKey(
        tab="telemetry",
        key="telemetry.aggregate_window",
        label="Default aggregation window",
        type="str",
        default="24h",
        description="Rolling window used when aggregating telemetry counters.",
    ),
    ConfigKey(
        tab="telemetry",
        key="telemetry.db_kind",
        label="Telemetry database backend",
        type="choice",
        default="sqlite",
        description="sqlite = always-available stdlib backend; duckdb = opt-in analytics upgrade.",
        choices=("duckdb", "sqlite"),
    ),
    ConfigKey(
        tab="telemetry",
        key="telemetry.enabled",
        label="Enable telemetry collection",
        type="bool",
        default=False,
        description="Telemetry is opt-in and strict-local; no data leaves the machine.",
    ),
    ConfigKey(
        tab="telemetry",
        key="telemetry.export.format",
        label="Telemetry export format",
        type="choice",
        default="prom",
        choices=("prom", "json", "csv"),
    ),
    ConfigKey(
        tab="telemetry",
        key="telemetry.window_default",
        label="Default reporting window",
        type="str",
        default="7d",
        description="Default look-back window for telemetry reports.",
    ),
    ConfigKey(
        tab="ui",
        key="ui.bare_command",
        label="Behaviour of bare `eawf` invocation",
        type="choice",
        default="tui",
        description=(
            "tui = launch interactive dashboard (default); status = print "
            "non-interactive status line."
        ),
        choices=("tui", "status"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.color",
        label="Color output policy",
        type="choice",
        default="auto",
        choices=("auto", "always", "never"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.dashboard_panes",
        label="Dashboard panes shown on the repo screen",
        type="multichoice",
        default=(),
        description="Subset of dashboard panes to render; empty falls back to the built-in set.",
        choices=("state", "roadmap", "hypotheses", "audits", "ship", "memory", "config"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.glyphs",
        label="Glyph rendering mode",
        type="choice",
        default="auto",
        description="auto = detect glyph coverage; ascii = plain fallback; unicode = force.",
        choices=("auto", "ascii", "unicode"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.refresh_ms",
        label="Refresh interval for the TUI (ms)",
        type="int",
        default=1000,
        min_value=100,
        max_value=10000,
    ),
    ConfigKey(
        tab="ui",
        key="ui.theme",
        label="TUI colour theme",
        type="choice",
        default="dark",
        description=(
            "dark = Wong colour-blind-safe (default); cb = IBM "
            "colour-blind-safe; light = light background; auto = detect."
        ),
        choices=("dark", "light", "cb", "auto"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.toasts",
        label="Ambient state-change toast verbosity",
        type="choice",
        default="important",
        description=(
            "off = no toasts; important = wave close / audit verdict / "
            "needs-user only (default); all = more verbose."
        ),
        choices=("off", "important", "all"),
    ),
    ConfigKey(
        tab="ui",
        key="ui.tour_completed",
        label="First-run onboarding tour completed",
        type="bool",
        default=False,
        description=(
            "True once the operator has dismissed the first-run tour; the "
            "tour modal sets this on dismissal so it does not re-open."
        ),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.auto_commit",
        label="Auto-commit policy",
        type="choice",
        default="ask",
        description="ask = prompt; auto = commit without prompting; never = require manual.",
        choices=("ask", "auto", "never"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.auto_push",
        label="Auto-push policy",
        type="choice",
        default="ask",
        choices=("ask", "auto", "never"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.conventions.release.agent_driven",
        label="Agent-driven release cadence",
        type="choice",
        default="per-phase",
        description="Cadence used when the agent-driven profile owns release policy.",
        choices=("manual", "per-phase"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.conventions.release.cadence",
        label="Release cadence",
        type="choice",
        default="manual",
        description=(
            "manual = releases ride explicit operator action; "
            "per-phase = phase close prepares release."
        ),
        choices=("manual", "per-phase"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.pr_open",
        label="Open PR automatically on ship",
        type="choice",
        default="ask",
        choices=("ask", "auto", "never"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.require_ci_green",
        label="Require CI green before merge",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="verify",
        key="verify.require_iter_audit_accepted",
        label="Require an accepted iter audit",
        type="bool",
        default=False,
        description="Require a completed accepted evaluation audit before iter close.",
    ),
    ConfigKey(
        tab="verify",
        key="verify.waiver_mode",
        label="Operator waiver policy",
        type="choice",
        default="B",
        description="A/B/C preserve existing policies; disabled is absorbing across profiles.",
        choices=("A", "B", "C", "disabled"),
    ),
    ConfigKey(
        tab="worktrees",
        key="worktrees.enabled",
        label="Worktree dispatch policy",
        type="choice",
        default="auto",
        description="auto = enable when wave dep DAG permits; always | never.",
        choices=("auto", "always", "never"),
    ),
    ConfigKey(
        tab="worktrees",
        key="worktrees.use_for_parallel_writers",
        label="Use worktrees for parallel writers",
        type="bool",
        default=True,
    ),
)


# Ordering invariants — asserted at module load so a future edit cannot
# silently break the diff-hygiene contract.
assert len({entry.key for entry in CONFIG_REGISTRY}) == len(CONFIG_REGISTRY), (
    "CONFIG_REGISTRY keys must be unique"
)
assert list(CONFIG_REGISTRY) == sorted(CONFIG_REGISTRY, key=lambda e: e.key), (
    "CONFIG_REGISTRY entries must be stored sorted by key"
)


def tabs_sorted() -> tuple[str, ...]:
    """Return the unique tab names in alphabetical order.

    Used by the menu's outer ``select`` widget; the sort is performed once
    per invocation so callers never depend on the storage order.
    """
    return tuple(sorted({entry.tab for entry in CONFIG_REGISTRY}))


def keys_for_tab(tab: str) -> tuple[ConfigKey, ...]:
    """Return all :class:`ConfigKey` entries under *tab*, alphabetical by key.

    Args:
        tab: Tab name (case-sensitive). Unknown tabs return an empty tuple
            so the menu can defensively render an empty list.
    """
    matched = [entry for entry in CONFIG_REGISTRY if entry.tab == tab]
    return tuple(sorted(matched, key=lambda e: e.key))


def registry_lookup(key: str) -> ConfigKey | None:
    """Return the :class:`ConfigKey` whose ``key`` matches *key*, or ``None``.

    Args:
        key: Dotted config key (e.g. ``"vcs.auto_commit"``).
    """
    for entry in CONFIG_REGISTRY:
        if entry.key == key:
            return entry
    return None
