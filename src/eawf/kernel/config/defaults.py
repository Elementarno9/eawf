"""Built-in (read-only) configuration defaults.

The contents mirror ``docs/architecture/envelope.md`` "Config schema
required sections". Every required section listed there appears here so
the merged config has every key resolvable to ``built-in`` when no later
layer overrides.

This module exposes a single read-only constant: :data:`BUILT_IN_DEFAULTS`.
Callers that need to mutate the structure (loaders, mergers) MUST deep-copy
first. The constant itself is treated as immutable; tests assert this contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH

# The literal name "built-in" is the canonical layer label everywhere — keep
# it in lockstep with :mod:`eawf.kernel.config.layered` and ``cli/commands/config.py``.
BUILT_IN_LAYER: str = "built-in"

# Single source of truth for the on-disk ``.ea/config.yaml`` schema version.
# Bumped to ``"1.0"`` in P25-W14 (C08 spec series) — the canonical layered
# taxonomy. Earlier marker values ``"1.1"`` (P14-W03 ``runtime.adapters``
# shim) and ``"2"`` (interim experimental) are auto-upgraded to ``"1.0"``
# by :mod:`eawf.kernel.config.migration`. The numeric ordering of the marker
# strings is irrelevant — they are opaque schema-shape identifiers.
CONFIG_SCHEMA_VERSION: str = "1.0"


_BUILT_IN_DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "config": {
        # When ``false``, ``eawf config get`` hides the layer-source column
        # in default output (operators that prefer terse listings).
        "layers_visible": True,
    },
    "cli": {
        "canonical_command": "eawf",
        "preferred_command": "eawf",
        "install_aliases": ["ea"],
        "omit_ea_alias": False,
    },
    "project": {
        "code": None,
        "title": None,
        "slug": None,
        "domains": [],
        "default_track": None,
        # Free-form project-level goal strings (one per list item). Surfaced
        # in dispatch envelopes + research/audit briefs so subagents see
        # project intent without re-deriving from state.
        "goals": [],
        # Quantitative goal targets keyed by metric name (e.g. p99 latency,
        # contributor count). Values are floats so the same registry entry
        # can carry rates / ratios / counts.
        "success_metrics": {},
    },
    "workspace": {
        "enabled": False,
        "code": None,
        "state_path": ".ea/state.json",
        "repos": {},
    },
    "profiles": {
        "enabled": ["core"],
        "catalog": [
            "core",
            "research",
            "python",
            "docs",
            "apps",
            "infra",
            "ml",
            "quant",
            "re",
            "game",
            "robotics",
        ],
        "conflict_resolution": "prompt",
        "safety_policy": "strictest_wins",
        # Content-hash trust ledger; key = profile id, value = sha256 of
        # the body the operator last accepted. The composition loader
        # cross-references this map when a profile body changes between
        # loads — drift surfaces as a prompt.
        "trusted": {},
    },
    "runtime": {
        "default": "claude",
        # ``adapters`` is the user-facing selector list. Built-in default
        # opts the project into the Claude adapter only; the wizard /
        # workspace overlay extends or replaces it.
        "adapters": ["claude"],
        # ``preference`` is the C08-canonical fallback ladder; first entry
        # is primary. The legacy-shim path in :mod:`eawf.kernel.config.layered`
        # synthesises ``preference`` from ``adapters`` when only the
        # latter is present.
        "preference": ["claude"],
        # Fallback policy applied when the primary runtime rejects a
        # dispatch (rate-limit / server error / timeout / API error).
        "fallback": {
            "on_errors": [
                "RUNTIME_RATE_LIMIT",
                "RUNTIME_SERVER_ERROR",
                "RUNTIME_TIMEOUT",
                "RUNTIME_API_ERROR",
            ],
            "retry_policy": "hybrid",
            "max_backoff_seconds": 90,
        },
        "slash_commands": [
            "init",
            "roadmap",
            "differentiate",
            "research",
            "prep",
            "audit",
            "ship",
            "review",
            "polish",
        ],
        # ``adapter_catalog`` holds the per-adapter config blocks. Indexed
        # by adapter id; the runtime spine reads it after consulting
        # ``adapters`` for the selector list.
        "adapter_catalog": {
            "claude": {
                "enabled": True,
                "plugin_install": "ask",
                "skills_path": ".claude/skills",
                "agents_path": ".claude/agents",
            },
            "codex": {"enabled": False, "status": "planned"},
            "opencode": {"enabled": False, "status": "deferred"},
        },
    },
    "ui": {
        "bare_command": "tui",
        "color": "auto",
        "glyphs": "auto",
        "refresh_ms": 1000,
        "tour_completed": False,
        "dashboard_panes": [
            "state",
            "roadmap",
            "hypotheses",
            "audits",
            "ship",
            "memory",
            "config",
        ],
    },
    # C09 (telemetry) projector reads these keys. Telemetry is opt-in
    # (``enabled`` defaults False) and strict-local: there is no export
    # endpoint key, so a projection / export never contacts an external
    # service. ``db_kind`` defaults to the always-available stdlib sqlite
    # backend; ``duckdb`` is the opt-in analytics upgrade.
    "telemetry": {
        "enabled": False,
        "export": {
            "format": "prom",
        },
        "window_default": "7d",
        "aggregate_window": "24h",
        "db_kind": "sqlite",
    },
    # Dispatch defaults — the per-skill or per-profile manifest still
    # wins; these are the bottom-of-stack values.
    "dispatch": {
        "session_policy_default": "hybrid",
        "session_handle_ttl_seconds": 86400,
        # Per-block token ceiling for injected role-tier dispatch blocks.
        # The renderer raises (never truncates) over the cap; the code
        # fallback is DEFAULT_ROLE_TIER_TOKEN_CAP when the leaf is unset.
        "role_tier_token_cap": 2400,
    },
    # Language-fit knobs. ``runtime`` is locked at ``python`` for
    # v0.3-v0.5 (D6); ``fast_extras`` opts in to PyO3 hot paths.
    "language": {
        "runtime": "python",
        "fast_extras": [],
    },
    "storage": {
        "state_path": ".ea/state.json",
        "stores_dir": ".ea/stores",
        "artifacts_dir": ".ea/artifacts",
        "rendered_dir": ".ea/artifacts/rendered",
        "generated_index": ".ea/indexes/generated.json",
        "content_addressed_blobs": True,
        "commit_jsonl": "all_nonlocal",
        "max_inline_chars": 2000,
        "lock_strategy": "sibling_lockfiles",
    },
    "research": {
        "folder": ".ea/artifacts/rendered/research",
        "auto_save": False,
        "default_depth": DEFAULT_RESEARCH_DEPTH.value,
        "default_sources": "both",
        "agent_count": 4,
    },
    "planning": {
        "approval": "ask",
        "max_parallel_waves": 4,
        "require_research_for_unknowns": True,
        # When False (default), ``/prep`` enters Claude Code plan mode and
        # presents the proposed wave DAG to the operator before any state
        # mutation. Set to True (or pass ``--auto-plan`` on the slash
        # invocation) to skip the proposal and dispatch the plan inline.
        "auto_plan": False,
    },
    # Operator planner + AskUserQuestion defaults. Each value is a closed
    # enum (see :mod:`eawf.kernel.config.schema`); the planner / AUQ
    # consumers read these in a later wave.
    "preferences": {
        "solution_bias": "balanced",
        "scope_size": "M",
        "auto_choose": "off",
    },
    # Doc-clarity prose-lint stack knobs (see ``ProseConfig`` in
    # :mod:`eawf.kernel.config.schema`). ``level`` is the strictness floor a
    # local layer may only tighten, never loosen, below the baseline the
    # active profile sets (agent-driven = strict, managed = loose); the
    # built-in baseline is ``standard``. ``clarity_judge`` / ``block_on_lint``
    # default ``null`` so each defers to the level until a layer opts a single
    # gate on or off within the level's floor.
    # Verify-spine repo-layer knobs. ``odr_blocking`` lets a repo opt into
    # the Oracle-Determinism-Ratio floor REFUSING an iter close (the profile
    # default keeps the floor advisory); a layer can only tighten -- the
    # overlay ORs onto the profile block, never loosens it.
    "verify": {
        "odr_blocking": False,
    },
    "prose": {
        "level": "standard",
        "clarity_judge": None,
        "block_on_lint": None,
    },
    "estimation": {
        "enabled": True,
        "eu_minutes": 30,
        "eu_basis": "api_duration",
        "realtime_recalibration": False,
        "calibration_profile": "eawf_v0_lockbox_2026_05",
        "idle_policy": "D30_non_agent_gap",
        "display": {
            "show_category": False,
            "show_raw_eu": True,
            "show_expected_time": True,
            "show_pessimistic_time": True,
            "eu_quantum": 0.25,
            "time_quantum_under_2h_minutes": 15,
            "time_quantum_over_2h_minutes": 30,
        },
        "buckets": {
            "overrides": {},
            "n_min": 5,
            "high_confidence_n": 30,
        },
    },
    "audit": {
        "default_checks": ["state", "tests", "lint", "typecheck", "docs"],
        "fix_safe": False,
        "flaky_retry_count": 1,
    },
    "ship": {
        "require_audit_pass": True,
        "require_memory_review": True,
        "use_vcs_policy": True,
    },
    "review": {
        "post_default": "ask",
        "template": "default",
        "require_checks_before_approve": True,
    },
    "polish": {
        "auto_apply_safe": False,
        "include_memory": True,
        "include_agent_memory": True,
        "deletion_policy": "recoverable_with_reason",
    },
    "flow": {
        # Per-stage gates. When False (default), ``/flow`` asks the operator
        # via ``AskUserQuestion`` before advancing past the named step. Set
        # the per-stage flag to True (or pass ``--auto-accept=<stage>[,...]``)
        # to advance without a prompt.
        "auto_accept": {
            "research": False,
            "prep": False,
            "audit": False,
            "ship": False,
            "review": False,
            "polish": False,
        },
        # Encourage subagent prompts and skill bodies to surface discrete
        # decisions through ``AskUserQuestion`` rather than free-text prompts.
        "ask_on_decisions": True,
        # Per-wave token-budget enforcement. ``soft`` (default) warns and
        # lets the wave continue past its cap; ``hard`` halts the wave at
        # the cap via the SIGTERM->SIGKILL ladder. ``multiplier`` scales
        # the base budget to derive the enforced cap (1.5 == 50% headroom).
        "budget": {
            "enforce": "soft",
            "multiplier": 1.5,
        },
    },
    "memory": {
        "stores": ["project", "track", "agent", "user"],
        "review_on_ship": True,
        "review_on_polish": True,
        "auto_promote": "ask",
        "prune": "ask",
        "max_injected_tokens": 2000,
    },
    "vcs": {
        "conventions": {
            "subject_style": "bracket",
            "wave_trailer": "Eawf-Wave",
            "release": {
                "cadence": "manual",
                "agent_driven": "per-phase",
            },
        },
        "commit_template": "state_scoped",
        "pr_template": "iter",
        "branch_pattern": "eawf/{project}/{scope}-{slug}",
        "checkpoint_requires_commit": True,
        "protected_branches": ["main", "master"],
        "auto_commit": "ask",
        "auto_push": "ask",
        "pr_open": "ask",
        "pr_merge_method": "merge",
        "squash_allowed": False,
        "delete_branch_after_merge": False,
        "require_ci_green": True,
        "require_review_before_merge": True,
        "force_push": "forbidden_protected",
        "coauthor": {
            "mode": "runtime",
            "default_runtime": "claude",
            "project": None,
            "trailers": {
                "claude": {
                    "name": "Claude",
                    "email": "noreply@anthropic.com",
                },
                "codex": {
                    "name": "Codex",
                    "email": "noreply@openai.com",
                },
            },
            "require_trailer": True,
        },
    },
    "worktrees": {
        "enabled": "auto",
        "root": ".worktrees",
        "merge_mode": "cherry_pick",
        "use_for_parallel_writers": True,
        "use_for_risky_changes": True,
        "use_for_readonly_research": False,
        "preserve_on_conflict": True,
        "remove_when_clean": True,
    },
    "acceptance": {
        "commands": {
            "tests": None,
            "lint": None,
            "typecheck": None,
            "build": None,
        },
        "required_before_ship": ["state"],
    },
    "security": {
        "secrets_policy": "env_refs_only",  # pragma: allowlist secret
        "env_ref_syntax": "${ENV:NAME}",
        "permission_mode": "ask_first",
        "secret_scan": True,
        "store_scan_before_checkpoint": True,
        "store_scan_on_finding": "block",
        "allow_destructive": "ask",
    },
    "hooks": {
        "policy": "mixed_strict",
        "timeout_seconds": 30,
        "enabled": ["state_validate", "generated_drift", "post_edit_lint"],
        "fail_closed": ["state_validate", "secret_scan", "protected_vcs"],
        "fail_open": ["post_edit_lint", "statusline", "memory_capture"],
        "ask_on_fail": ["overwrite_conflict", "destructive_action"],
    },
    "mcp": {
        "default_policy": "ask_install",
        "manage_only_owner": "eawf",
        "env_ref_syntax": "${ENV:NAME}",
        "servers": {},
    },
    "statusline": {
        "modules_default": "ask_per_module",
        "modules_available": [
            "state",
            "git",
            "model_session_cwd",
            "context_tokens",
            "mcp_health",
            "hooks_plugins",
            "memory",
            "token_saving",
        ],
        "glyph_mode": "auto",
        "color_mode": "auto",
        "rows": 1,
    },
    "docs": {
        "generated_default_dir": ".ea/artifacts/rendered",
        "generation_policy": "ask_per_category",
        "categories": [
            "roadmap",
            "research",
            "audit",
            "decisions",
            "incidents",
            "memory",
            "status",
        ],
    },
    "commands": {
        "inventory_policy": "full_io_spec_before_code",
    },
    "state_schema": {
        "strictness": "full_strict_schema_before_code",
        "id_padding": 2,
    },
    "daemon": {
        # When True (default since P24-W10), state + config + registry
        # mutations route through the daemon RPCs (``state.mutate``,
        # ``config.set_layer_value``, ``registry.update``). Flip to
        # False for the V1 daemonless carve-out (CI, read-only one-
        # shot, recovery shell); the in-process portalocker path
        # remains as the fallback. ``EAWF_DAEMONLESS=1`` is the
        # process-level escape hatch and overrides this flag.
        "proxy_enabled": True,
        # Idle window after which the daemon self-shuts-down when no
        # subscribers or in-flight mutations are live (seconds).
        # Aligned with the Anthropic prompt-cache TTL (5 min) so a
        # subscriber reconnect after a cache window does not racing-
        # spawn the daemon mid-warmup.
        "idle_timeout_seconds": 300,
        # Per-handle TTL for the session table sweep (seconds);
        # W07 wires the sweep.
        "session_handle_ttl_seconds": 86400,
    },
}


def built_in_defaults() -> dict[str, Any]:
    """Return a fresh deep-copy of the built-in defaults.

    Callers in :mod:`eawf.kernel.config.layered` mutate the returned dict via deep
    merge — they MUST receive a fresh copy each call so successive merges do
    not pollute the read-only baseline.
    """
    return deepcopy(_BUILT_IN_DEFAULTS)


# Public read-only view: callers that just want to inspect (e.g. tests) can
# use this directly. It MUST NOT be mutated; deep-copy first if needed.
BUILT_IN_DEFAULTS: dict[str, Any] = _BUILT_IN_DEFAULTS
