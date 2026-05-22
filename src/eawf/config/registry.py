"""Typed metadata registry for the interactive ``eawf config`` menu
plus the C08 leaf-key catalog.

This module hosts two related-but-distinct registries:

1. :data:`CONFIG_REGISTRY` — the operator-tunable subset surfaced by the
   interactive ``eawf config`` menu (P20-W10) and the TUI config hotkey
   (P20-W11). One :class:`ConfigKey` row per menu entry, ordered for
   diff hygiene.
2. :data:`LEAF_KEY_REGISTRY` — the full ~150-key catalog (P25-W14 / C08
   §5.2) covering every leaf in the layered config. Each entry is a
   :class:`LeafKey` record naming its declared type, default, and the
   list of layers that may write it. The catalog is what the daemon
   uses to reject ``unknown config key: <key!r>`` writes; the menu
   never iterates the full set.

Public API (menu surface — pre-P25):

- :class:`ConfigKeyType` — narrow Literal of supported value shapes.
- :class:`ConfigKey` — frozen Pydantic record describing one tunable key.
- :data:`CONFIG_REGISTRY` — the canonical ordered tuple of registry entries.
- :func:`tabs_sorted` — alphabetical tab names extracted from the registry.
- :func:`keys_for_tab` — alphabetical :class:`ConfigKey` list for one tab.
- :func:`registry_lookup` — locate an entry by dotted key.
- :func:`coerce_and_validate` — turn a raw string answer into a typed value
  matching the entry's declared type, raising :class:`InvalidInput` on
  failure.

Public API (leaf-key catalog — P25-W14 / C08):

- :class:`LeafKey` — frozen Pydantic record (name, declared type, default,
  writable layers, optional description / domain).
- :data:`LEAF_KEY_REGISTRY` — read-only mapping ``dotted_key → LeafKey``.
- :func:`leaf_key_lookup` — strict lookup; raises ``ValueError`` on
  unknown keys with the canonical ``unknown config key: <key!r>``
  message.
- :func:`is_known_leaf_key` — ``True`` when the dotted path resolves to
  a :data:`LEAF_KEY_REGISTRY` entry.

Ordering policy (success criteria, P20-W10 wave brief):

* Tabs are returned alphabetical.
* Fields **within** a tab are returned alphabetical by dotted key.

The ordering policy is part of the menu's UX contract — the registry stores
entries in alphabetical-by-key order for self-documentation, and the
accessor helpers enforce the sort at the boundary so callers never need to
re-sort.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from eawf.cli.errors import InvalidInput

logger = logging.getLogger(__name__)


# Narrow Literal of value shapes the registry handles. ``str`` covers all
# free-text scalars (paths included — the menu does not validate path
# existence). ``choice`` and ``multichoice`` require ``choices`` to be set.
# Adding a new kind requires a matching branch in :func:`coerce_and_validate`
# and the questionary dispatcher in :mod:`eawf.cli.commands.config`.
ConfigKeyType = Literal["bool", "int", "float", "str", "choice", "multichoice"]


class ConfigKey(BaseModel):
    """One tunable config key in the metadata registry.

    Attributes:
        tab: Tab grouping the key belongs to (one of the high-level config
            sections — ``runtime``, ``vcs``, ``ui``, etc.). Tabs are sorted
            alphabetically when surfaced.
        key: Dotted config key path (e.g. ``"runtime.default"``). Matches
            the form accepted by :func:`eawf.cli.commands.config.config_get`.
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
        key="audit.fix_safe",
        label="Apply safe fixes automatically during audit",
        type="bool",
        default=False,
        description="When True, low-risk lint/format fixes are applied without prompting.",
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
        tab="estimation",
        key="estimation.enabled",
        label="Enable estimation calibration",
        type="bool",
        default=True,
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
        tab="planning",
        key="planning.approval",
        label="Approval mode for plan apply",
        type="choice",
        default="ask",
        description="Where the operator confirmation gate sits on /roadmap apply.",
        choices=("ask", "auto", "never"),
    ),
    ConfigKey(
        tab="planning",
        key="planning.auto_plan",
        label="Skip plan-mode proposal on /prep",
        type="bool",
        default=False,
        description="When True, /prep dispatches the planned DAG without an inline proposal.",
    ),
    ConfigKey(
        tab="planning",
        key="planning.max_parallel_waves",
        label="Maximum waves dispatched in parallel",
        type="int",
        default=4,
        description="Upper bound for parallel worktree dispatch within an iter.",
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
        default="normal",
        choices=("shallow", "normal", "deep"),
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
        key="runtime.default",
        label="Default runtime adapter",
        type="choice",
        default="claude",
        description="Selected when no per-command override is supplied.",
        choices=("claude", "codex", "opencode"),
    ),
    ConfigKey(
        tab="ship",
        key="ship.require_audit_pass",
        label="Block ship until audit verdict is pass",
        type="bool",
        default=True,
    ),
    ConfigKey(
        tab="ship",
        key="ship.require_memory_review",
        label="Block ship until memory review is recorded",
        type="bool",
        default=True,
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


def _coerce_bool(raw: str | bool) -> bool:
    """Coerce a string answer to bool. Empty / unknown raises :class:`InvalidInput`."""
    if isinstance(raw, bool):
        return raw
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "y", "1", "on"):
        return True
    if lowered in ("false", "no", "n", "0", "off"):
        return False
    raise InvalidInput(f"cannot coerce {raw!r} to bool")


def _coerce_number(raw: str | int | float, *, want_int: bool) -> int | float:
    """Coerce a string answer to int or float.

    Raises:
        InvalidInput: When the string fails the requested numeric parse.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if want_int else float(raw)
    text = str(raw).strip()
    try:
        return int(text) if want_int else float(text)
    except ValueError as exc:
        kind = "int" if want_int else "float"
        raise InvalidInput(f"cannot coerce {raw!r} to {kind}") from exc


def coerce_and_validate(entry: ConfigKey, raw: Any) -> Any:
    """Convert *raw* into the typed value declared by *entry*.

    Args:
        entry: Registry entry describing the key.
        raw: Raw answer from the menu — typically a string from questionary,
            but already-typed values (e.g. bool from ``questionary.confirm``)
            are accepted unchanged.

    Returns:
        The coerced + range-checked value, ready to be written to the YAML
        layer through the existing :func:`_atomic_write_yaml` helper.

    Raises:
        InvalidInput: When the raw value cannot be parsed as the declared
            type, is outside the declared range, or is not one of the
            declared choices.
    """
    if entry.type == "bool":
        return _coerce_bool(raw)
    if entry.type == "int":
        value_int = _coerce_number(raw, want_int=True)
        if entry.min_value is not None and value_int < entry.min_value:
            raise InvalidInput(f"value {value_int} below minimum {entry.min_value} for {entry.key}")
        if entry.max_value is not None and value_int > entry.max_value:
            raise InvalidInput(f"value {value_int} above maximum {entry.max_value} for {entry.key}")
        return value_int
    if entry.type == "float":
        value_float = _coerce_number(raw, want_int=False)
        if entry.min_value is not None and value_float < entry.min_value:
            raise InvalidInput(
                f"value {value_float} below minimum {entry.min_value} for {entry.key}"
            )
        if entry.max_value is not None and value_float > entry.max_value:
            raise InvalidInput(
                f"value {value_float} above maximum {entry.max_value} for {entry.key}"
            )
        return value_float
    if entry.type == "str":
        return str(raw)
    if entry.type == "choice":
        text = str(raw)
        if entry.choices is None or text not in entry.choices:
            raise InvalidInput(
                f"value {text!r} not in choices {list(entry.choices or ())} for {entry.key}"
            )
        return text
    if entry.type == "multichoice":
        # Multichoice accepts a sequence (tuple/list) or a comma-separated string.
        if isinstance(raw, (list, tuple)):
            items = [str(item) for item in raw]
        else:
            items = [chunk.strip() for chunk in str(raw).split(",") if chunk.strip()]
        if entry.choices is None:
            raise InvalidInput(f"multichoice key {entry.key} declared without choices")
        unknown = [item for item in items if item not in entry.choices]
        if unknown:
            raise InvalidInput(
                f"value(s) {unknown!r} not in choices {list(entry.choices)} for {entry.key}"
            )
        return items
    raise InvalidInput(f"unknown registry type: {entry.type}")


def _registry_self_check_defaults() -> None:
    """Module-load assertion: each entry's ``default`` coerces under its declared type.

    Failing this assertion at import time is loud — a future contributor
    cannot land a registry entry whose default contradicts its declared
    type or violates its declared range / choices.
    """
    for entry in CONFIG_REGISTRY:
        try:
            coerce_and_validate(entry, entry.default)
        except InvalidInput as exc:  # pragma: no cover  asserted, not branched
            raise AssertionError(
                f"registry entry {entry.key!r}: default {entry.default!r} fails its own "
                f"declared validation ({exc})"
            ) from exc


_registry_self_check_defaults()


def is_known_key(merged: Mapping[str, Any] | None, key: str) -> bool:
    """Return True when *key* either has a registry entry or appears in *merged*.

    Helper for the menu's "save back to merged config" round-trip. The
    registry is the authoritative metadata source for menu UX, but the
    layered config may carry keys that pre-date the registry — the helper
    treats either presence as sufficient to flag the key as known.
    """
    if registry_lookup(key) is not None:
        return True
    if merged is None:
        return False
    cur: Any = merged
    for part in key.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False
        cur = cur[part]
    return True


# ---------------------------------------------------------------------------
# C08 leaf-key catalog (P25-W14)
# ---------------------------------------------------------------------------


#: Narrow Literal of supported leaf-key value shapes. Mirrors the YAML
#: scalars the loader writes; ``mapping`` covers dict-typed leaves
#: (e.g. ``workspace.repos``, ``profiles.trusted``); ``any`` is the
#: escape hatch for irregular shapes (e.g. ``project.success_metrics``
#: whose value type can vary).
LeafKeyType = Literal[
    "bool",
    "int",
    "float",
    "str",
    "list_str",
    "list_any",
    "mapping",
    "any",
    "literal",
]


class LeafKey(BaseModel):
    """One leaf-key row in the C08 layered-config catalog.

    Attributes:
        key: Dotted config key path (canonical form). Identical to the
            address used by :func:`eawf.config.layered.get_dotted` and
            the ``key_path`` parameter of the daemon's
            ``config.set_layer_value`` RPC.
        domain: High-level grouping (``daemon``, ``render``, ``runtime``,
            ``scope``, ``config``, ``profiles``, etc.). Mirrors brief
            §5.2's section titles so audits can ``group_by(domain)``.
        type: Value shape — see :data:`LeafKeyType`.
        default: Default value the built-in layer ships with.
        writable_layers: Tuple of layer labels that may write this
            leaf. ``()`` marks a code-only / locked leaf (e.g.
            ``schema_version``, ``language.runtime``).
        description: One-line human-readable summary.
        choices: Allowed values for ``literal``-typed leaves; ``None``
            for other shapes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    domain: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    type: LeafKeyType
    default: Any = None
    writable_layers: tuple[str, ...] = ()
    description: str = ""
    choices: tuple[str, ...] | None = None

    @field_validator("writable_layers")
    @classmethod
    def _validate_layers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject layer names outside the canonical taxonomy."""
        allowed = {
            "global",
            "workspace",
            "repo",
            "branch",
            "local",
            "wave",
            "env",
            "cli",
        }
        unknown = [layer for layer in value if layer not in allowed]
        if unknown:
            raise ValueError(f"unknown layer label(s): {unknown!r}")
        return value

    @field_validator("choices")
    @classmethod
    def _validate_choices(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        """Reject an empty choices tuple — a literal with no options is unreachable."""
        if value is not None and not value:
            raise ValueError("choices must be non-empty when set")
        return value


# Layer shorthands (read by every row below; reduces line-noise).
_WRITABLE_ALL_DURABLE: tuple[str, ...] = (
    "global",
    "workspace",
    "repo",
    "branch",
    "local",
)
_WRITABLE_GWR: tuple[str, ...] = ("global", "workspace", "repo")
_WRITABLE_REPO_ONLY: tuple[str, ...] = ("repo",)
_WRITABLE_PROJECT_GOALS: tuple[str, ...] = ("repo", "branch", "local")
_WRITABLE_RUNTIME_PREFERENCE: tuple[str, ...] = (
    "global",
    "workspace",
    "repo",
    "branch",
    "local",
    "env",
    "cli",
    "wave",
)
_WRITABLE_NONE: tuple[str, ...] = ()  # locked / code-only


_LEAF_KEYS: tuple[LeafKey, ...] = (
    # --- top-level + schema ------------------------------------------------
    LeafKey(
        key="schema_version",
        domain="config",
        type="literal",
        default="1.0",
        writable_layers=_WRITABLE_NONE,
        description="Marker for the on-disk layered-config schema shape.",
        choices=("1.0",),
    ),
    LeafKey(
        key="config.layers_visible",
        domain="config",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
        description="When false, eawf config get hides the layer-source column.",
    ),
    # --- cli ---------------------------------------------------------------
    LeafKey(
        key="cli.canonical_command",
        domain="cli",
        type="str",
        default="eawf",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="cli.preferred_command",
        domain="cli",
        type="str",
        default="eawf",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="cli.install_aliases",
        domain="cli",
        type="list_str",
        default=("ea",),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="cli.omit_ea_alias",
        domain="cli",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- project -----------------------------------------------------------
    LeafKey(
        key="project.code",
        domain="project",
        type="str",
        default=None,
        writable_layers=_WRITABLE_REPO_ONLY,
    ),
    LeafKey(
        key="project.title",
        domain="project",
        type="str",
        default=None,
        writable_layers=_WRITABLE_REPO_ONLY,
    ),
    LeafKey(
        key="project.slug",
        domain="project",
        type="str",
        default=None,
        writable_layers=_WRITABLE_REPO_ONLY,
    ),
    LeafKey(
        key="project.domains",
        domain="project",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_REPO_ONLY,
    ),
    LeafKey(
        key="project.default_subproject",
        domain="project",
        type="str",
        default=None,
        writable_layers=_WRITABLE_REPO_ONLY,
    ),
    LeafKey(
        key="project.goals",
        domain="project",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_PROJECT_GOALS,
        description="Free-form project-level goal strings.",
    ),
    LeafKey(
        key="project.success_metrics",
        domain="project",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_PROJECT_GOALS,
        description="Per-metric target value (float).",
    ),
    # --- workspace ---------------------------------------------------------
    LeafKey(
        key="workspace.enabled",
        domain="workspace",
        type="bool",
        default=False,
        writable_layers=("workspace", "global"),
    ),
    LeafKey(
        key="workspace.code",
        domain="workspace",
        type="str",
        default=None,
        writable_layers=("workspace",),
    ),
    LeafKey(
        key="workspace.state_path",
        domain="workspace",
        type="str",
        default=".ea/state.json",
        writable_layers=("workspace",),
    ),
    LeafKey(
        key="workspace.repos",
        domain="workspace",
        type="mapping",
        default={},
        writable_layers=("workspace",),
    ),
    # --- profiles ----------------------------------------------------------
    LeafKey(
        key="profiles.enabled",
        domain="profiles",
        type="list_str",
        default=("core",),
        writable_layers=_WRITABLE_ALL_DURABLE,
    ),
    LeafKey(
        key="profiles.catalog",
        domain="profiles",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_NONE,
    ),
    LeafKey(
        key="profiles.conflict_resolution",
        domain="profiles",
        type="literal",
        default="prompt",
        writable_layers=_WRITABLE_GWR,
        choices=("prompt", "fail", "first-wins"),
    ),
    LeafKey(
        key="profiles.safety_policy",
        domain="profiles",
        type="literal",
        default="strictest_wins",
        writable_layers=_WRITABLE_NONE,
        choices=("strictest_wins",),
    ),
    LeafKey(
        key="profiles.trusted",
        domain="profiles",
        type="mapping",
        default={},
        writable_layers=("repo", "branch"),
        description="Profile id → sha256 of last-trusted body.",
    ),
    # --- runtime -----------------------------------------------------------
    LeafKey(
        key="runtime.default",
        domain="runtime",
        type="str",
        default="claude",
        writable_layers=_WRITABLE_GWR,
        description="Deprecated alias of runtime.preference[0].",
    ),
    LeafKey(
        key="runtime.adapters",
        domain="runtime",
        type="list_str",
        default=("claude",),
        writable_layers=_WRITABLE_GWR,
        description="Legacy v1.1 selector; superseded by runtime.preference.",
    ),
    LeafKey(
        key="runtime.preference",
        domain="runtime",
        type="list_str",
        default=("claude",),
        writable_layers=_WRITABLE_RUNTIME_PREFERENCE,
        description="C08-canonical fallback ladder; first entry is primary.",
    ),
    LeafKey(
        key="runtime.fallback.on_errors",
        domain="runtime",
        type="list_str",
        default=(
            "RUNTIME_RATE_LIMIT",
            "RUNTIME_SERVER_ERROR",
            "RUNTIME_TIMEOUT",
            "RUNTIME_API_ERROR",
        ),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="runtime.fallback.retry_policy",
        domain="runtime",
        type="literal",
        default="hybrid",
        writable_layers=_WRITABLE_GWR,
        choices=("hybrid", "backoff", "immediate"),
    ),
    LeafKey(
        key="runtime.fallback.max_backoff_seconds",
        domain="runtime",
        type="int",
        default=90,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="runtime.slash_commands",
        domain="runtime",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # Per-adapter sub-keys (claude / codex / opencode).
    LeafKey(
        key="runtime.adapter_catalog.claude.enabled",
        domain="runtime",
        type="bool",
        default=True,
        writable_layers=("repo",),
    ),
    LeafKey(
        key="runtime.adapter_catalog.claude.plugin_install",
        domain="runtime",
        type="literal",
        default="ask",
        writable_layers=_WRITABLE_GWR,
        choices=("auto", "ask", "skip"),
    ),
    LeafKey(
        key="runtime.adapter_catalog.claude.skills_path",
        domain="runtime",
        type="str",
        default=".claude/skills",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="runtime.adapter_catalog.claude.agents_path",
        domain="runtime",
        type="str",
        default=".claude/agents",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="runtime.adapter_catalog.codex.enabled",
        domain="runtime",
        type="bool",
        default=False,
        writable_layers=("repo",),
    ),
    LeafKey(
        key="runtime.adapter_catalog.codex.status",
        domain="runtime",
        type="str",
        default="planned",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="runtime.adapter_catalog.opencode.enabled",
        domain="runtime",
        type="bool",
        default=False,
        writable_layers=("repo",),
    ),
    LeafKey(
        key="runtime.adapter_catalog.opencode.status",
        domain="runtime",
        type="str",
        default="deferred",
        writable_layers=_WRITABLE_GWR,
    ),
    # --- ui ----------------------------------------------------------------
    LeafKey(
        key="ui.bare_command",
        domain="ui",
        type="literal",
        default="tui",
        writable_layers=_WRITABLE_GWR,
        choices=("tui", "help", "status"),
    ),
    LeafKey(
        key="ui.color",
        domain="ui",
        type="literal",
        default="auto",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("auto", "always", "never"),
    ),
    LeafKey(
        key="ui.theme",
        domain="ui",
        type="literal",
        default="dark",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("dark", "light", "cb", "auto"),
        description="TUI colour theme: dark (Wong) / cb (IBM) / light / auto.",
    ),
    LeafKey(
        key="ui.glyphs",
        domain="ui",
        type="literal",
        default="auto",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("auto", "ascii", "unicode"),
    ),
    LeafKey(
        key="ui.refresh_ms", domain="ui", type="int", default=1000, writable_layers=_WRITABLE_GWR
    ),
    LeafKey(
        key="ui.dashboard_panes",
        domain="ui",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # --- telemetry (C09 surface) ------------------------------------------
    LeafKey(
        key="telemetry.enabled",
        domain="telemetry",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="telemetry.export.endpoint",
        domain="telemetry",
        type="str",
        default=None,
        writable_layers=("global",),
    ),
    LeafKey(
        key="telemetry.export.format",
        domain="telemetry",
        type="literal",
        default="prom",
        writable_layers=("global",),
        choices=("prom", "otlp", "json", "csv"),
    ),
    LeafKey(
        key="telemetry.window_default",
        domain="telemetry",
        type="str",
        default="7d",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="telemetry.aggregate_window",
        domain="telemetry",
        type="str",
        default="24h",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="telemetry.db_kind",
        domain="telemetry",
        type="literal",
        default="duckdb",
        writable_layers=("global",),
        choices=("duckdb", "sqlite"),
    ),
    # --- dispatch ----------------------------------------------------------
    LeafKey(
        key="dispatch.session_policy_default",
        domain="dispatch",
        type="literal",
        default="hybrid",
        writable_layers=_WRITABLE_GWR,
        choices=("fresh", "continue", "hybrid"),
    ),
    LeafKey(
        key="dispatch.session_handle_ttl_seconds",
        domain="dispatch",
        type="int",
        default=86400,
        writable_layers=("global",),
    ),
    # --- language ----------------------------------------------------------
    LeafKey(
        key="language.runtime",
        domain="language",
        type="literal",
        default="python",
        writable_layers=_WRITABLE_NONE,
        choices=("python",),
    ),
    LeafKey(
        key="language.fast_extras",
        domain="language",
        type="list_str",
        default=(),
        writable_layers=("global",),
    ),
    # --- storage -----------------------------------------------------------
    LeafKey(
        key="storage.state_path",
        domain="storage",
        type="str",
        default=".ea/state.json",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.stores_dir",
        domain="storage",
        type="str",
        default=".ea/stores",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.artifacts_dir",
        domain="storage",
        type="str",
        default=".ea/artifacts",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.rendered_dir",
        domain="storage",
        type="str",
        default=".ea/artifacts/rendered",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.generated_index",
        domain="storage",
        type="str",
        default=".ea/indexes/generated.json",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.content_addressed_blobs",
        domain="storage",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.commit_jsonl",
        domain="storage",
        type="str",
        default="all_nonlocal",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.max_inline_chars",
        domain="storage",
        type="int",
        default=2000,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="storage.lock_strategy",
        domain="storage",
        type="str",
        default="sibling_lockfiles",
        writable_layers=_WRITABLE_GWR,
    ),
    # --- research ----------------------------------------------------------
    LeafKey(
        key="research.folder",
        domain="research",
        type="str",
        default=".ea/artifacts/rendered/research",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="research.auto_save",
        domain="research",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="research.default_depth",
        domain="research",
        type="literal",
        default="normal",
        writable_layers=_WRITABLE_GWR,
        choices=("shallow", "normal", "deep"),
    ),
    LeafKey(
        key="research.default_sources",
        domain="research",
        type="literal",
        default="both",
        writable_layers=_WRITABLE_GWR,
        choices=("docs", "web", "both"),
    ),
    LeafKey(
        key="research.agent_count",
        domain="research",
        type="int",
        default=4,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- planning ----------------------------------------------------------
    LeafKey(
        key="planning.approval",
        domain="planning",
        type="literal",
        default="ask",
        writable_layers=_WRITABLE_GWR,
        choices=("ask", "auto", "never"),
    ),
    LeafKey(
        key="planning.max_parallel_waves",
        domain="planning",
        type="int",
        default=4,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="planning.require_research_for_unknowns",
        domain="planning",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="planning.auto_plan",
        domain="planning",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- estimation --------------------------------------------------------
    LeafKey(
        key="estimation.enabled",
        domain="estimation",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.eu_minutes",
        domain="estimation",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.realtime_recalibration",
        domain="estimation",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.calibration_profile",
        domain="estimation",
        type="str",
        default="eawf_v0_lockbox_2026_05",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.idle_policy",
        domain="estimation",
        type="str",
        default="D30_non_agent_gap",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.show_category",
        domain="estimation",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.show_raw_eu",
        domain="estimation",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.show_expected_time",
        domain="estimation",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.show_pessimistic_time",
        domain="estimation",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.eu_quantum",
        domain="estimation",
        type="float",
        default=0.25,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.time_quantum_under_2h_minutes",
        domain="estimation",
        type="int",
        default=15,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.display.time_quantum_over_2h_minutes",
        domain="estimation",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- audit -------------------------------------------------------------
    LeafKey(
        key="audit.default_checks",
        domain="audit",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="audit.fix_safe",
        domain="audit",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="audit.flaky_retry_count",
        domain="audit",
        type="int",
        default=1,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- ship --------------------------------------------------------------
    LeafKey(
        key="ship.require_audit_pass",
        domain="ship",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="ship.require_memory_review",
        domain="ship",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="ship.use_vcs_policy",
        domain="ship",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- review ------------------------------------------------------------
    LeafKey(
        key="review.post_default",
        domain="review",
        type="str",
        default="ask",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="review.template",
        domain="review",
        type="str",
        default="default",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="review.require_checks_before_approve",
        domain="review",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- polish ------------------------------------------------------------
    LeafKey(
        key="polish.auto_apply_safe",
        domain="polish",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="polish.include_memory",
        domain="polish",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="polish.include_agent_memory",
        domain="polish",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="polish.deletion_policy",
        domain="polish",
        type="str",
        default="recoverable_with_reason",
        writable_layers=_WRITABLE_GWR,
    ),
    # --- flow --------------------------------------------------------------
    LeafKey(
        key="flow.auto_accept.research",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.auto_accept.prep",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.auto_accept.audit",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.auto_accept.ship",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.auto_accept.review",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.auto_accept.polish",
        domain="flow",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="flow.ask_on_decisions",
        domain="flow",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- memory ------------------------------------------------------------
    LeafKey(
        key="memory.stores",
        domain="memory",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="memory.review_on_ship",
        domain="memory",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="memory.review_on_polish",
        domain="memory",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="memory.auto_promote",
        domain="memory",
        type="str",
        default="ask",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="memory.prune",
        domain="memory",
        type="str",
        default="ask",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="memory.max_injected_tokens",
        domain="memory",
        type="int",
        default=2000,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- vcs ---------------------------------------------------------------
    LeafKey(
        key="vcs.commit_template",
        domain="vcs",
        type="str",
        default="state_scoped",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.pr_template",
        domain="vcs",
        type="str",
        default="iter",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.branch_pattern",
        domain="vcs",
        type="str",
        default="eawf/{project}/{scope}-{slug}",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.checkpoint_requires_commit",
        domain="vcs",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.protected_branches",
        domain="vcs",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.auto_commit",
        domain="vcs",
        type="str",
        default="ask",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.auto_push", domain="vcs", type="str", default="ask", writable_layers=_WRITABLE_GWR
    ),
    LeafKey(
        key="vcs.pr_open", domain="vcs", type="str", default="ask", writable_layers=_WRITABLE_GWR
    ),
    LeafKey(
        key="vcs.pr_merge_method",
        domain="vcs",
        type="str",
        default="merge",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.squash_allowed",
        domain="vcs",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.delete_branch_after_merge",
        domain="vcs",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.require_ci_green",
        domain="vcs",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.require_review_before_merge",
        domain="vcs",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.force_push",
        domain="vcs",
        type="str",
        default="forbidden_protected",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.mode",
        domain="vcs",
        type="str",
        default="runtime",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.default_runtime",
        domain="vcs",
        type="str",
        default="claude",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.project",
        domain="vcs",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.trailers.claude.name",
        domain="vcs",
        type="str",
        default="Claude",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.trailers.claude.email",
        domain="vcs",
        type="str",
        default="noreply@anthropic.com",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.trailers.codex.name",
        domain="vcs",
        type="str",
        default="Codex",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.trailers.codex.email",
        domain="vcs",
        type="str",
        default="noreply@openai.com",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.coauthor.require_trailer",
        domain="vcs",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- worktrees ---------------------------------------------------------
    LeafKey(
        key="worktrees.enabled",
        domain="worktrees",
        type="literal",
        default="auto",
        writable_layers=_WRITABLE_GWR,
        choices=("auto", "always", "never"),
    ),
    LeafKey(
        key="worktrees.root",
        domain="worktrees",
        type="str",
        default=".worktrees",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.merge_mode",
        domain="worktrees",
        type="str",
        default="cherry_pick",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.use_for_parallel_writers",
        domain="worktrees",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.use_for_risky_changes",
        domain="worktrees",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.use_for_readonly_research",
        domain="worktrees",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.preserve_on_conflict",
        domain="worktrees",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="worktrees.remove_when_clean",
        domain="worktrees",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- acceptance --------------------------------------------------------
    LeafKey(
        key="acceptance.commands.tests",
        domain="acceptance",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="acceptance.commands.lint",
        domain="acceptance",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="acceptance.commands.typecheck",
        domain="acceptance",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="acceptance.commands.build",
        domain="acceptance",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="acceptance.required_before_ship",
        domain="acceptance",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # --- security ----------------------------------------------------------
    LeafKey(
        key="security.secrets_policy",
        domain="security",
        type="str",
        default="env_refs_only",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.env_ref_syntax",
        domain="security",
        type="str",
        default="${ENV:NAME}",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.permission_mode",
        domain="security",
        type="str",
        default="ask_first",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.secret_scan",
        domain="security",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.store_scan_before_checkpoint",
        domain="security",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.store_scan_on_finding",
        domain="security",
        type="str",
        default="block",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="security.allow_destructive",
        domain="security",
        type="str",
        default="ask",
        writable_layers=_WRITABLE_GWR,
    ),
    # --- hooks -------------------------------------------------------------
    LeafKey(
        key="hooks.policy",
        domain="hooks",
        type="str",
        default="mixed_strict",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="hooks.timeout_seconds",
        domain="hooks",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="hooks.enabled",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="hooks.fail_closed",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="hooks.fail_open",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="hooks.ask_on_fail",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # --- mcp ---------------------------------------------------------------
    LeafKey(
        key="mcp.default_policy",
        domain="mcp",
        type="str",
        default="ask_install",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="mcp.manage_only_owner",
        domain="mcp",
        type="str",
        default="eawf",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="mcp.env_ref_syntax",
        domain="mcp",
        type="str",
        default="${ENV:NAME}",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="mcp.servers", domain="mcp", type="mapping", default={}, writable_layers=_WRITABLE_GWR
    ),
    # --- statusline --------------------------------------------------------
    LeafKey(
        key="statusline.modules_default",
        domain="statusline",
        type="str",
        default="ask_per_module",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="statusline.modules_available",
        domain="statusline",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # --- docs --------------------------------------------------------------
    LeafKey(
        key="docs.generated_default_dir",
        domain="docs",
        type="str",
        default=".ea/artifacts/rendered",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="docs.generation_policy",
        domain="docs",
        type="str",
        default="ask_per_category",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="docs.categories",
        domain="docs",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
    ),
    # --- commands ----------------------------------------------------------
    LeafKey(
        key="commands.inventory_policy",
        domain="commands",
        type="str",
        default="full_io_spec_before_code",
        writable_layers=_WRITABLE_GWR,
    ),
    # --- state_schema ------------------------------------------------------
    LeafKey(
        key="state_schema.strictness",
        domain="state_schema",
        type="str",
        default="full_strict_schema_before_code",
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="state_schema.id_padding",
        domain="state_schema",
        type="int",
        default=2,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- daemon ------------------------------------------------------------
    LeafKey(
        key="daemon.proxy_enabled",
        domain="daemon",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
        description="When false, mutations bypass the daemon and use portalocker direct-writes.",
    ),
    LeafKey(
        key="daemon.idle_timeout_seconds",
        domain="daemon",
        type="int",
        default=300,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="daemon.session_handle_ttl_seconds",
        domain="daemon",
        type="int",
        default=86400,
        writable_layers=_WRITABLE_GWR,
    ),
)


# Public read-only mapping. The dict shape is convenient for dotted-key
# lookup; the underlying tuple preserves declaration order for tests
# that want to iterate by section.
LEAF_KEY_REGISTRY: Mapping[str, LeafKey] = {entry.key: entry for entry in _LEAF_KEYS}


# Module-load invariants — diff hygiene gates.
assert len(LEAF_KEY_REGISTRY) == len(_LEAF_KEYS), "LEAF_KEY_REGISTRY: duplicate keys in _LEAF_KEYS"


def leaf_key_lookup(key: str) -> LeafKey:
    """Return the :class:`LeafKey` row for *key* or raise.

    Args:
        key: Dotted config key (e.g. ``"runtime.preference"``).

    Returns:
        The :class:`LeafKey` row.

    Raises:
        ValueError: When *key* is not in :data:`LEAF_KEY_REGISTRY`. The
            error message is the canonical
            ``unknown config key: <key!r>`` form so the daemon RPC can
            propagate it verbatim.
    """
    entry = LEAF_KEY_REGISTRY.get(key)
    if entry is None:
        raise ValueError(f"unknown config key: {key!r}")
    return entry


def is_known_leaf_key(key: str) -> bool:
    """Return ``True`` when *key* resolves to a :data:`LEAF_KEY_REGISTRY` row."""
    return key in LEAF_KEY_REGISTRY


def leaf_keys_by_domain(domain: str) -> tuple[LeafKey, ...]:
    """Return every :class:`LeafKey` whose ``domain`` matches *domain*.

    Args:
        domain: Domain label (e.g. ``"runtime"``, ``"telemetry"``).
            Unknown domains return an empty tuple — callers can audit
            their own coverage without raising.
    """
    return tuple(entry for entry in _LEAF_KEYS if entry.domain == domain)


__all__ = [
    "CONFIG_REGISTRY",
    "LEAF_KEY_REGISTRY",
    "ConfigKey",
    "ConfigKeyType",
    "LeafKey",
    "LeafKeyType",
    "coerce_and_validate",
    "is_known_key",
    "is_known_leaf_key",
    "keys_for_tab",
    "leaf_key_lookup",
    "leaf_keys_by_domain",
    "registry_lookup",
    "tabs_sorted",
]
