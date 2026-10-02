"""C08 leaf-key catalog data table + lookup accessors (P25-W14 / C08 §5.2).

This module hosts :data:`LEAF_KEY_REGISTRY` — the catalog of every leaf in
the layered config that some code reads. Each entry is a
:class:`~eawf.kernel.config.registry.leaf_keys.LeafKey` record naming its declared
type, default, and the list of layers that may write it. The catalog is
what the daemon uses to reject ``unknown config key: <key!r>`` writes; the
``eawf config`` menu never iterates the full set.

The :class:`LeafKey` model and the ``_WRITABLE_*`` layer shorthands live in
the sibling :mod:`eawf.kernel.config.registry.leaf_keys` module; this module owns
only the large declaration-ordered data table and its accessors so neither
file grows past the EAWF010 line-of-code alarm.

Public API:

- :data:`LEAF_KEY_REGISTRY` — read-only mapping ``dotted_key → LeafKey``.
- :func:`leaf_key_lookup` — strict lookup; raises ``ValueError`` on unknown
  keys with the canonical ``unknown config key: <key!r>`` message.
- :func:`is_known_leaf_key` — ``True`` when the dotted path resolves to a
  :data:`LEAF_KEY_REGISTRY` entry.
- :func:`leaf_keys_by_domain` — every :class:`LeafKey` under one domain.
- :data:`DEPRECATED_LEAF_KEYS` — keys no code reads any more, which the config
  migration strips and doctor reports.
"""

from __future__ import annotations

from collections.abc import Mapping

from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY
from eawf.kernel.config.registry.leaf_keys import (
    _WRITABLE_ALL_DURABLE,
    _WRITABLE_GWR,
    _WRITABLE_NONE,
    _WRITABLE_RUNTIME_PREFERENCE,
    LeafKey,
)
from eawf.kernel.config.schema import (
    ALL_ROLES,
    DEFAULT_PERMISSION_WAIT_SECONDS,
    DEFAULT_STALL_INTERVAL_SECONDS,
    RUNTIME_ADAPTER_IDS,
)
from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH, RESEARCH_DEPTH_VALUES
from eawf.kernel.state.enums import AgentSessionRole

# Catalog data table — declaration-ordered by domain section for review.
_DECLARED_LEAF_KEYS: tuple[LeafKey, ...] = (
    # --- top-level + schema ------------------------------------------------
    LeafKey(
        key="schema_version",
        domain="config",
        type="literal",
        default="1.0",
        writable_layers=_WRITABLE_NONE,
        description="Marker for the on-disk layered-config schema shape.",
        choices=("1.0",),
        consumer="eawf.kernel.config.migration.migrate_config_payload",
    ),
    # --- profiles ----------------------------------------------------------
    LeafKey(
        key="profiles.enabled",
        domain="profiles",
        type="list_str",
        default=("core",),
        writable_layers=_WRITABLE_ALL_DURABLE,
        editor="check",
        choices_from="profiles",
    ),
    LeafKey(
        key="profiles.trusted",
        domain="profiles",
        type="mapping",
        default={},
        writable_layers=("repo", "branch"),
        description="Profile id → sha256 of last-trusted body.",
        editor="pin",
        choices_from="profile_trust",
    ),
    LeafKey(
        key="profiles.certified",
        domain="profiles",
        type="mapping",
        default={},
        writable_layers=("repo", "branch"),
        description="Profile id → digest an enriched profile is certified as managed under.",
        editor="pin",
        choices_from="profile_certification",
    ),
    # --- runtime -----------------------------------------------------------
    LeafKey(
        key="runtime.adapters",
        domain="runtime",
        type="list_str",
        default=("claude-code",),
        writable_layers=_WRITABLE_GWR,
        description="Legacy v1.1 selector; superseded by runtime.preference.",
        editor="check",
        choices=RUNTIME_ADAPTER_IDS,
    ),
    LeafKey(
        key="runtime.preference",
        domain="runtime",
        type="list_str",
        default=("claude-code",),
        writable_layers=_WRITABLE_RUNTIME_PREFERENCE,
        description="C08-canonical fallback ladder; first entry is primary.",
        editor="order",
        choices=RUNTIME_ADAPTER_IDS,
    ),
    LeafKey(
        key="runtime.models.claude",
        domain="runtime",
        type="list_str",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="Optional cheap/mid/top model ladder for the Claude runtime.",
        runtime="claude",
        editor="tiers",
    ),
    LeafKey(
        key="runtime.models.codex",
        domain="runtime",
        type="list_str",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="Optional cheap/mid/top model ladder for the Codex runtime.",
        runtime="codex",
        editor="tiers",
    ),
    LeafKey(
        key="runtime.models.opencode",
        domain="runtime",
        type="list_str",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="Optional cheap/mid/top model ladder for the opencode runtime.",
        runtime="opencode",
        editor="tiers",
    ),
    LeafKey(
        key="runtime.claude.stall_interval_s",
        domain="runtime",
        type="int",
        default=DEFAULT_STALL_INTERVAL_SECONDS,
        writable_layers=_WRITABLE_GWR,
        description="Seconds a Claude Run may produce nothing before it is flagged stalled.",
        runtime="claude",
    ),
    LeafKey(
        key="runtime.claude.permission_wait_s",
        domain="runtime",
        type="int",
        default=DEFAULT_PERMISSION_WAIT_SECONDS,
        writable_layers=_WRITABLE_GWR,
        description="Seconds Claude's permission hook waits for a principal's decision.",
    ),
    LeafKey(
        key="runtime.codex.stall_interval_s",
        domain="runtime",
        type="int",
        default=DEFAULT_STALL_INTERVAL_SECONDS,
        writable_layers=_WRITABLE_GWR,
        description="Seconds a Codex Run may produce nothing before it is flagged stalled.",
        runtime="codex",
    ),
    LeafKey(
        key="runtime.opencode.stall_interval_s",
        domain="runtime",
        type="int",
        default=DEFAULT_STALL_INTERVAL_SECONDS,
        writable_layers=_WRITABLE_GWR,
        description="Seconds an opencode Run may produce nothing before it is flagged stalled.",
        runtime="opencode",
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
        key="ui.toasts",
        domain="ui",
        type="literal",
        default="important",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("off", "important", "all"),
        description=(
            "Toasts for state changes nobody keyed: off raises none; important and all raise "
            "them. Answers to a key and the quit prompt always show."
        ),
        consumer="eawf.surfaces.tui.launch.persisted_toast_verbosity",
    ),
    # --- tui ---------------------------------------------------------------
    LeafKey(
        key="tui.eu_view.density",
        domain="tui",
        type="literal",
        default="full",
        writable_layers=_WRITABLE_GWR,
        choices=("full", "compact"),
        description="Roadmap EU/hour rollup table: full adds a detail column; compact omits it.",
        consumer="eawf.surfaces.render.plan_view._eu_view_config",
    ),
    LeafKey(
        key="tui.eu_view.fields",
        domain="tui",
        type="list_str",
        default=("work_sum", "critical_path", "queue", "realistic"),
        writable_layers=_WRITABLE_GWR,
        choices=("work_sum", "critical_path", "queue", "realistic"),
        description="Roadmap EU/hour rollup metrics, in row order; at least one.",
        consumer="eawf.surfaces.render.plan_view._eu_view_config",
    ),
    # --- telemetry (C09 surface) ------------------------------------------
    LeafKey(
        key="telemetry.enabled",
        domain="telemetry",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="telemetry.db_kind",
        domain="telemetry",
        type="literal",
        default="sqlite",
        writable_layers=("global",),
        choices=("duckdb", "sqlite"),
    ),
    # --- dispatch ----------------------------------------------------------
    LeafKey(
        key="dispatch.role_tier_token_cap",
        domain="dispatch",
        type="int",
        default=2400,
        writable_layers=_WRITABLE_GWR,
        description="Token ceiling per injected role-tier dispatch block (raise, never truncate).",
    ),
    # --- agents ------------------------------------------------------------
    LeafKey(
        key="agents.extra_tools",
        domain="agents",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
        description=(
            "Per-role extra tool names appended to each rendered subagent's "
            "allowlist; the '*' key grants to every role. Empty map renders "
            "the built-in allowlists unchanged."
        ),
        consumer="eawf.kernel.config.layered.resolve_agent_extra_tools",
        editor="rows",
        choices=(ALL_ROLES, *(role.value for role in AgentSessionRole)),
    ),
    # --- economics ---------------------------------------------------------
    # Each leaf is one whole policy record; unset keeps the shipped policy, which
    # the economics module owns rather than the built-in layer.
    LeafKey(
        key="economics.prompt_budget",
        domain="economics",
        type="mapping",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="Prompt budget: the input window, reserved output and per-class ceilings.",
        consumer="eawf.kernel.economics.governor.economics_policy_from",
    ),
    LeafKey(
        key="economics.governor",
        domain="economics",
        type="mapping",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="In-flight governor: concurrent Runs, in-flight tokens and cost, admission.",
        consumer="eawf.kernel.economics.governor.economics_policy_from",
    ),
    LeafKey(
        key="economics.notice_policy",
        domain="economics",
        type="mapping",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="When a Run's budget notice fires, as a fraction of its estimate.",
        consumer="eawf.kernel.economics.governor.economics_policy_from",
    ),
    # --- research ----------------------------------------------------------
    LeafKey(
        key="research.default_depth",
        domain="research",
        type="literal",
        default=DEFAULT_RESEARCH_DEPTH.value,
        writable_layers=_WRITABLE_GWR,
        choices=RESEARCH_DEPTH_VALUES,
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
        key="planning.max_parallel_waves",
        domain="planning",
        type="int",
        default=4,
        writable_layers=_WRITABLE_GWR,
        description="Repo-wide hard cap for CLAIMED and IN_PROGRESS waves.",
        consumer="eawf.workflow.lifecycle._capacity.resolve_max_parallel_waves",
    ),
    # --- verify --------------------------------------------------------------
    LeafKey(
        key="verify.odr_blocking",
        domain="verify",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
        description="Repo opt-in: a below-floor Oracle-Determinism-Ratio refuses iter close.",
        consumer="eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
    ),
    LeafKey(
        key="verify.require_iter_audit_accepted",
        domain="verify",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
        description="Require a completed accepted audit before iter close.",
    ),
    LeafKey(
        key="verify.waiver_mode",
        domain="verify",
        type="literal",
        default="B",
        writable_layers=_WRITABLE_GWR,
        description="Operator waiver policy; disabled is an absorbing composition value.",
        choices=("A", "B", "C", "disabled"),
    ),
    LeafKey(
        key="verify.retyped_rule_threshold",
        domain="verify",
        type="int",
        default=3,
        writable_layers=_WRITABLE_GWR,
        description="Re-typings of one rule within a release above which it must be triaged.",
        consumer="eawf.observability.reflect.retyped.resolve_retyped_rule_threshold",
    ),
    LeafKey(
        key="verify.jury_max_brier",
        domain="verify",
        type="float",
        default=0.25,
        writable_layers=_WRITABLE_GWR,
        description="Highest jury Brier score that still earns blocking verdicts.",
        consumer="eawf.runtime.daemon.verdict_observations.resolve_jury_thresholds",
    ),
    LeafKey(
        key="verify.jury_max_co_error",
        domain="verify",
        type="float",
        default=0.10,
        writable_layers=_WRITABLE_GWR,
        description="Highest share of known-bad subjects a jury may pass unanimously.",
        consumer="eawf.runtime.daemon.verdict_observations.resolve_jury_thresholds",
    ),
    LeafKey(
        key="verify.juror_wall_clock_seconds",
        domain="verify",
        type="float",
        default=600.0,
        writable_layers=_WRITABLE_GWR,
        description="Wall-clock ceiling for close-time auditor and juror runs.",
        consumer="eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
    ),
    # --- preferences -------------------------------------------------------
    LeafKey(
        key="preferences.auto_choose",
        domain="preferences",
        type="literal",
        default="off",
        writable_layers=_WRITABLE_GWR,
        description="Whether AskUserQuestion auto-picks the recommended option.",
        choices=("off", "recommended", "always"),
    ),
    # --- estimation --------------------------------------------------------
    LeafKey(
        key="estimation.eu_minutes",
        domain="estimation",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.eu_basis",
        domain="estimation",
        type="literal",
        default="api_duration",
        writable_layers=_WRITABLE_GWR,
        description="Captured quantity used to convert runtime counters into elapsed EU.",
        choices=("api_duration", "tokens", "wall_clock"),
    ),
    # --- audit -------------------------------------------------------------
    LeafKey(
        key="audit.fix_safe",
        domain="audit",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
        description="Reserved for compatibility; /audit never mutates source automatically.",
    ),
    # --- ship --------------------------------------------------------------
    LeafKey(
        key="ship.require_audit_pass",
        domain="ship",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
        description="Reserved for compatibility; this value does not gate ship.",
    ),
    LeafKey(
        key="ship.require_memory_review",
        domain="ship",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    # --- flow --------------------------------------------------------------
    LeafKey(
        key="flow.budget.enforce",
        domain="flow",
        type="literal",
        default="soft",
        writable_layers=_WRITABLE_GWR,
        description=(
            "Token-budget enforcement mode. soft (default) warns and lets "
            "the wave continue past its cap; hard halts the wave at the cap "
            "via the SIGTERM->SIGKILL ladder."
        ),
        choices=("soft", "hard"),
    ),
    LeafKey(
        key="flow.budget.multiplier",
        domain="flow",
        type="float",
        default=1.5,
        writable_layers=_WRITABLE_GWR,
        description=(
            "Safety multiplier applied to a wave's base budget to derive the "
            "enforced cap (1.5 == 50% headroom)."
        ),
    ),
    # --- vcs ---------------------------------------------------------------
    LeafKey(
        key="vcs.checkpoint_requires_commit",
        domain="vcs",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="vcs.integration_commit_unit",
        domain="vcs",
        type="literal",
        default="batch",
        writable_layers=_WRITABLE_GWR,
        choices=("batch", "task"),
        description=(
            "Unit of one verified delivery commit: 'batch' squashes a Batch's sealed "
            "candidates at integration; 'task' keeps one delivery commit per Task."
        ),
    ),
    LeafKey(
        key="vcs.task_reference",
        domain="vcs",
        type="literal",
        default="trailer",
        writable_layers=_WRITABLE_GWR,
        choices=("trailer", "subject", "none"),
        description=(
            "How a delivery commit names its Task: a 'Task:' trailer, an in-subject "
            "identifier, or nothing beyond the provenance manifest."
        ),
    ),
    LeafKey(
        key="vcs.conventions.subject_style",
        domain="vcs",
        type="literal",
        default="trailer",
        writable_layers=_WRITABLE_GWR,
        choices=("bracket", "trailer"),
        description=(
            "Written commit-subject form. 'trailer' writes '<type>: <summary>' plus an "
            "'Eawf-Wave: P##-I##-W##' trailer; 'bracket' writes the deprecated "
            "'[P##-I##-W##] <type>:' prefix."
        ),
    ),
    LeafKey(
        key="vcs.conventions.release.cadence",
        domain="vcs",
        type="literal",
        default="manual",
        writable_layers=_WRITABLE_GWR,
        choices=("manual", "per-phase"),
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
        key="vcs.coauthor.project.name",
        domain="vcs",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="The co-author name a project-mode trailer names.",
        editor="pair",
    ),
    LeafKey(
        key="vcs.coauthor.project.email",
        domain="vcs",
        type="str",
        default=None,
        writable_layers=_WRITABLE_GWR,
        description="The co-author email a project-mode trailer names.",
        editor="pair",
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


# The callable that reads each leaf's resolved value, for leaves not naming it inline.
_CONSUMER_BY_KEY: dict[str, str] = {
    "daemon.idle_timeout_seconds": "eawf.runtime.daemon.main._resolve_idle_timeout",
    "daemon.proxy_enabled": "eawf.surfaces.cli._mutation._proxy_enabled",
    "daemon.session_handle_ttl_seconds": "eawf.runtime.daemon.main._resolve_session_ttl_seconds",
    "dispatch.role_tier_token_cap": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "estimation.eu_basis": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "estimation.eu_minutes": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "flow.budget.enforce": "eawf.runtime.daemon.methods.agent._resolve_budget_config",
    "flow.budget.multiplier": "eawf.runtime.daemon.methods.agent._resolve_budget_config",
    "planning.max_parallel_waves": "eawf.workflow.lifecycle._capacity.resolve_max_parallel_waves",
    "preferences.auto_choose": "eawf.runtime.daemon.methods.question_decision.resolved_preferences",
    "profiles.certified": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "profiles.enabled": "eawf.platform.profiles.selection.resolve_enabled_profiles",
    "profiles.trusted": "eawf.platform.profiles.trust.load_trust_ledger",
    "research.agent_count": "eawf.workflow.skills.research.ResearchSkill._resolve_agents",
    "research.default_depth": "eawf.workflow.skills.research.ResearchSkill._resolve_depth",
    "runtime.adapters": "eawf.kernel.config.layered.resolve_dispatch_provider_tuple",
    "runtime.claude.permission_wait_s": (
        "eawf.kernel.config.layered.resolve_permission_wait_seconds"
    ),
    "runtime.claude.stall_interval_s": "eawf.kernel.config.layered.resolve_stall_interval_seconds",
    "runtime.codex.stall_interval_s": "eawf.kernel.config.layered.resolve_stall_interval_seconds",
    "runtime.models.claude": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "runtime.models.codex": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "runtime.models.opencode": "eawf.kernel.config.layered.resolve_runtime_tier_models",
    "runtime.opencode.stall_interval_s": (
        "eawf.kernel.config.layered.resolve_stall_interval_seconds"
    ),
    "runtime.preference": "eawf.kernel.config.layered.resolve_dispatch_provider_tuple",
    "telemetry.db_kind": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "telemetry.enabled": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "ui.glyphs": "eawf.surfaces.tui.launch.persisted_glyphs",
    "ui.theme": "eawf.surfaces.tui.chassis.theme.persisted_theme",
    "vcs.checkpoint_requires_commit": "eawf.runtime.vcs.checkpoint.resolve_checkpoint_cadence",
    "vcs.coauthor.default_runtime": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.mode": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.project.email": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.project.name": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.require_trailer": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.trailers.claude.email": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.trailers.claude.name": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.trailers.codex.email": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.coauthor.trailers.codex.name": "eawf.runtime.vcs.coauthor.resolve_coauthor_trailer",
    "vcs.conventions.release.cadence": "eawf.runtime.vcs.coauthor.requires_phase_release_preflight",
    "vcs.conventions.subject_style": "tools.commit_prefix_lint._configured_subject_style",
    "vcs.integration_commit_unit": "eawf.runtime.daemon.methods.delivery.integrate_delivery",
    "vcs.task_reference": "eawf.runtime.daemon.methods.delivery.integrate_delivery",
    "verify.require_iter_audit_accepted": "eawf.workflow.lifecycle.iter_.close_iter",
    "verify.waiver_mode": "eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
}

_DEPRECATED_KEYS: frozenset[str] = frozenset(
    {
        "audit.fix_safe",
        "flow.auto_accept.audit",
        "flow.auto_accept.polish",
        "flow.auto_accept.prep",
        "flow.auto_accept.research",
        "flow.auto_accept.review",
        "flow.auto_accept.ship",
        "polish.auto_apply_safe",
        "polish.deletion_policy",
        "runtime.adapter_catalog.claude.agents_path",
        "runtime.adapter_catalog.claude.enabled",
        "runtime.adapter_catalog.claude.plugin_install",
        "runtime.adapter_catalog.claude.skills_path",
        "runtime.adapter_catalog.codex.enabled",
        "runtime.adapter_catalog.codex.status",
        "runtime.adapter_catalog.opencode.enabled",
        "runtime.adapter_catalog.opencode.status",
        "ship.require_audit_pass",
        "ship.require_memory_review",
        "vcs.auto_push",
        "vcs.pr_open",
    }
)
# Leaves removed from the catalog because no code read their value. The config
# migration strips them from layer files and doctor names any a layer still sets.
_RETIRED_KEYS: frozenset[str] = frozenset(
    {
        # read only by a retired skill: the ship gauntlet and its acceptance commands,
        # the audit and review levels, the flow transitions, the prep resume and the
        # pull-request merge policy
        "acceptance.commands.build",
        "acceptance.commands.lint",
        "acceptance.commands.tests",
        "acceptance.commands.typecheck",
        "acceptance.required_before_ship",
        "audit.default_level",
        "flow.advance_after.audit",
        "flow.advance_after.polish",
        "flow.advance_after.prep",
        "flow.advance_after.research",
        "flow.max_repair_cycles",
        "prep.auto_resume",
        "review.default_level",
        "ship.gauntlet",
        "vcs.pr_merge_method",
        "vcs.squash_allowed",
        # named /research as its reader, but no code ever read it
        "research.auto_save",
        # retired before this catalog tracked them: renamed or never consumed
        "estimation.buckets",
        "mcp.enabled",
        "preferences.scope_size",
        "preferences.solution_bias",
        "project.default_subproject",
        "telemetry.export.endpoint",
        "audit.default_checks",
        "audit.flaky_retry_count",
        "cli.canonical_command",
        "cli.install_aliases",
        "cli.omit_ea_alias",
        "cli.preferred_command",
        "commands.inventory_policy",
        "config.layers_visible",
        "dispatch.routing",
        "dispatch.session_handle_ttl_seconds",
        "dispatch.session_policy_default",
        "docs.categories",
        "docs.generated_default_dir",
        "docs.generation_policy",
        "estimation.calibration_profile",
        "estimation.display.eu_quantum",
        "estimation.display.show_category",
        "estimation.display.show_expected_time",
        "estimation.display.show_pessimistic_time",
        "estimation.display.show_raw_eu",
        "estimation.display.time_quantum_over_2h_minutes",
        "estimation.display.time_quantum_under_2h_minutes",
        "estimation.enabled",
        "estimation.idle_policy",
        "estimation.realtime_recalibration",
        "hooks.ask_on_fail",
        "hooks.enabled",
        "hooks.fail_closed",
        "hooks.fail_open",
        "hooks.policy",
        "hooks.timeout_seconds",
        "language.fast_extras",
        "language.runtime",
        "mcp.default_policy",
        "mcp.env_ref_syntax",
        "mcp.manage_only_owner",
        "mcp.servers",
        "memory.auto_promote",
        "memory.max_injected_tokens",
        "memory.prune",
        "memory.review_on_polish",
        "memory.review_on_ship",
        "memory.stores",
        "planning.approval",
        "planning.auto_plan",
        "planning.require_research_for_unknowns",
        "polish.include_agent_memory",
        "polish.include_memory",
        "profiles.catalog",
        "profiles.conflict_resolution",
        "profiles.safety_policy",
        "project.code",
        "project.default_track",
        "project.domains",
        "project.goals",
        "project.slug",
        "project.success_metrics",
        "project.title",
        "prose.block_on_lint",
        "prose.clarity_judge",
        "prose.level",
        "research.default_sources",
        "research.folder",
        "review.post_default",
        "review.require_checks_before_approve",
        "review.template",
        "runtime.default",
        "runtime.fallback.max_backoff_seconds",
        "runtime.fallback.on_errors",
        "runtime.fallback.retry_policy",
        "runtime.slash_commands",
        "security.allow_destructive",
        "security.env_ref_syntax",
        "security.permission_mode",
        "security.secret_scan",
        "security.secrets_policy",
        "security.store_scan_before_checkpoint",
        "security.store_scan_on_finding",
        "ship.use_vcs_policy",
        "state_schema.id_padding",
        "state_schema.strictness",
        "statusline.color_mode",
        "statusline.glyph_mode",
        "statusline.modules_available",
        "statusline.modules_default",
        "statusline.rows",
        "storage.artifacts_dir",
        "storage.commit_jsonl",
        "storage.content_addressed_blobs",
        "storage.generated_index",
        "storage.lock_strategy",
        "storage.max_inline_chars",
        "storage.rendered_dir",
        "storage.state_path",
        "storage.stores_dir",
        "telemetry.aggregate_window",
        "telemetry.export.format",
        "telemetry.window_default",
        "ui.bare_command",
        "ui.color",
        "ui.dashboard_panes",
        "ui.refresh_ms",
        "ui.tour_completed",
        "vcs.auto_commit",
        "vcs.branch_pattern",
        "vcs.commit_template",
        "vcs.conventions.release.agent_driven",
        "vcs.delete_branch_after_merge",
        "vcs.force_push",
        "vcs.pr_template",
        "vcs.protected_branches",
        "vcs.require_ci_green",
        "vcs.require_review_before_merge",
        "workspace.code",
        "workspace.enabled",
        "workspace.repos",
        "workspace.state_path",
        "worktrees.enabled",
        "worktrees.merge_mode",
        "worktrees.preserve_on_conflict",
        "worktrees.remove_when_clean",
        "worktrees.root",
        "worktrees.use_for_parallel_writers",
        "worktrees.use_for_readonly_research",
        "worktrees.use_for_risky_changes",
    }
)
DEPRECATED_LEAF_KEYS: frozenset[str] = _DEPRECATED_KEYS | _RETIRED_KEYS


def _bind_behavior_metadata(entry: LeafKey) -> LeafKey:
    """Attach the audited consumer-or-deprecated classification to *entry*."""
    if entry.key in _DEPRECATED_KEYS:
        return entry.model_copy(
            update={"consumer": None, "consumer_kind": "deprecated", "reserved": True}
        )
    consumer = entry.consumer or _CONSUMER_BY_KEY[entry.key]
    consumer_kind = "skill" if consumer.startswith("eawf.workflow.skills.") else "engine"
    return entry.model_copy(
        update={"consumer": consumer, "consumer_kind": consumer_kind, "reserved": False}
    )


_RANGE_BY_KEY: dict[str, tuple[float | None, float | None]] = {
    entry.key: (entry.min_value, entry.max_value)
    for entry in CONFIG_REGISTRY
    if entry.min_value is not None or entry.max_value is not None
}


def _bind_value_range(entry: LeafKey) -> LeafKey:
    """Attach the range the interactive config registry holds *entry*'s value to."""
    bounds = _RANGE_BY_KEY.get(entry.key)
    return entry if bounds is None else entry.model_copy(update={"value_range": bounds})


_LEAF_KEYS: tuple[LeafKey, ...] = tuple(
    _bind_value_range(_bind_behavior_metadata(entry)) for entry in _DECLARED_LEAF_KEYS
)

_DECLARED_KEYS = {entry.key for entry in _LEAF_KEYS}
assert not (_CONSUMER_BY_KEY.keys() - _DECLARED_KEYS), (
    f"consumer binding targets undeclared leaf: {sorted(_CONSUMER_BY_KEY.keys() - _DECLARED_KEYS)}"
)
assert not (_RETIRED_KEYS & _DECLARED_KEYS), (
    f"retired leaf still declared: {sorted(_RETIRED_KEYS & _DECLARED_KEYS)}"
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


def pair_siblings(key: str) -> tuple[str, ...]:
    """Return the leaves written together with *key*, in catalog order.

    A ``pair`` leaf is only valid beside the other ``pair`` leaves of its block, such
    as a co-author's name and email, so it is never written alone.

    Args:
        key: Dotted config key.

    Returns:
        The other ``pair`` leaves under the same block; empty for any other key.
    """
    entry = LEAF_KEY_REGISTRY.get(key)
    if entry is None or entry.editor != "pair":
        return ()
    block = key.rpartition(".")[0]
    return tuple(
        other.key
        for other in _LEAF_KEYS
        if other.editor == "pair" and other.key != key and other.key.rpartition(".")[0] == block
    )


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
