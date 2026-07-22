"""C08 leaf-key catalog data table + lookup accessors (P25-W14 / C08 §5.2).

This module hosts :data:`LEAF_KEY_REGISTRY` — the full ~150-key catalog
covering every leaf in the layered config. Each entry is a
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
"""

from __future__ import annotations

from collections.abc import Mapping

from eawf.kernel.config.registry.config_keys import CONFIG_REGISTRY
from eawf.kernel.config.registry.leaf_keys import (
    _WRITABLE_ALL_DURABLE,
    _WRITABLE_GWR,
    _WRITABLE_NONE,
    _WRITABLE_PROJECT_GOALS,
    _WRITABLE_REPO_ONLY,
    _WRITABLE_RUNTIME_PREFERENCE,
    LeafKey,
)
from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH, RESEARCH_DEPTH_VALUES

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
        key="project.default_track",
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
    LeafKey(
        key="ui.toasts",
        domain="ui",
        type="literal",
        default="important",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("off", "important", "all"),
        description="Ambient state-change toast verbosity: off / important / all.",
    ),
    LeafKey(
        key="ui.tour_completed",
        domain="ui",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
        description="True once the first-run onboarding tour has been dismissed.",
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
        key="telemetry.export.format",
        domain="telemetry",
        type="literal",
        default="prom",
        writable_layers=("global",),
        choices=("prom", "json", "csv"),
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
        default="sqlite",
        writable_layers=("global",),
        choices=("duckdb", "sqlite"),
    ),
    # --- dispatch ----------------------------------------------------------
    LeafKey(
        key="dispatch.session_policy_default",
        domain="dispatch",
        type="literal",
        default="fresh",
        writable_layers=_WRITABLE_GWR,
        # ``continue`` / ``hybrid`` require session resume, which no
        # runtime adapter implements yet (deferred to P31); only
        # ``fresh`` runs today, so the choice set is narrowed to it.
        choices=("fresh",),
    ),
    LeafKey(
        key="dispatch.session_handle_ttl_seconds",
        domain="dispatch",
        type="int",
        default=86400,
        writable_layers=("global",),
    ),
    LeafKey(
        key="dispatch.role_tier_token_cap",
        domain="dispatch",
        type="int",
        default=2400,
        writable_layers=_WRITABLE_GWR,
        description="Token ceiling per injected role-tier dispatch block (raise, never truncate).",
    ),
    LeafKey(
        key="dispatch.routing",
        domain="dispatch",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
        description=(
            "Per (agent_role, effort_bucket) model/runtime overrides; "
            "empty map uses the built-in DEFAULT_ROUTING_TABLE."
        ),
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
        default=DEFAULT_RESEARCH_DEPTH.value,
        writable_layers=_WRITABLE_GWR,
        choices=RESEARCH_DEPTH_VALUES,
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
        description="Reserved for compatibility; roadmap approval remains explicit.",
        choices=("ask", "auto", "never"),
    ),
    LeafKey(
        key="planning.max_parallel_waves",
        domain="planning",
        type="int",
        default=4,
        writable_layers=_WRITABLE_GWR,
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
        key="verify.juror_wall_clock_seconds",
        domain="verify",
        type="float",
        default=600.0,
        writable_layers=_WRITABLE_GWR,
        description="Wall-clock ceiling for close-time auditor and juror runs.",
        consumer="eawf.workflow.verify.readiness._overlay_repo_verify_leaves",
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
        description="Reserved for compatibility; /prep always presents the plan gate.",
    ),
    # --- preferences -------------------------------------------------------
    LeafKey(
        key="preferences.solution_bias",
        domain="preferences",
        type="literal",
        default="balanced",
        writable_layers=_WRITABLE_GWR,
        description="Planner bias toward solution complexity.",
        choices=("simple", "balanced", "thorough"),
    ),
    LeafKey(
        key="preferences.scope_size",
        domain="preferences",
        type="literal",
        default="M",
        writable_layers=_WRITABLE_GWR,
        description="Preferred default effort bucket for planned waves.",
        choices=("XS", "S", "M", "L", "XL"),
    ),
    LeafKey(
        key="preferences.auto_choose",
        domain="preferences",
        type="literal",
        default="off",
        writable_layers=_WRITABLE_GWR,
        description="Whether AskUserQuestion auto-picks the recommended option.",
        choices=("off", "recommended", "always"),
    ),
    # --- prose (doc-clarity) -----------------------------------------------
    # A durable local layer may write these (the authority guard enforces
    # tighten-only against the profile baseline at validation time, not the
    # writability gate). ``clarity_judge`` / ``block_on_lint`` are tri-state
    # (true / false / null-defers-to-level) so they use the ``any`` shape.
    LeafKey(
        key="prose.level",
        domain="prose",
        type="literal",
        default="standard",
        writable_layers=_WRITABLE_ALL_DURABLE,
        description="Doc-clarity prose-lint strictness floor (local may only tighten).",
        choices=("loose", "standard", "strict"),
    ),
    LeafKey(
        key="prose.clarity_judge",
        domain="prose",
        type="any",
        default=None,
        writable_layers=_WRITABLE_ALL_DURABLE,
        description="Run the LLM clarity-judge gate; null defers to prose.level.",
    ),
    LeafKey(
        key="prose.block_on_lint",
        domain="prose",
        type="any",
        default=None,
        writable_layers=_WRITABLE_ALL_DURABLE,
        description="Block on deterministic prose lints; null defers to prose.level.",
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
        key="estimation.eu_basis",
        domain="estimation",
        type="literal",
        default="api_duration",
        writable_layers=_WRITABLE_GWR,
        description="Captured quantity used to convert runtime counters into elapsed EU.",
        choices=("api_duration", "tokens", "wall_clock"),
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
    LeafKey(
        key="estimation.buckets.n_min",
        domain="estimation",
        type="int",
        default=5,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.high_confidence_n",
        domain="estimation",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.overrides.XS",
        domain="estimation",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.overrides.S",
        domain="estimation",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.overrides.M",
        domain="estimation",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.overrides.L",
        domain="estimation",
        type="mapping",
        default={},
        writable_layers=_WRITABLE_GWR,
    ),
    LeafKey(
        key="estimation.buckets.overrides.XL",
        domain="estimation",
        type="mapping",
        default={},
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
        key="audit.default_level",
        domain="audit",
        type="literal",
        default="standard",
        writable_layers=_WRITABLE_GWR,
        description="Default /audit check-plan breadth (quick narrows, deep widens).",
        choices=("quick", "standard", "deep"),
    ),
    LeafKey(
        key="audit.fix_safe",
        domain="audit",
        type="bool",
        default=False,
        writable_layers=_WRITABLE_GWR,
        description="Reserved for compatibility; /audit never mutates source automatically.",
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
        key="ship.gauntlet",
        domain="ship",
        type="literal",
        default="full",
        writable_layers=_WRITABLE_GWR,
        description="Ship gauntlet breadth: full (default) runs all gates; scoped is re-run only.",
        choices=("full", "scoped"),
    ),
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
    LeafKey(
        key="flow.max_repair_cycles",
        domain="flow",
        type="int",
        default=3,
        writable_layers=_WRITABLE_GWR,
        description="Reserved for compatibility; this value does not bound repair execution.",
    ),
    # --- prep --------------------------------------------------------------
    LeafKey(
        key="prep.auto_resume",
        domain="prep",
        type="bool",
        default=True,
        writable_layers=_WRITABLE_GWR,
        description="When True, /prep leads its claim actions with an `eawf dispatch resume`.",
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
        key="vcs.conventions.release.agent_driven",
        domain="vcs",
        type="literal",
        default="per-phase",
        writable_layers=_WRITABLE_GWR,
        choices=("manual", "per-phase"),
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
        description="Reserved hook policy; accepted for compatibility but not enforced.",
    ),
    LeafKey(
        key="hooks.timeout_seconds",
        domain="hooks",
        type="int",
        default=30,
        writable_layers=_WRITABLE_GWR,
        description="Reserved hook policy; accepted for compatibility but not enforced.",
    ),
    LeafKey(
        key="hooks.enabled",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
        description="Reserved hook policy; accepted for compatibility but not enforced.",
    ),
    LeafKey(
        key="hooks.fail_closed",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
        description="Reserved hook policy; accepted for compatibility but not enforced.",
    ),
    LeafKey(
        key="hooks.fail_open",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
        description="Reserved hook policy; accepted for compatibility but not enforced.",
    ),
    LeafKey(
        key="hooks.ask_on_fail",
        domain="hooks",
        type="list_str",
        default=(),
        writable_layers=_WRITABLE_GWR,
        description="Reserved hook policy; accepted for compatibility but not enforced.",
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
    LeafKey(
        key="statusline.glyph_mode",
        domain="statusline",
        type="literal",
        default="auto",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("auto", "ascii", "unicode"),
        description="Statusline glyph set: auto (downgrade on a no-color term) / ascii / unicode.",
    ),
    LeafKey(
        key="statusline.color_mode",
        domain="statusline",
        type="literal",
        default="auto",
        writable_layers=("global", "workspace", "repo", "env"),
        choices=("auto", "always", "never"),
        description="Statusline ANSI colour: auto (off on a no-color term) / always / never.",
    ),
    LeafKey(
        key="statusline.rows",
        domain="statusline",
        type="int",
        default=1,
        writable_layers=("global", "workspace", "repo", "env"),
        description="Number of statusline rows the renderer emits (1..3).",
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


_CONSUMER_BY_KEY: dict[str, str] = {
    "audit.default_level": "eawf.workflow.skills.audit._resolve_level",
    "daemon.proxy_enabled": "eawf.surfaces.cli._mutation._proxy_enabled",
    "dispatch.role_tier_token_cap": "eawf.workflow.dispatch.renderer.resolve_role_blocks",
    "estimation.eu_basis": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "estimation.eu_minutes": "eawf.runtime.daemon.methods.state._wave_close_rollup_config",
    "flow.budget.enforce": "eawf.runtime.daemon.methods.agent._resolve_budget_enforce",
    "prep.auto_resume": "eawf.workflow.skills.prep.PrepSkill._resolve_auto_resume",
    "research.agent_count": "eawf.workflow.skills.research.ResearchSkill._resolve_agents",
    "research.default_depth": "eawf.workflow.skills.research.ResearchSkill._resolve_depth",
    "ship.gauntlet": "eawf.workflow.skills.ship._resolve_gauntlet",
    "telemetry.db_kind": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "telemetry.enabled": "eawf.surfaces.cli.commands.metrics._read_telemetry_config",
    "ui.glyphs": "eawf.surfaces.tui.app._persisted_glyphs",
    "ui.theme": "eawf.surfaces.tui.app._persisted_theme",
    "vcs.conventions.release.cadence": (
        "eawf.runtime.vcs.coauthor.requires_phase_release_preflight"
    ),
    "verify.waiver_mode": "eawf.workflow.lifecycle.waivers.resolve_waiver_mode",
}

_INTERACTIVE_KEYS = {entry.key for entry in CONFIG_REGISTRY}
_HOOK_POLICY_KEYS = {entry.key for entry in _DECLARED_LEAF_KEYS if entry.key.startswith("hooks.")}
_BEHAVIOR_KEYS = _INTERACTIVE_KEYS | _HOOK_POLICY_KEYS
_RESERVED_BEHAVIOR_KEYS: frozenset[str] = frozenset(_BEHAVIOR_KEYS - _CONSUMER_BY_KEY.keys())


def _bind_behavior_metadata(entry: LeafKey) -> LeafKey:
    """Attach the audited consumer-or-reserved classification to *entry*."""
    if entry.key in _RESERVED_BEHAVIOR_KEYS:
        return entry.model_copy(update={"consumer": None, "reserved": True})
    consumer = _CONSUMER_BY_KEY.get(entry.key)
    if consumer is not None:
        return entry.model_copy(update={"consumer": consumer, "reserved": False})
    return entry


_LEAF_KEYS: tuple[LeafKey, ...] = tuple(
    _bind_behavior_metadata(entry) for entry in _DECLARED_LEAF_KEYS
)

_DECLARED_KEYS = {entry.key for entry in _LEAF_KEYS}
assert not (_BEHAVIOR_KEYS - _DECLARED_KEYS), (
    f"interactive config keys missing from leaf catalog: {sorted(_BEHAVIOR_KEYS - _DECLARED_KEYS)}"
)
assert not (_CONSUMER_BY_KEY.keys() - _BEHAVIOR_KEYS), (
    f"consumer binding targets non-behaviour leaf: "
    f"{sorted(_CONSUMER_BY_KEY.keys() - _BEHAVIOR_KEYS)}"
)
assert all(
    (entry.consumer is not None) ^ entry.reserved
    for entry in _LEAF_KEYS
    if entry.key in _BEHAVIOR_KEYS
), "every interactive and hooks.* leaf must declare exactly one consumer or reserved=true"


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
