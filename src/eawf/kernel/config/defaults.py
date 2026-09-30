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


_BUILT_IN_DEFAULTS: dict[str, Any] = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "profiles": {
        "enabled": ["core"],
        # Content-hash trust ledger; key = profile id, value = sha256 of
        # the body the operator last accepted. The composition loader
        # cross-references this map when a profile body changes between
        # loads — drift surfaces as a prompt.
        "trusted": {},
        # Managed-certification ledger; key = profile id, value = the
        # digest an enriched profile is certified under. Committed at the
        # repo layer so certifying a profile is a reviewed change.
        "certified": {},
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
        # Liveness per runtime: the silence a Run may keep before it is flagged
        # stalled, and how long Claude's permission hook waits for a decision.
        "claude": {
            "stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS,
            "permission_wait_s": DEFAULT_PERMISSION_WAIT_SECONDS,
        },
        "codex": {"stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS},
        "opencode": {"stall_interval_s": DEFAULT_STALL_INTERVAL_SECONDS},
    },
    "ui": {
        "theme": "dark",
        "glyphs": "auto",
        "toasts": "important",
    },
    # Roadmap EU/hour rollup view; mirrors the defaults of
    # :class:`eawf.surfaces.render.plan_view.EuViewConfig`.
    "tui": {
        "eu_view": {
            "density": "full",
            "fields": ["work_sum", "critical_path", "queue", "realistic"],
        },
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
    "dispatch": {
        # Per-block token ceiling for injected role-tier dispatch blocks.
        # The renderer raises (never truncates) over the cap; the code
        # fallback is DEFAULT_ROLE_TIER_TOKEN_CAP when the leaf is unset.
        "role_tier_token_cap": 2400,
    },
    # Per-role runtime tool grants appended to each rendered subagent's
    # built-in allowlist. Empty by default so an unconfigured repo renders
    # exactly the tools AGENT_REGISTRY declares; see
    # :class:`eawf.kernel.config.schema.AgentsConfig` for the accepted shape.
    "agents": {
        "extra_tools": {},
    },
    "research": {
        "auto_save": False,
        "default_depth": DEFAULT_RESEARCH_DEPTH.value,
        "agent_count": 4,
    },
    "planning": {
        "max_parallel_waves": 4,
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
        "require_iter_audit_accepted": False,
        # A rule the operator re-types more often than this within a release
        # must be triaged into a mechanism before the release tags.
        "retyped_rule_threshold": 3,
        "waiver_mode": "B",
    },
    "estimation": {
        "eu_minutes": 30,
        "eu_basis": "api_duration",
    },
    "audit": {
        # Default /audit check-plan breadth: quick narrows to a smoke set,
        # standard is the full default set, deep is the widest.
        "default_level": "standard",
    },
    # ``/prep`` runtime knobs. ``auto_resume`` leads /prep's emitted claim
    # actions with the dispatch-resume action so a leaked ``dispatch_paused``
    # flag does not silently reject the claim batch.
    "prep": {
        "auto_resume": True,
    },
    "ship": {
        # Ship gauntlet breadth: full (default, mandatory for migration waves
        # + iter close) runs every gate; scoped is legal only for re-runs.
        "gauntlet": "full",
    },
    "review": {
        "default_level": "medium",
    },
    "flow": {
        # Per-transition gates. A true value authorises advancing after the
        # named completed stage. Protected actions inside the next skill keep
        # their own explicit confirmation requirements.
        "advance_after": {
            "research": False,
            "prep": False,
            "audit": False,
            "polish": False,
        },
        # Per-wave token-budget enforcement. ``soft`` (default) warns and
        # lets the wave continue past its cap; ``hard`` halts the wave at
        # the cap via the SIGTERM->SIGKILL ladder. ``multiplier`` scales
        # the base budget to derive the enforced cap (1.5 == 50% headroom).
        "budget": {
            "enforce": "soft",
            "multiplier": 1.5,
        },
        # Stop re-entering a failing flow stage past this many repair cycles.
        "max_repair_cycles": 3,
    },
    "vcs": {
        "conventions": {
            "subject_style": "trailer",
            "wave_trailer": "Eawf-Wave",
            "release": {
                "cadence": "manual",
            },
        },
        "checkpoint_requires_commit": True,
        "pr_merge_method": "merge",
        "squash_allowed": False,
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
    "acceptance": {
        "commands": {
            "tests": None,
            "lint": None,
            "typecheck": None,
            "build": None,
        },
        "required_before_ship": ["state"],
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
        # Seconds an ended attempt's session handle is kept before the
        # daemon's sweep prunes it.
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
