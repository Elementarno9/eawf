"""Built-in (read-only) configuration defaults.

Every catalogued leaf that has a shipped value appears here, so the merged
config resolves each such key to ``built-in`` when no later layer
overrides it.

This module exposes a single read-only constant: :data:`BUILT_IN_DEFAULTS`.
Callers that need to mutate the structure (loaders, mergers) MUST deep-copy
first. The constant itself is treated as immutable; tests assert this contract.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from eawf.kernel.config.schema import (
    DEFAULT_PERMISSION_WAIT_SECONDS,
    DEFAULT_STALL_INTERVAL_SECONDS,
)
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


#: Where every built-in provider profile and route says it came from, so a
#: compiled Run's source map tells a shipped default from an operator record.
BUILT_IN_PROVIDER_SOURCE: str = "config://built-in/runtime"

#: The semantic tools a built-in profile lets a Task Run ask for; a Run's
#: capsule may grant fewer, never more.
_TASK_TOOLS: list[str] = [
    "repo_read",
    "repo_search",
    "diff_read",
    "workspace_apply_patch",
    "report_progress",
    "attach_evidence",
    "submit_report",
]


def _built_in_profile(
    profile_id: str, driver_ref: str, auth_profile_ref: str, models: list[str], kind: str
) -> dict[str, Any]:
    """Return one built-in profile running Task work on one shipped launcher.

    Only the capabilities a machine probe can attest are required, and writes
    are confined to the leased workspace with no network, so the profile asks
    for nothing a fresh machine's certification cannot back. The semantic
    tools are optional: only a certification canary observes them, and until
    one has, a Run compiles with no tool granted rather than not at all.
    """
    return {
        "profile_id": profile_id,
        "driver_ref": driver_ref,
        "auth_profile_ref": auth_profile_ref,
        "model_policy": {"allowed": models, "default": models[0]},
        "required_capabilities": ["streaming", "tool_use"],
        "optional_capabilities": ["semantic_tools"],
        "tool_policy": {"allow": list(_TASK_TOOLS)},
        "limits": {"wall_seconds": 3600, "output_bytes": 1_048_576},
        "sandbox": {
            "mode": "workspace_write",
            "network": "deny",
            "filesystem_policy_ref": "policy://filesystem/task-workspace",
        },
        "authority_ceiling": {
            "state": "proposal_only",
            "workspace": "scoped_write",
            "git": "read_metadata",
        },
        "provider_options": {"provider_kind": kind},
        "revision": 1,
        "source_ref": BUILT_IN_PROVIDER_SOURCE,
    }


_BUILT_IN_DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "profiles": {
        "enabled": ["core"],
        # Content-hash trust ledger; key = profile id, value = sha256 of
        # the body the operator last accepted. The composition loader
        # cross-references this map when a profile body changes between
        # loads — drift surfaces as a prompt.
        "trusted": {},
    },
    "runtime": {
        # ``adapters`` is the user-facing selector list. Built-in default
        # opts the project into the Claude adapter only; the wizard /
        # workspace overlay extends or replaces it.
        "adapters": ["claude-code"],
        # ``preference`` is the C08-canonical fallback ladder; first entry
        # is primary. The legacy-shim path in :mod:`eawf.kernel.config.layered`
        # synthesises ``preference`` from ``adapters`` when only the
        # latter is present.
        "preference": ["claude-code"],
        # Probe a runtime version no certification covers when a Run first
        # reports it, so its controls certify without an operator step.
        "auto_certify": True,
        # Liveness per runtime: the silence a Run may keep before it is flagged
        # stalled, and how long Claude's permission hook waits for a decision.
        "claude": {
            "stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS,
            "permission_wait_s": DEFAULT_PERMISSION_WAIT_SECONDS,
        },
        "codex": {"stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS},
        "opencode": {"stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS},
        # Days a Run's full message text stays in the machine-local store.
        "transcript": {"retention_days": 30},
        # One profile per shipped launcher and the routes that send Task Runs
        # and Campaign rounds to them, so a fresh tree compiles a Run with
        # nothing written.
        # The provider loader files these under the global layer, beneath
        # the operator's own records; a repository layer can only narrow.
        "profiles": [
            _built_in_profile(
                "claude-default",
                "driver://claude-agent-sdk/v1",
                "auth://local-session/claude",
                ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5"],
                "claude",
            ),
            _built_in_profile(
                "codex-default",
                "driver://codex-python-sdk/v1",
                "auth://local-session/codex",
                ["gpt-5.5", "gpt-5.3-codex-spark"],
                "codex",
            ),
        ],
        "routes": [
            {
                "route_id": "task",
                "match": {"scope_kind": "task"},
                "allowed_profiles": ["claude-default", "codex-default"],
                "revision": 1,
                "source_ref": BUILT_IN_PROVIDER_SOURCE,
            },
            # A Campaign round reads the repository and writes nothing; the
            # compiler's safe baseline narrows the shared profiles to a
            # read-only sandbox and workspace for it.
            {
                "route_id": "campaign-research",
                "match": {
                    "scope_kind": "campaign",
                    "purpose": ["research"],
                    "requires_mutation": False,
                },
                "allowed_profiles": ["claude-default", "codex-default"],
                "revision": 1,
                "source_ref": BUILT_IN_PROVIDER_SOURCE,
            },
        ],
    },
    "ui": {
        "theme": "dark",
        "glyphs": "auto",
        "toasts": "important",
    },
    # C09 (telemetry) projector reads these keys. Ingestion is ON by
    # default because an off-by-default projector never runs in CI or
    # dogfood, so every duration / cost distribution reads as an empty
    # cohort and the metrics surface is decorative. Collection is
    # strict-local: there is no export endpoint key, so a projection /
    # export never contacts an external service, which is what makes
    # on-by-default safe. ``db_kind`` defaults to the always-available
    # stdlib sqlite backend; ``duckdb`` is the opt-in analytics upgrade.
    "telemetry": {
        "enabled": True,
        "db_kind": "sqlite",
    },
    # Per-role runtime tool grants appended to each rendered subagent's
    # built-in allowlist. Empty by default so an unconfigured repo renders
    # exactly the tools AGENT_REGISTRY declares; see
    # :class:`eawf.kernel.config.schema.AgentsConfig` for the accepted shape.
    "agents": {
        "extra_tools": {},
    },
    "research": {
        "default_depth": DEFAULT_RESEARCH_DEPTH.value,
        "agent_count": 4,
    },
    # AskUserQuestion auto-pick default; a closed enum (see
    # :mod:`eawf.kernel.config.schema`).
    "preferences": {
        "auto_choose": "off",
    },
    # Verify-spine repo-layer knobs. ``odr_blocking`` lets a repo opt into
    # the Oracle-Determinism-Ratio floor REFUSING an iter close (the profile
    # default keeps the floor advisory); a layer can only tighten -- the
    # overlay ORs onto the profile block, never loosens it.
    "verify": {
        # The ceilings a jury's calibration must clear before its verification-site
        # verdicts block: a Brier score and a co-error rate, both lower-is-better.
        "jury_max_brier": 0.25,
        "jury_max_co_error": 0.10,
        "juror_wall_clock_seconds": 600.0,
        "odr_blocking": False,
        # A rule the operator re-types more often than this within a release
        # must be triaged into a mechanism before the release tags.
        "retyped_rule_threshold": 3,
        "waiver_mode": "B",
    },
    "estimation": {
        "eu_minutes": 30,
        "eu_basis": "api_duration",
    },
    "flow": {
        # Per-wave token-budget enforcement. ``soft`` (default) warns and
        # lets the wave continue past its cap; ``hard`` halts the wave at
        # the cap via the SIGTERM->SIGKILL ladder. ``multiplier`` scales
        # the base budget to derive the enforced cap (1.5 == 50% headroom).
        "budget": {
            "enforce": "soft",
            "multiplier": 1.5,
        },
    },
    "vcs": {
        "conventions": {
            "subject_style": "trailer",
        },
        "checkpoint_requires_commit": True,
        "integration_commit_unit": "batch",
        "task_reference": "trailer",
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
    },
    # Heavy spike artefacts live under ``scratch_root``, outside the
    # repository; doctor warns once ``.ea/local`` grows past ``size_warn_mb``.
    "local": {
        "scratch_root": "~/.eawf/scratch",
        "size_warn_mb": 1024,
    },
    "worktrees": {
        "remove_when_clean": True,
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
