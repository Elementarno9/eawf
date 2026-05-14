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

# The literal name "built-in" is the canonical layer label everywhere — keep
# it in lockstep with :mod:`eawf.config.layered` and ``cli/commands/config.py``.
BUILT_IN_LAYER: str = "built-in"

# Single source of truth for the on-disk ``.ea/config.yaml`` schema version.
# Bumped to ``"1.1"`` in P14-W03 to introduce ``runtime.adapters: list[str]``
# (D14); the loader keeps a deprecation shim that accepts legacy ``"1.0"``
# config files whose ``runtime.kind`` is the only adapter selector.
CONFIG_SCHEMA_VERSION: str = "1.1"


_BUILT_IN_DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
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
        "default_subproject": None,
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
    },
    "runtime": {
        "default": "claude",
        # ``adapters`` is the user-facing selector list (D14 / P14-W03).
        # Built-in default opts the project into the Claude adapter only;
        # the wizard / workspace overlay extends or replaces it.
        "adapters": ["claude"],
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
        "default_depth": "normal",
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
    "estimation": {
        "enabled": True,
        "eu_minutes": 30,
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
    },
    "memory": {
        "stores": ["project", "subproject", "agent", "user"],
        "review_on_ship": True,
        "review_on_polish": True,
        "auto_promote": "ask",
        "prune": "ask",
        "max_injected_tokens": 2000,
    },
    "vcs": {
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
}


def built_in_defaults() -> dict[str, Any]:
    """Return a fresh deep-copy of the built-in defaults.

    Callers in :mod:`eawf.config.layered` mutate the returned dict via deep
    merge — they MUST receive a fresh copy each call so successive merges do
    not pollute the read-only baseline.
    """
    return deepcopy(_BUILT_IN_DEFAULTS)


# Public read-only view: callers that just want to inspect (e.g. tests) can
# use this directly. It MUST NOT be mutated; deep-copy first if needed.
BUILT_IN_DEFAULTS: dict[str, Any] = _BUILT_IN_DEFAULTS
