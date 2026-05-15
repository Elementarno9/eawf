"""Typed metadata registry for the interactive ``eawf config`` menu.

The registry pairs each operator-tunable config key with structural metadata
the interactive surface needs: the tab grouping, a one-line label, the
value type / choice set, and the dotted key path. Both the questionary
menu in :mod:`eawf.cli.commands.config` (P20-W10) and the future TUI config
hotkey (P20-W11) consume this single registry so the two surfaces cannot
drift on key set, label, default, or validation rule.

Public API:

- :class:`ConfigKeyType` — narrow Literal of supported value shapes.
- :class:`ConfigKey` — frozen Pydantic record describing one tunable key.
- :data:`CONFIG_REGISTRY` — the canonical ordered tuple of registry entries.
- :func:`tabs_sorted` — alphabetical tab names extracted from the registry.
- :func:`keys_for_tab` — alphabetical :class:`ConfigKey` list for one tab.
- :func:`registry_lookup` — locate an entry by dotted key.
- :func:`coerce_and_validate` — turn a raw string answer into a typed value
  matching the entry's declared type, raising :class:`InvalidInput` on
  failure.

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


__all__ = [
    "CONFIG_REGISTRY",
    "ConfigKey",
    "ConfigKeyType",
    "coerce_and_validate",
    "is_known_key",
    "keys_for_tab",
    "registry_lookup",
    "tabs_sorted",
]
