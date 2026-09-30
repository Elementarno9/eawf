"""Operator-tunable config-key metadata registry for the ``eawf config`` menu.

This module hosts :data:`CONFIG_REGISTRY` — the operator-tunable subset
surfaced by the interactive ``eawf config`` menu and the TUI
config hotkey. One :class:`ConfigKey` row per menu entry,
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
        key: Dotted config key path (e.g. ``"ui.theme"``). Matches
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
        tab="daemon",
        key="daemon.idle_timeout_seconds",
        label="Daemon idle shutdown timeout (seconds)",
        type="int",
        default=300,
        description="Seconds the daemon stays alive after the last request before idle shutdown.",
        min_value=1,
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
        min_value=1,
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
        key="flow.advance_after.audit",
        label="Advance from /audit to /polish",
        type="bool",
        default=False,
        description="When True, /flow advances after audit without a flow-level pause.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.advance_after.polish",
        label="Advance from /polish to /ship",
        type="bool",
        default=False,
        description="When True, /flow advances after polish without a flow-level pause.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.advance_after.prep",
        label="Advance from /prep to /audit",
        type="bool",
        default=False,
        description="When True, /flow advances after prep without a flow-level pause.",
    ),
    ConfigKey(
        tab="flow",
        key="flow.advance_after.research",
        label="Advance from /research to /prep",
        type="bool",
        default=False,
        description="When True, /flow advances after research without a flow-level pause.",
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
        label="Max repair cycles per flow stage",
        type="int",
        default=3,
        description="Maximum bounded repair re-entry count for a failing flow stage.",
        min_value=0,
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
        tab="planning",
        key="prep.auto_resume",
        label="Lead each claim batch with the dispatch-resume action",
        type="bool",
        default=True,
        description="When True, /prep leads its claim actions with a dispatch-resume command.",
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
        tab="review",
        key="review.default_level",
        label="Default review confidence threshold",
        type="choice",
        default="medium",
        choices=("low", "medium", "high"),
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
        default=True,
        description="Telemetry is on by default and strict-local; no data leaves the machine.",
    ),
    ConfigKey(
        tab="tui",
        key="tui.eu_view.density",
        label="Roadmap EU/hour rollup density",
        type="choice",
        default="full",
        description="full = with a detail column (default); compact = EU and hours only.",
        choices=("full", "compact"),
    ),
    ConfigKey(
        tab="tui",
        key="tui.eu_view.fields",
        label="Roadmap EU/hour rollup metrics",
        type="multichoice",
        default=("work_sum", "critical_path", "queue", "realistic"),
        description="The metrics the rollup lists per phase, in this order.",
        choices=("work_sum", "critical_path", "queue", "realistic"),
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
            "off = no ambient toasts; important = wave close / audit verdict / "
            "needs-user (default); all = more verbose. Answers to a key always show."
        ),
        choices=("off", "important", "all"),
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
        key="vcs.integration_commit_unit",
        label="Delivery commit unit",
        type="choice",
        default="batch",
        description=(
            "batch = squash a Batch's sealed candidates into one delivery commit "
            "at integration; task = one delivery commit per Task."
        ),
        choices=("batch", "task"),
    ),
    ConfigKey(
        tab="vcs",
        key="vcs.task_reference",
        label="Task reference in delivery commits",
        type="choice",
        default="trailer",
        description=(
            "trailer = a 'Task:' trailer; subject = an in-subject identifier; "
            "none = the provenance manifest is the only link."
        ),
        choices=("trailer", "subject", "none"),
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
    return tuple(sorted({entry.tab for entry in _editable_registry()}))


def keys_for_tab(tab: str) -> tuple[ConfigKey, ...]:
    """Return all :class:`ConfigKey` entries under *tab*, alphabetical by key.

    Args:
        tab: Tab name (case-sensitive). Unknown tabs return an empty tuple
            so the menu can defensively render an empty list.
    """
    matched = [entry for entry in _editable_registry() if entry.tab == tab]
    return tuple(sorted(matched, key=lambda e: e.key))


def _editable_registry() -> tuple[ConfigKey, ...]:
    """Return operator keys backed by active, non-reserved catalog rows."""
    from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY

    return tuple(
        entry
        for entry in CONFIG_REGISTRY
        if (leaf := LEAF_KEY_REGISTRY.get(entry.key)) is not None and not leaf.reserved
    )


def registry_lookup(key: str) -> ConfigKey | None:
    """Return the :class:`ConfigKey` whose ``key`` matches *key*, or ``None``.

    Args:
        key: Dotted config key (e.g. ``"ui.theme"``).
    """
    for entry in CONFIG_REGISTRY:
        if entry.key == key:
            return entry
    return None
